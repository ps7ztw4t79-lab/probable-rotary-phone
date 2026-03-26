#!/usr/bin/env python3
"""
process_feedback.py — Parse a digest-feedback GitHub issue and append to feedback_log.json.

Expected issue title format:
  digest-feedback: 👍 [85] Some Article Title Here
  digest-feedback: 👎 [72] Some Article Title Here

Reads from env vars:
  ISSUE_TITLE  — the full issue title string
  ISSUE_BODY   — the full issue body (contains Vote:, Score:, Source:, URL:)
  ISSUE_NUMBER — the GitHub issue number
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FEEDBACK_FILE = Path("feedback_log.json")


def parse_vote_from_title(title: str) -> str | None:
    """Extract 'up' or 'down' from the emoji in the issue title."""
    if "👍" in title:
        return "up"
    if "👎" in title:
        return "down"
    # Also accept text fallbacks
    title_lower = title.lower()
    if "thumbs_up" in title_lower or "thumb_up" in title_lower:
        return "up"
    if "thumbs_down" in title_lower or "thumb_down" in title_lower:
        return "down"
    return None


def parse_score_from_title(title: str) -> int:
    """Extract the score integer from [85] in the title."""
    m = re.search(r"\[(\d+)\]", title)
    return int(m.group(1)) if m else 0


def parse_item_title(title: str) -> str:
    """Extract the article title from the issue title after the score bracket."""
    # Format: "digest-feedback: 👍 [85] The actual article title"
    m = re.search(r"\[\d+\]\s+(.+)$", title)
    return m.group(1).strip() if m else title


def parse_body_field(body: str, field: str) -> str:
    """Extract a field value from issue body lines like 'Field: value'."""
    for line in body.splitlines():
        if line.startswith(f"{field}:"):
            return line[len(field) + 1:].strip()
    return ""


def main() -> None:
    issue_title = os.getenv("ISSUE_TITLE", "")
    issue_body = os.getenv("ISSUE_BODY", "")
    issue_number = os.getenv("ISSUE_NUMBER", "")

    if not issue_title:
        print("ERROR: ISSUE_TITLE not set", file=sys.stderr)
        sys.exit(1)

    vote = parse_vote_from_title(issue_title)
    if vote is None:
        print(f"Could not determine vote from title: {issue_title!r} — skipping", file=sys.stderr)
        sys.exit(0)

    score = parse_score_from_title(issue_title)
    item_title = parse_item_title(issue_title)
    source = parse_body_field(issue_body, "Source")
    url = parse_body_field(issue_body, "URL")

    entry = {
        "vote": vote,
        "title": item_title,
        "score": score,
        "source": source,
        "url": url,
        "issue": int(issue_number) if issue_number.isdigit() else None,
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # Load existing log
    if FEEDBACK_FILE.exists():
        try:
            data = json.loads(FEEDBACK_FILE.read_text())
        except Exception:
            data = {"feedback": []}
    else:
        data = {"feedback": []}

    data["feedback"].append(entry)

    FEEDBACK_FILE.write_text(json.dumps(data, indent=2))
    print(f"Recorded {vote} vote for: {item_title!r} (score {score})")


if __name__ == "__main__":
    main()
