"""
fetcher.py — Pull defense news (RSS), contract opportunities (SAM.gov, optional),
and contract award intelligence (USASpending.gov + FPDS, no API keys required).
"""

import json
import os
import re
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import feedparser
import requests
import yaml

log = logging.getLogger(__name__)

_PROFILE: dict | None = None

# ──────────────────────────────────────────────────────────────────────────────
# Caching helpers
# ──────────────────────────────────────────────────────────────────────────────

_CACHE_DIR = Path(".cache")
_CACHE_DIR.mkdir(exist_ok=True)


def _utc_day_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _load_json_file(path: Path) -> Any | None:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception as exc:
        log.warning("Failed reading cache file %s: %s", path, exc)
    return None


def _save_json_file(path: Path, data: Any) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        log.warning("Failed writing cache file %s: %s", path, exc)


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
                if published and published < cutoff:
                    continue

                summary = (
                    getattr(entry, "summary", None)
                    or getattr(entry, "description", None)
                    or ""
                )
                summary = re.sub(r"<[^>]+>", " ", summary).strip()[:600]

                article_url = entry.get("link", "")
                if article_url and article_url in seen_urls:
                    continue
                if article_url:
                    seen_urls.add(article_url)

                items.append(
                    {
                        "type": "news",
                        "source": name,
                        "title": entry.get("title", "").strip(),
                        "url": article_url,
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


def _sam_cache_file() -> Path:
    return _CACHE_DIR / f"sam_opps_{_utc_day_str()}.json"


def _sam_quota_file() -> Path:
    return _CACHE_DIR / f"sam_quota_exhausted_{_utc_day_str()}.json"


def fetch_sam_opportunities() -> list[dict[str, Any]]:
    """
    Fetch recent DoD/IC contract opportunities from SAM.gov.

    Behavior:
    - Uses daily cache to avoid repeat calls in the same UTC day
    - If quota is exhausted (429), writes a sentinel file and skips future calls
      that day
    - Saves partial results if one query succeeds before another hits quota
    """
    api_key = os.getenv("SAM_GOV_API_KEY", "").strip()
    if not api_key:
        log.warning("SAM_GOV_API_KEY not set — skipping SAM.gov fetch")
        return []

    cache_file = _sam_cache_file()
    quota_file = _sam_quota_file()

    if quota_file.exists():
        cached = _load_json_file(cache_file)
        if isinstance(cached, list):
            log.info("SAM.gov: quota exhausted earlier today; using cached results")
            return cached
        log.info("SAM.gov: quota exhausted earlier today; no cache available")
        return []

    cached = _load_json_file(cache_file)
    if isinstance(cached, list):
        log.info("SAM.gov: using cached results from %s", cache_file.name)
        return cached

    log.info("SAM.gov: API key present (%s...)", api_key[:6])
    posted_from = _cutoff_dt().strftime("%m/%d/%Y")
    posted_to = datetime.now(timezone.utc).strftime("%m/%d/%Y")

    all_opps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    dept_queries = [
        ("DEPT OF DEFENSE", "DoD"),
        ("OFFICE OF THE DIRECTOR OF NATIONAL INTELLIGENCE", "ODNI/IC"),
    ]

    for deptname, label in dept_queries:
        params = {
            "api_key": api_key,
            "deptname": deptname,
            "postedFrom": posted_from,
            "postedTo": posted_to,
            "ptype": "p,r,s,k,o",
            "limit": 100,
            "offset": 0,
        }

        log.info("SAM.gov: fetching %s solicitations %s -> %s", label, posted_from, posted_to)
        prepared = requests.Request("GET", _SAM_URL, params=params).prepare()
        log.info("SAM.gov request URL: %s", (prepared.url or "").replace(api_key, "REDACTED"))

        try:
            resp = requests.get(_SAM_URL, params=params, timeout=30)

            if resp.status_code == 429:
                body_preview = resp.text[:800].replace("\n", " ")
                log.warning("SAM.gov 429 (%s): daily quota exhausted — response: %s", label, body_preview)
                _save_json_file(
                    quota_file,
                    {
                        "status": "quota_exhausted",
                        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                        "label": label,
                    },
                )
                if all_opps:
                    _save_json_file(cache_file, all_opps)
                return all_opps

            if resp.status_code != 200:
                body_preview = resp.text[:800].replace("\n", " ")
                log.warning("SAM.gov HTTP %s (%s) — response: %s", resp.status_code, label, body_preview)
                if resp.status_code == 403:
                    log.warning(
                        "SAM.gov 403: likely using a key/server-IP combination not allowed for CI use"
                    )
                continue

            data = resp.json()
            records = data.get("opportunitiesData", [])
            log.info(
                "SAM.gov %s: %d records returned (totalRecords=%s)",
                label, len(records), data.get("totalRecords", "?"),
            )

            for opp in records:
                notice_id = opp.get("noticeId", "")
                if not notice_id or notice_id in seen_ids:
                    continue
                seen_ids.add(notice_id)

                agency = (
                    opp.get("fullParentPathName")
                    or opp.get("organizationHierarchy", "")
                    or opp.get("officeAddress", {}).get("city", "")
                )

                raw_desc = opp.get("description") or ""
                if raw_desc.startswith("/") or raw_desc.startswith("http"):
                    raw_desc = ""

                naics_desc = opp.get("naicsDescription", "")
                set_aside_desc = opp.get("typeOfSetAsideDescription", "")
                place = (opp.get("placeOfPerformance") or {}).get("city", {})
                place_str = place.get("name", "") if isinstance(place, dict) else ""

                description_parts = [p for p in [naics_desc, set_aside_desc, place_str, raw_desc] if p]
                description = "; ".join(description_parts)[:700]

                all_opps.append(
                    {
                        "type": "opportunity",
                        "source": "SAM.gov",
                        "title": (opp.get("title") or "").strip(),
                        "url": f"https://sam.gov/opp/{notice_id}/view",
                        "agency": agency,
                        "naics": opp.get("naicsCode", ""),
                        "set_aside": opp.get("typeOfSetAside", "") or set_aside_desc,
                        "notice_type": opp.get("type", ""),
                        "solicitation_number": opp.get("solicitationNumber", ""),
                        "posted_date": opp.get("postedDate", ""),
                        "response_deadline": opp.get("responseDeadLine", ""),
                        "description": description,
                        "notice_id": notice_id,
                    }
                )

        except Exception as exc:
            log.warning("SAM.gov fetch failed (%s): %s", label, exc)

    _save_json_file(cache_file, all_opps)
    log.info("SAM.gov total: %d unique opportunities", len(all_opps))
    return all_opps


# ──────────────────────────────────────────────────────────────────────────────
# USASpending.gov — Contract Award Intelligence (no API key required)
# ──────────────────────────────────────────────────────────────────────────────

_USASPENDING_URL = "https://api.usaspending.gov/api/v2/search/spending_by_award/"

_DEFAULT_NAICS = [
    "541330", "541715", "541512", "541519",
    "541690", "334511", "517410", "336411",
]


def fetch_usaspending_awards() -> list[dict[str, Any]]:
    """
    Fetch recent DoD contract awards from USASpending.gov.
    Completely free — no registration or API key required.
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
            "award_type_codes": ["A", "B", "C", "D"],
            # Narrow to DoD-funded awards server-side — avoids scoring irrelevant
            # civilian agency awards that happen to share our NAICS codes
            "agencies": [
                {"type": "funding", "tier": "toptier", "name": "Department of Defense"}
            ],
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
            "Start Date",
            "End Date",
            "Award Type",
        ],
        "sort": "Award Amount",
        "order": "desc",
        "limit": 50,
        "page": 1,
    }

    try:
        log.info("USASpending.gov: fetching recent awards (%s -> %s)", start_date, end_date)
        resp = requests.post(_USASPENDING_URL, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
    except requests.HTTPError as exc:
        body = exc.response.text[:1500].replace("\n", " ") if exc.response is not None else ""
        log.warning("USASpending.gov fetch failed: %s | body=%s", exc, body)
        return []
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
        perf_start = result.get("Start Date", "")
        perf_end = result.get("End Date", "")

        summary = f"{recipient} awarded ${amount:,.0f}" if amount else f"Award to {recipient}"
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
                "notice_type": result.get("Award Type", "Award"),
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

    Server-side filters: DoD agency, NAICS, award type — the API handles these.
    No time_period filter: we want active contracts regardless of award date,
    including multi-year IDIQs and long-duration awards placed 5+ years ago.
    The 180-730 day expiration horizon is applied client-side in Python since
    USASpending does not document a server-side end-date-range filter for this
    endpoint. Results are sorted ascending by End Date so the target window
    appears early and pagination exits once we've passed it.
    """
    profile = _load_profile()
    naics_codes = [
        str(n) for n in profile.get("company", {}).get("naics_codes", _DEFAULT_NAICS)
    ]

    now = datetime.now(timezone.utc)
    expire_min = now + timedelta(days=days_min)
    expire_max = now + timedelta(days=days_max)

    expiring: list[dict[str, Any]] = []

    for page in range(1, 11):
        payload = {
            "filters": {
                # No time_period — we want ALL active DoD contracts in scope,
                # not just recently awarded ones.
                "naics_codes": {"require": naics_codes},
                "award_type_codes": ["A", "B", "C", "D"],
                "agencies": [
                    {"type": "funding", "tier": "toptier", "name": "Department of Defense"}
                ],
            },
            "fields": [
                "Award ID",
                "Recipient Name",
                "Award Amount",
                "Description",
                "Awarding Agency",
                "Awarding Sub Agency",
                "Funding Agency",
                "Funding Sub Agency",
                "NAICS Code",
                "NAICS Description",
                "Start Date",
                "End Date",
                "Award Type",
            ],
            "sort": "End Date",
            "order": "asc",
            "limit": 100,
            "page": page,
        }

        try:
            log.info(
                "USASpending.gov: scanning recompetes page %d expiring %d-%d days out",
                page, days_min, days_max,
            )
            resp = requests.post(_USASPENDING_URL, json=payload, timeout=20)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except requests.HTTPError as exc:
            body = exc.response.text[:1500].replace("\n", " ") if exc.response is not None else ""
            log.warning(
                "USASpending expiring contracts failed on page %d: %s | body=%s",
                page, exc, body,
            )
            break
        except Exception as exc:
            log.warning("USASpending expiring contracts failed on page %d: %s", page, exc)
            break

        if not results:
            break

        page_reached_target_window = False
        page_moved_past_target_window = False

        for award in results:
            end_str = award.get("End Date", "")
            if not end_str:
                continue

            try:
                end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue

            if end_dt >= expire_min:
                page_reached_target_window = True

            if end_dt > expire_max:
                page_moved_past_target_window = True
                continue

            if not (expire_min <= end_dt <= expire_max):
                continue

            days_remaining = (end_dt - now).days
            recipient = award.get("Recipient Name", "Unknown")
            agency = (
                award.get("Awarding Sub Agency")
                or award.get("Awarding Agency")
                or award.get("Funding Sub Agency")
                or award.get("Funding Agency", "")
            )
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
                    "summary": (
                        f"Contract expiring in {days_remaining} days. "
                        f"Incumbent: {recipient}. "
                        f"Value: ${amount:,.0f}. "
                        f"{description}"
                    ),
                    "description": description,
                    "related_news": [],
                }
            )

        # Results sorted ascending by End Date — once a page both reaches and
        # passes the target window, later pages only move further out.
        if page_reached_target_window and page_moved_past_target_window:
            break

        if len(expiring) >= 25:
            break

    expiring.sort(key=lambda x: x["days_remaining"])
    log.info(
        "USASpending recompetes: %d found in %d-%d day horizon",
        len(expiring), days_min, days_max,
    )
    return expiring[:15]


# ──────────────────────────────────────────────────────────────────────────────
# FPDS — Federal Procurement Data System (no API key required)
# ──────────────────────────────────────────────────────────────────────────────

_FPDS_BASE = "https://www.fpds.gov/ezsearch/FEEDS/ATOM?FEEDNAME=AWARD&q="

_FPDS_QUERIES = [
    "directed energy",
    "high energy laser",
    "ISR exploitation",
    "sensor fusion",
    "TITAN",
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
                        "agency": "",
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

_SBIR_TARGET_AGENCIES = {
    "army", "darpa", "devcom", "mda", "missile defense",
    "air force research", "afrl", "defense advanced",
}

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

    records = data if isinstance(data, list) else data.get("solicitations", data.get("results", []))

    for record in records:
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

        agency_text = f"{agency} {branch}"
        if not any(t in agency_text for t in _SBIR_TARGET_AGENCIES):
            continue

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
