#!/usr/bin/env python3
"""
digest.py — Weekly Defense BD Digest
=====================================
Fetches defense news and contract intelligence, scores items with Claude AI,
and sends a curated HTML email digest.

Required:  ANTHROPIC_API_KEY
Email:     SMTP_USERNAME + SMTP_PASSWORD  (Gmail/Outlook app password)
           OR SENDGRID_API_KEY  (if you prefer SendGrid)
Optional:  SAM_GOV_API_KEY  (adds live solicitations; USASpending + FPDS run
           automatically with no key)

Usage:
    python digest.py              # run once immediately
    python digest.py --dry-run    # build email, print HTML to stdout, no send
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import yaml
from dotenv import load_dotenv

# Load .env before importing modules that read env vars
load_dotenv()

from fetcher import fetch_all_news, fetch_sam_opportunities, fetch_usaspending_awards, fetch_fpds_awards, fetch_sbir_topics
from scorer import score_items, rescore_top_items, synthesize_news_trends
from email_builder import build_email, send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

_STATE_FILE = Path("seen_items.json")


# ── State (deduplication + trend tracking) ────────────────────────────────────

def _load_state() -> dict:
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_urls": [], "last_high_count": None, "last_run": ""}


def _save_state(state: dict) -> None:
    _STATE_FILE.write_text(json.dumps(state, indent=2))


def _filter_seen(items: list[dict], seen_urls: set) -> list[dict]:
    return [i for i in items if i.get("url", "") not in seen_urls]


# ── Keyword pre-filter ────────────────────────────────────────────────────────

def _keyword_prefilter(items: list[dict], max_items: int = 60) -> list[dict]:
    """
    Rank news items by keyword hits before sending to Claude.
    Keeps up to max_items most relevant-looking articles, cutting API cost
    and runtime significantly when RSS feeds return 100+ articles.
    """
    with open("company_profile.yaml") as f:
        profile = yaml.safe_load(f)
    kw = profile.get("keywords", {})
    high = [k.lower() for k in kw.get("high_priority", [])]
    medium = [k.lower() for k in kw.get("medium_priority", [])]

    exclude = [k.lower() for k in kw.get("exclude_terms", [])]

    def _score(item: dict) -> int:
        text = (item.get("title", "") + " " + item.get("summary", "")).lower()
        # Hard-exclude items matching irrelevant categories
        if any(k in text for k in exclude):
            return -1
        return sum(2 for k in high if k in text) + sum(1 for k in medium if k in text)

    ranked = sorted(items, key=_score, reverse=True)
    hits = [i for i in ranked if _score(i) > 0]
    no_hits = [i for i in ranked if _score(i) == 0]
    # Items scoring -1 are excluded entirely
    result = (hits + no_hits)[:max_items]
    log.info("  Keyword pre-filter: %d → %d items", len(items), len(result))
    return result


# ── Executive summary ─────────────────────────────────────────────────────────

def generate_executive_summary(
    top_news: list[dict],
    top_opps: list[dict],
    profile: dict,
    api_key: str,
) -> str:
    """
    Use Claude Opus to write a 3-5 sentence BD-focused executive briefing
    based on the week's top scored items.
    """
    c = profile["company"]
    all_top = sorted(
        top_news[:5] + top_opps[:5],
        key=lambda x: x.get("relevance_score", 0),
        reverse=True,
    )[:8]

    if not all_top:
        return ""

    items_text = "\n".join(
        f"[{i+1}] ({item.get('lead_type','').upper()}, score {item.get('relevance_score',0)}) "
        f"{item.get('title','')} — {item.get('rationale','')}"
        for i, item in enumerate(all_top)
    )

    prompt = f"""You are writing the Monday morning BD briefing for the leadership team of a small defense subcontractor.

Company profile:
  Capabilities: {', '.join(c['capabilities'][:5])}
  Contract vehicles: {', '.join(c.get('contract_vehicles', []))}
  Clearances: {', '.join(c.get('personnel_clearances', []))}
  Primary focus: subcontracting under large primes; growing into autonomous systems, EW, and MBSE

This week's top leads:
{items_text}

Write 3-5 sentences that:
1. Identify the single highest-priority action this week
2. Call out the best teaming or subcontracting angle
3. Note any notable program or budget trend that affects our pipeline

