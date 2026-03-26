"""
fetcher.py — Pull defense news (RSS), contract opportunities (SAM.gov, optional),
and contract award intelligence (USASpending.gov + FPDS, no API keys required).
"""

import os
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote_plus

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
    """Fetch articles from all RSS feeds defined in company_profile.yaml.
    Deduplicates within the run by URL."""
    profile = _load_profile()
    feeds = profile.get("rss_feeds", [])
    cutoff = _cutoff_dt()
    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for feed_cfg in feeds:
        name = feed_cfg["name"]
        url = feed_cfg["url"]
        try:
            log.info("Fetching RSS: %s", name)
            resp = requests.get(url, timeout=10, headers={"User-Agent": "DefenseBDDigest/1.0"})
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)

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
                summary = re.sub(r"<[^>]+>", " ", summary).strip()[:600]

                url = entry.get("link", "")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)

                items.append(
                    {
                        "type": "news",
                        "source": name,
                        "title": entry.get("title", "").strip(),
                        "url": url,
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
    Fetch recent DoD contract opportunities from SAM.gov in a single API call.

    Uses a broad DoD department filter instead of keyword searches — one request
    returns up to 100 opportunities, then the keyword pre-filter and Claude scoring
    handle relevance. This conserves the free-tier daily quota (~10 requests/day).

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

    posted_from = (_cutoff_dt()).strftime("%m/%d/%Y")
    all_opps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # Two queries: DoD (covers Army/Navy/DARPA/MDA/NSA/NGA/DIA/NRO) + ODNI (IC community).
    # CIA is rarely on SAM so skipped. 2 requests = well within free-tier daily quota.
    # Notice types: p=presol, r=sources sought, s=special notice, k=combined synopsis, o=solicitation
    # Excludes award notices (a) since USASpending already covers awards.
    dept_queries = [
        ("DEPT OF DEFENSE", "DoD"),
        ("OFFICE OF THE DIRECTOR OF NATIONAL INTELLIGENCE", "ODNI/IC"),
    ]
    for deptname, label in dept_queries:
        params = {
            "api_key": api_key,
            "deptname": deptname,
            "postedFrom": posted_from,
            "ptype": "p,r,s,k,o",
            "limit": 100,
            "offset": 0,
        }
        log.info("SAM.gov: fetching %s solicitations posted since %s", label, posted_from)
        try:
            resp = requests.get(_SAM_URL, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            for opp in data.get("opportunitiesData", []):
                notice_id = opp.get("noticeId", "")
                if not notice_id or notice_id in seen_ids:
                    continue
                seen_ids.add(notice_id)

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

            total_available = data.get("totalRecords", len(all_opps))
            log.info("SAM.gov %s: %d results (of %d total in window)", label, len(all_opps), total_available)

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            log.warning("SAM.gov HTTP %s error (%s): %s", status, label, exc)
            if exc.response is not None and exc.response.status_code == 429:
                log.warning("SAM.gov daily quota exhausted — will retry tomorrow")
                break
        except Exception as exc:
            log.warning("SAM.gov fetch failed (%s): %s", label, exc)
        else:
            import time; time.sleep(1)  # brief pause between the two requests

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


# ──────────────────────────────────────────────────────────────────────────────
# USASpending.gov — Expiring Contract / Recompete Watch (no API key required)
# ──────────────────────────────────────────────────────────────────────────────

def fetch_expiring_contracts(days_min: int = 180, days_max: int = 730) -> list[dict[str, Any]]:
    """
    Find DoD contracts in our NAICS space expiring 6 months to 2 years from now.

    This horizon surfaces strategic recompete candidates early enough to build
    relationships before the RFP drops — ahead of GovWin and most competitors.
    Results carry a 'recompete' type for distinct email rendering.
    """
    profile = _load_profile()
    naics_codes = [
        str(n) for n in profile.get("company", {}).get("naics_codes", _DEFAULT_NAICS)
    ]
    now = datetime.now(timezone.utc)
    expire_min = now + timedelta(days=days_min)
    expire_max = now + timedelta(days=days_max)

    # Query a 5-year award window to capture contracts of all durations that
    # fall in our 6mo–2yr expiry horizon.  Sort ascending so soonest-expiring
    # results appear on page 1 (allows easy Python filtering).
    start_date = (now - timedelta(days=5 * 365)).strftime("%Y-%m-%d")
    end_date = now.strftime("%Y-%m-%d")

    payload = {
        "filters": {
            "time_period": [{"start_date": start_date, "end_date": end_date}],
            "naics_codes": {"require": naics_codes},
            "award_type_codes": ["A", "B", "C", "D"],
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
        "sort": "Period of Performance Current End Date",
        "order": "asc",
        "limit": 100,
        "page": 1,
    }

    try:
        log.info("USASpending.gov: scanning for recompetes expiring %d–%d days out", days_min, days_max)
        resp = requests.post(_USASPENDING_URL, json=payload, timeout=20)
        resp.raise_for_status()
        results = resp.json().get("results", [])
    except Exception as exc:
        log.warning("USASpending expiring contracts fetch failed: %s", exc)
        return []

    expiring: list[dict[str, Any]] = []
    for award in results:
        end_str = award.get("Period of Performance Current End Date", "")
        if not end_str:
            continue
        try:
            end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        # Only contracts in the 6mo–2yr strategic horizon
        if not (expire_min <= end_dt <= expire_max):
            continue

        days_remaining = (end_dt - now).days
        recipient = award.get("Recipient Name", "Unknown")
        agency = award.get("Awarding Sub Agency") or award.get("Awarding Agency", "")
        amount = award.get("Award Amount") or 0
        description = (award.get("Description") or "")[:500]
        award_id = award.get("Award ID", "")

        expiring.append(
            {
                "type": "recompete",
                "source": "USASpending.gov",
                "title": f"{agency} — {description[:70] or award_id}".strip(" —"),
                "url": f"https://www.usaspending.gov/award/{award_id}" if award_id else "https://www.usaspending.gov",
                "agency": agency,
                "incumbent": recipient,
                "award_amount": amount,
                "days_remaining": days_remaining,
                "end_date": end_str,
                "notice_type": f"Expires {end_str} ({days_remaining}d)",
                "response_deadline": end_str,
                "set_aside": "",
                "naics": award.get("NAICS Code", ""),
                # Rich summary for Claude scoring
                "summary": (
                    f"Contract expiring in {days_remaining} days. "
                    f"Incumbent: {recipient}. "
                    f"Value: ${amount:,.0f}. "
                    f"{description}"
                ),
                "description": description,
                "related_news": [],  # populated by digest.py cross-reference
            }
        )

    # Return soonest-expiring first, cap at 15
    expiring.sort(key=lambda x: x["days_remaining"])
    log.info(
        "USASpending recompetes: %d found in %d–%d day horizon",
        len(expiring), days_min, days_max,
    )
    return expiring[:15]


# ──────────────────────────────────────────────────────────────────────────────
# FPDS — Federal Procurement Data System (no API key required)
# ──────────────────────────────────────────────────────────────────────────────

_FPDS_BASE = "https://www.fpds.gov/ezsearch/FEEDS/ATOM?FEEDNAME=AWARD&q="

# FPDS requires short, simple queries — long phrases and hyphens break its parser.
# Keep these to 1-3 words; pull from profile keywords that map cleanly.
_FPDS_QUERIES = [
    "directed energy",
    "high energy laser",
    "ISR exploitation",
    "sensor fusion",
    "TITAN",
    "Linchpin",
    "DEVCOM",
    "fiber laser",
    "SIGINT",
    "FPGA signal processing",
]


def fetch_fpds_awards() -> list[dict[str, Any]]:
    """
    Fetch recent contract awards from FPDS-NG Atom feeds.

    NOTE: FPDS blocks GitHub Actions IP ranges with 400 errors on all queries.
    Returns empty list until a working access method is identified.
    """
    log.info("FPDS: skipped (API blocks GitHub Actions IPs)")
    return []

    # --- unreachable until FPDS access is resolved ---
    cutoff = _cutoff_dt()  # noqa: F841

    awards: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for term in _FPDS_QUERIES:
        url = _FPDS_BASE + quote_plus(term)
        try:
            log.info("FPDS search: '%s'", term)
            resp = requests.get(
                url, timeout=15, headers={"User-Agent": "DefenseBDDigest/1.0"}
            )
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)

            for entry in parsed.entries:
                link = entry.get("link", "")
                if not link or link in seen_ids:
                    continue
                seen_ids.add(link)

                published = _parse_feed_date(entry)
                if published and published < cutoff:
                    continue

                title = (entry.get("title") or "").strip()
                summary = re.sub(
                    r"<[^>]+>",
                    " ",
                    (getattr(entry, "summary", None) or ""),
                ).strip()[:600]

                awards.append(
                    {
                        "type": "opportunity",
                        "source": "FPDS",
                        "title": title,
                        "url": link,
                        "agency": "",  # Claude extracts this from title/summary
                        "naics": "",
                        "set_aside": "",
                        "notice_type": "Contract Award",
                        "solicitation_number": "",
                        "posted_date": published.isoformat() if published else "",
                        "response_deadline": "",
                        "description": summary,
                        "notice_id": link,
                    }
                )

        except Exception as exc:
            log.warning("FPDS search failed for '%s': %s", term, exc)

    log.info("FPDS total: %d unique awards fetched", len(awards))
    return awards


# ──────────────────────────────────────────────────────────────────────────────
# SBIR.gov — Open DoD Topics (no API key required)
# ──────────────────────────────────────────────────────────────────────────────

_SBIR_API = "https://api.sbir.gov/public/api/solicitations"

# Components to filter for — anything with these strings in agency/branch is relevant
_SBIR_TARGET_AGENCIES = {
    "army", "darpa", "devcom", "mda", "missile defense",
    "air force research", "afrl", "defense advanced",
}

# Keywords in topic title/description that indicate relevance to our capabilities
_SBIR_RELEVANCE_TERMS = {
    "isr", "intelligence", "surveillance", "reconnaissance",
    "sensor fusion", "data fusion", "sigint", "elint",
    "directed energy", "laser", "high energy",
    "artificial intelligence", "machine learning", "autonomy", "autonomous",
    "targeting", "electronic warfare", "ew ", " ew,",
    "counter-uas", "counter uas", "c-uas",
}


def fetch_sbir_topics() -> list[dict[str, Any]]:
    """
    Fetch open DoD SBIR/STTR solicitation topics from SBIR.gov.

    NOTE: api.sbir.gov DNS does not resolve from GitHub Actions.
    Returns empty list until the correct public endpoint is confirmed.
    """
    log.info("SBIR.gov: skipped (API endpoint unresolvable — needs investigation)")
    return []

    # --- unreachable until correct SBIR endpoint is confirmed ---
    params = {
        "rows": 50,
        "program": "SBIR",
        "agency": "DOD",
    }

    try:
        log.info("SBIR.gov: fetching open DoD topics")
        resp = requests.get(
            _SBIR_API, params=params, timeout=20,
            headers={"User-Agent": "DefenseBDDigest/1.0"},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("SBIR.gov fetch failed: %s", exc)
        return []

    topics: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # API may return list directly or nested under a key
    records = data if isinstance(data, list) else data.get("solicitations", data.get("results", []))

    for record in records:
        # Extract fields — SBIR.gov field names vary slightly by endpoint version
        topic_id = str(
            record.get("solicitation_id") or record.get("id") or record.get("solicitation_number", "")
        )
        if not topic_id or topic_id in seen_ids:
            continue
        seen_ids.add(topic_id)

        agency = (record.get("agency") or record.get("agency_name") or "").lower()
        branch = (record.get("branch") or record.get("program_name") or "").lower()
        title = (record.get("solicitation_title") or record.get("title") or "").strip()
        description = (record.get("program_description") or record.get("description") or "")[:600].strip()
        open_date = record.get("open_date") or record.get("posted_date") or ""
        close_date = record.get("close_date") or record.get("due_date") or ""
        url = record.get("solicitation_url") or record.get("url") or f"https://www.sbir.gov/node/{topic_id}"

        # Filter: only agencies we care about
        agency_text = f"{agency} {branch}"
        if not any(t in agency_text for t in _SBIR_TARGET_AGENCIES):
            continue

        # Relevance filter: topic must touch our capabilities
        combined = f"{title} {description}".lower()
        if not any(t in combined for t in _SBIR_RELEVANCE_TERMS):
            continue

        topics.append(
            {
                "type": "opportunity",
                "source": "SBIR.gov",
                "title": f"[SBIR TOPIC] {title}" if title else f"[SBIR] {agency.upper()} Topic {topic_id}",
                "url": url,
                "agency": f"{record.get('agency', '')} {record.get('branch', '')}".strip(),
                "naics": "",
                "set_aside": "SBIR",
                "notice_type": "SBIR Topic",
                "solicitation_number": topic_id,
                "posted_date": open_date,
                "response_deadline": close_date,
                "description": description,
                "notice_id": topic_id,
            }
        )

    log.info("SBIR.gov total: %d relevant open topics found", len(topics))
    return topics
