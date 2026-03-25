"""
fetcher.py — Pull defense news (RSS), contract opportunities (SAM.gov, optional),
and contract award intelligence (USASpending.gov, no API key required).
"""

import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import feedparser
import requests
import yaml

log = logging.getLogger(__name__)

_PROFILE: dict | None = None


def _load_profile() -> dict:
    global _PROFILE
    if _PROFILE is None:
        with open("company_profile.yaml") as f:
            _PROFILE = yaml.safe_load(f)
    return _PROFILE


def _lookback_days() -> int:
    return int(os.getenv("LOOKBACK_DAYS", "7"))


def _cutoff_dt() -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=_lookback_days())


def _parse_feed_date(entry) -> datetime | None:
    for attr in ("published_parsed", "updated_parsed"):
        val = getattr(entry, attr, None)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


# ──────────────────────────────────────────────────────────────────────────────
# RSS News
# ──────────────────────────────────────────────────────────────────────────────

def fetch_all_news() -> list[dict[str, Any]]:
    """Fetch articles from all RSS feeds defined in company_profile.yaml."""
    profile = _load_profile()
    feeds = profile.get("rss_feeds", [])
    cutoff = _cutoff_dt()
    items: list[dict[str, Any]] = []

    for feed_cfg in feeds:
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        try:
            log.info("Fetching RSS: %s", name)
            parsed = feedparser.parse(url)

            for entry in parsed.entries:
                published = _parse_feed_date(entry)
                # Skip articles older than the lookback window
                if published and published < cutoff:
                    continue

                summary = (
                    getattr(entry, "summary", None)
                    or getattr(entry, "description", None)
                    or ""
                )
                # Strip HTML tags from summary for cleaner AI input
                import re
                summary = re.sub(r"<[^>]+>", " ", summary).strip()[:600]

                items.append(
                    {
                        "type": "news",
                        "source": name,
                        "title": entry.get("title", "").strip(),
                        "url": entry.get("link", ""),
                        "summary": summary,
                        "published": published.isoformat() if published else "",
                    }
                )

        except Exception as exc:
            log.warning("Failed to fetch '%s': %s", name, exc)

    log.info("RSS total: %d articles fetched", len(items))
    return items


# ──────────────────────────────────────────────────────────────────────────────
# SAM.gov Opportunities
# ──────────────────────────────────────────────────────────────────────────────

_SAM_URL = "https://api.sam.gov/opportunities/v2/search"


