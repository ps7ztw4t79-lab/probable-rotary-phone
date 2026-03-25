#!/usr/bin/env python3
"""
digest.py — Weekly Defense BD Digest
=====================================
Fetches defense news and contract intelligence, scores items with Claude AI,
and sends a curated HTML email digest.

Required:  ANTHROPIC_API_KEY
Email:     SMTP_USERNAME + SMTP_PASSWORD  (Gmail/Outlook app password)
           OR SENDGRID_API_KEY  (if you prefer SendGrid)
Optional:  SAM_GOV_API_KEY  (adds live contract opportunities on top of
           USASpending.gov award intelligence, which needs no key)

Usage:
    python digest.py              # run once immediately
    python digest.py --dry-run    # build email, print HTML to stdout, no send
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

# Load .env before importing modules that read env vars
load_dotenv()

from fetcher import fetch_all_news, fetch_sam_opportunities, fetch_usaspending_awards
from scorer import score_items
from email_builder import build_email, send_email

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


def _min_score(env_key: str, default: int) -> int:
    try:
        return int(os.getenv(env_key, str(default)))
    except ValueError:
        return default


def run(dry_run: bool = False) -> None:
    run_dt = datetime.now(timezone.utc)
    log.info("═" * 60)
    log.info("Defense BD Digest  —  %s", run_dt.strftime("%Y-%m-%d %H:%M UTC"))
    log.info("═" * 60)

    # ── 1. Fetch ──────────────────────────────────────────────────────────────
    log.info("Step 1/3 — Fetching content …")
    news_raw = fetch_all_news()

    # Use SAM.gov if key is available; always supplement with USASpending award intel
    sam_opps = fetch_sam_opportunities()
    usa_awards = fetch_usaspending_awards()
    opps_raw = sam_opps + usa_awards

    log.info(
        "  Raw news: %d  |  SAM.gov opps: %d  |  USASpending awards: %d",
        len(news_raw), len(sam_opps), len(usa_awards),
    )

    # ── 2. Score with Claude ──────────────────────────────────────────────────
    log.info("Step 2/3 — Scoring with Claude AI …")
    news_scored = score_items(news_raw, item_type="news")
    opps_scored = score_items(opps_raw, item_type="opportunity")

    min_news = _min_score("MIN_NEWS_SCORE", 45)
    min_opp = _min_score("MIN_OPPORTUNITY_SCORE", 55)

    news_filtered = sorted(
        [i for i in news_scored if i.get("relevance_score", 0) >= min_news],
        key=lambda x: x["relevance_score"],
        reverse=True,
    )[:20]

    opps_filtered = sorted(
        [i for i in opps_scored if i.get("relevance_score", 0) >= min_opp],
        key=lambda x: x["relevance_score"],
        reverse=True,
    )[:12]

    high_priority = sum(
        1
        for i in (news_filtered + opps_filtered)
        if i.get("relevance_score", 0) >= 70
    )
    log.info(
        "  After filter — news: %d  |  opportunities: %d  |  high-priority: %d",
        len(news_filtered),
        len(opps_filtered),
        high_priority,
    )

    # ── 3. Build & Send ───────────────────────────────────────────────────────
    log.info("Step 3/3 — Building email …")
    html = build_email(news_filtered, opps_filtered, run_dt)

    if dry_run:
        log.info("DRY RUN — printing HTML to stdout (not sending)")
        print(html)
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

    log.info("═" * 60)
    log.info("Done.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run the Defense BD Digest")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the email and print HTML to stdout without sending",
    )
    args = parser.parse_args()
    run(dry_run=args.dry_run)