Be specific — name programs, agencies, and contract vehicles where possible.
Write in direct, active voice. No bullet points. No headers."""

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=350,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        log.warning("Executive summary generation failed: %s", exc)
        return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _min_score(env_key: str, default: int) -> int:
    try:
        return int(os.getenv(env_key, str(default)))
    except ValueError:
        return default


# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run: bool = False) -> None:
    run_dt = datetime.now(timezone.utc)
    log.info("═" * 60)
    log.info("Defense BD Digest  —  %s", run_dt.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("═" * 60)

    state = _load_state()
    seen_urls: set[str] = set(state.get("seen_urls", []))
    last_high_count: int | None = state.get("last_high_count")

    # ── 1. Fetch ──────────────────────────────────────────────────────────────
    log.info("Step 1/4 — Fetching content …")
    news_raw = fetch_all_news()
    if os.getenv("SKIP_DEDUP", "").lower() not in ("1", "true", "yes"):
        news_raw = _filter_seen(news_raw, seen_urls)

    sam_opps = fetch_sam_opportunities()
    usa_awards = fetch_usaspending_awards()
    fpds_awards = fetch_fpds_awards()
    sbir_topics = fetch_sbir_topics()
    opps_raw = sam_opps + usa_awards + fpds_awards + sbir_topics

    log.info(
        "  News: %d  |  SAM.gov: %d  |  USASpending: %d  |  FPDS: %d  |  SBIR: %d",
        len(news_raw), len(sam_opps), len(usa_awards), len(fpds_awards), len(sbir_topics),
    )

    # ── 2. Score with Haiku ───────────────────────────────────────────────────
    log.info("Step 2/4 — Scoring with Claude Haiku …")
    news_prefiltered = _keyword_prefilter(news_raw, max_items=60)
    # Synthesize cross-article trends before scoring — a cluster of signals
    # can score higher than any individual article
    trend_items = synthesize_news_trends(news_prefiltered)
    news_scored = score_items(news_prefiltered + trend_items, item_type="news")
    opps_scored = score_items(opps_raw, item_type="opportunity")

    # Haiku threshold — lenient, just culls obvious noise before Opus sees candidates
    haiku_news = _min_score("MIN_NEWS_SCORE", 50)
    haiku_opp = _min_score("MIN_OPPORTUNITY_SCORE", 50)
    # Final threshold — Opus enforces this bar; only items above this go in the email
    final_threshold = 75

    news_filtered = sorted(
        [i for i in news_scored if i.get("relevance_score", 0) >= haiku_news],
        key=lambda x: x["relevance_score"],
        reverse=True,
    )[:20]

    opps_filtered = sorted(
        [i for i in opps_scored if i.get("relevance_score", 0) >= haiku_opp],
        key=lambda x: x["relevance_score"],
        reverse=True,
    )[:12]

    # ── 3. Deep-score top items with Opus ─────────────────────────────────────
    log.info("Step 3/4 — Deep-scoring top leads with Claude Opus …")
    all_scored = news_filtered + opps_filtered
    all_rescored = rescore_top_items(all_scored, top_n=10)
    # Opus applies the strict 75 bar via revised scores — filter here
    news_final = [i for i in all_rescored
                  if i.get("type") == "news" and i.get("relevance_score", 0) >= final_threshold]
    opps_final = [i for i in all_rescored
                  if i.get("type") == "opportunity" and i.get("relevance_score", 0) >= final_threshold]

    high_count = sum(
        1 for i in all_rescored if i.get("relevance_score", 0) >= 70
    )
    trend_delta: int | None = (
        (high_count - last_high_count) if last_high_count is not None else None
    )
    log.info(
        "  News: %d  |  Opportunities: %d  |  High-priority: %d  |  Trend: %s",
        len(news_final),
        len(opps_final),
        high_count,
        f"{trend_delta:+d} vs last week" if trend_delta is not None else "first run",
    )

    # ── Quiet-day check ───────────────────────────────────────────────────────
    if not news_final and not opps_final:
        log.info("No items above threshold today — skipping send.")
        _update_and_save_state(state, news_raw, opps_raw, 0, run_dt)
        return

    # ── 4. Executive summary + email ──────────────────────────────────────────
    log.info("Step 4/4 — Building email …")
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    with open("company_profile.yaml") as f:
        profile = yaml.safe_load(f)

    exec_summary = generate_executive_summary(news_final, opps_final, profile, api_key)

    html = build_email(
        news_items=news_final,
        opportunities=opps_final,
        run_dt=run_dt,
        executive_summary=exec_summary,
        trend_delta=trend_delta,
    )

    if dry_run:
        log.info("DRY RUN — printing HTML to stdout (not sending)")
        print(html)
        # Still save state on dry runs so deduplication works
        _update_and_save_state(state, news_raw, opps_raw, high_count, run_dt)
        return

    log.info("Sending email …")
    try:
        send_email(html)
        log.info("Digest sent successfully.")
    except EnvironmentError as exc:
        log.error("Configuration error: %s", exc)
        log.error(
            "Set SMTP_USERNAME + SMTP_PASSWORD (Gmail/Outlook app password) "
            "or SENDGRID_API_KEY in your .env file."
        )
        sys.exit(1)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
        sys.exit(1)

    _update_and_save_state(state, news_raw, opps_raw, high_count, run_dt)
    log.info("═" * 60)
    log.info("Done.")


def _update_and_save_state(
    state: dict,
    news_items: list[dict],
    opp_items: list[dict],
    high_count: int,
    run_dt: datetime,
) -> None:
    existing = set(state.get("seen_urls", []))
    new_urls = {i["url"] for i in news_items + opp_items if i.get("url")}
    # Keep the most recent 2000 URLs to bound file size
    all_urls = list(existing | new_urls)[-2000:]
    state["seen_urls"] = all_urls
    state["last_high_count"] = high_count
    state["last_run"] = run_dt.strftime("%Y-%m-%d")
    _save_state(state)
    log.info("State saved (%d seen URLs)", len(all_urls))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Defense BD Digest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the email and print HTML to stdout without sending",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