def fetch_sam_opportunities() -> list[dict[str, Any]]:
    """
    Search SAM.gov for recent contract opportunities using profile-driven keywords.

    Requires SAM_GOV_API_KEY environment variable.
    Free API keys: https://sam.gov/profile/details
    """
    api_key = os.getenv("SAM_GOV_API_KEY", "").strip()
    if not api_key:
        log.warning(
            "SAM_GOV_API_KEY not set — skipping contract opportunity fetch. "
            "Get a free key at https://sam.gov/profile/details"
        )
        return []

    profile = _load_profile()
    search_terms: list[str] = profile.get("sam_search_terms", [])
    posted_from = (_cutoff_dt()).strftime("%m/%d/%Y")

    all_opps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for term in search_terms:
        try:
            params = {
                "api_key": api_key,
                "q": term,
                "postedFrom": posted_from,
                "limit": 25,
                "offset": 0,
                "active": "Yes",
            }
            log.info("SAM.gov search: '%s'", term)
            resp = requests.get(_SAM_URL, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()

            for opp in data.get("opportunitiesData", []):
                notice_id = opp.get("noticeId", "")
                if not notice_id or notice_id in seen_ids:
                    continue
                seen_ids.add(notice_id)

                # Build a readable agency string from hierarchy
                agency = (
                    opp.get("fullParentPathName")
                    or opp.get("organizationHierarchy", "")
                    or opp.get("officeAddress", {}).get("city", "")
                )

                description = (opp.get("description") or "")[:700]

                all_opps.append(
                    {
                        "type": "opportunity",
                        "source": "SAM.gov",
                        "title": (opp.get("title") or "").strip(),
                        "url": f"https://sam.gov/opp/{notice_id}/view",
                        "agency": agency,
                        "naics": opp.get("naicsCode", ""),
                        "set_aside": opp.get("typeOfSetAside", "") or opp.get("typeOfSetAsideDescription", ""),
                        "notice_type": opp.get("type", ""),
                        "solicitation_number": opp.get("solicitationNumber", ""),
                        "posted_date": opp.get("postedDate", ""),
                        "response_deadline": opp.get("responseDeadLine", ""),
                        "description": description,
                        "notice_id": notice_id,
                    }
                )

        except requests.HTTPError as exc:
            log.warning("SAM.gov HTTP error for '%s': %s", term, exc)
        except Exception as exc:
            log.warning("SAM.gov fetch failed for '%s': %s", term, exc)

    log.info("SAM.gov total: %d unique opportunities", len(all_opps))
    return all_opps


# ──────────────────────────────────────────────────────────────────────────────
# USASpending.gov — Contract Award Intelligence (no API key required)
# ──────────────────────────────────────────────────────────────────────────────

_USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

# NAICS codes drawn from company_profile.yaml
_DEFAULT_NAICS = [
    "541330", "541715", "541512", "541519",
    "541690", "334511", "517410", "336411",
]


def fetch_usaspending_awards() -> list[dict[str, Any]]:
    """
    Fetch recent DoD/IC contract awards from USASpending.gov.

    Completely free — no registration or API key required.
    Returns award-intelligence items useful for identifying active programs,
    incumbent contractors, and teaming targets.
    """
    profile = _load_profile()
    naics_codes = [
        str(n) for n in profile.get("company", {}).get("naics_codes", _DEFAULT_NAICS)
    ]
    cutoff = _cutoff_dt()
    start_date = cutoff.strftime("%Y-%m-%d")
    end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    payload = {
        "filters": {
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "naics_codes": {"require": naics_codes},
            "award_type_codes": ["A", "B", "C", "D"],  # all contract types
        },
        "fields": [
            "Award ID",
            "Recipient Name",
            "Award Amount",
            "Description",
            "Awarding Agency",
            "Awarding Sub Agency",
            "NAICS Code",
            "NAICS Description",
            "Period of Performance Start Date",
            "Period of Performance Current End Date",
            "Contract Award Type",
        ],
        "sort": "Award Amount",
        "order": "desc",
        "limit": 50,
        "page": 1,
    }

    try:
        log.info("USASpending.gov: fetching recent awards (%s → %s)", start_date, end_date)
        resp = requests.post(_USASPENDING_URL, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("USASpending.gov fetch failed: %s", exc)
        return []

    awards: list[dict[str, Any]] = []
    for result in data.get("results", []):
        amount = result.get("Award Amount") or 0
        recipient = result.get("Recipient Name", "Unknown")
        agency = result.get("Awarding Sub Agency") or result.get("Awarding Agency", "")
        naics = result.get("NAICS Code", "")
        naics_desc = result.get("NAICS Description", "")
        award_id = result.get("Award ID", "")
        description = (result.get("Description") or "")[:600]
        perf_start = result.get("Period of Performance Start Date", "")
        perf_end = result.get("Period of Performance Current End Date", "")

        # Build a useful summary for the AI scorer
        summary = (
            f"{recipient} awarded ${amount:,.0f}" if amount else f"Award to {recipient}"
        )
        if naics_desc:
            summary += f" for {naics_desc}"
        if description:
            summary += f". {description}"

        awards.append(
            {
                "type": "opportunity",
                "source": "USASpending.gov",
                "title": f"{recipient} — {agency}" if agency else recipient,
                "url": f"https://www.usaspending.gov/award/{award_id}" if award_id else "https://www.usaspending.gov",
                "agency": agency,
                "naics": naics,
                "set_aside": "",
                "notice_type": result.get("Contract Award Type", "Award"),
                "solicitation_number": award_id,
                "posted_date": perf_start,
                "response_deadline": perf_end,
                "description": summary,
                "notice_id": award_id,
                "award_amount": amount,
            }
        )

    log.info("USASpending.gov total: %d awards fetched", len(awards))
    return awards
