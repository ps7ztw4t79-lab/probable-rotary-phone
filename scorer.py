"""
scorer.py — Use Claude to score and rank news articles and contract opportunities.

Each item receives:
  relevance_score    : 0-100 integer
  lead_type          : opportunity | teaming | award_intel | market_intel | program_news
  rationale          : 1-2 sentence explanation of why this matters to our company
  recommended_action : concrete next step (e.g., "Submit proposal by Apr 15")
  tags               : list of relevant capability/technology tags
"""

import json
import logging
import os
from typing import Any

import anthropic
import yaml

log = logging.getLogger(__name__)

BATCH_SIZE = 15  # Items per Claude call — balances cost vs. latency

_PROFILE: dict | None = None


def _load_profile() -> dict:
    global _PROFILE
    if _PROFILE is None:
        with open("company_profile.yaml") as f:
            _PROFILE = yaml.safe_load(f)
    return _PROFILE


def _build_system_prompt(profile: dict) -> str:
    c = profile["company"]
    kw = profile["keywords"]
    return f"""You are a senior business development analyst for a small defense contractor.
Your job is to evaluate defense news and government contract opportunities for sales lead potential.

COMPANY PROFILE
  Name: {c['name']}
  Description: {c['description']}
  Current capabilities: {', '.join(c['capabilities'])}
  Future growth areas: {', '.join(c['future_growth_areas'])}
  Target agencies: {', '.join(c['target_agencies'])}
  Eligible set-asides: {', '.join(c['contract_focus']['eligible_set_asides'])}
  INELIGIBLE set-asides (flag these): {', '.join(c['contract_focus']['ineligible_set_asides'])}
  Pursuit types: {', '.join(c['contract_focus']['pursuit_types'])}

HIGH-PRIORITY KEYWORDS: {', '.join(kw['high_priority'][:18])}
MEDIUM-PRIORITY KEYWORDS: {', '.join(kw['medium_priority'][:12])}

SCORING RULES
  Opportunities (SAM.gov):
    80-100 → Direct capability match + target agency + eligible set-aside → pursue NOW
    60-79  → Good match, worth tracking for an upcoming RFP or teaming approach
    40-59  → Partial match; potential subcontracting or teaming angle
    <40    → Low relevance; omit from digest

  News articles:
    80-100 → Hot lead signal: new program announcement, budget, or contract award
              in our target agencies/technologies that demands immediate BD action
    60-79  → Market intelligence that informs pipeline or reveals a teaming partner
    40-59  → Useful background on industry trends in our sectors
    <40    → Minimal relevance; omit from digest

IMPORTANT
  - If a set-aside is 8(a), HUBZone, WOSB, or EDWOSB, note in rationale that the
    company is not eligible as prime but may pursue as subcontractor.
  - recommended_action must be specific and actionable (not generic advice).
  - tags should be 2-5 short technology or capability labels."""


def _build_user_prompt(items: list[dict], item_type: str) -> str:
    condensed = []
    for i, item in enumerate(items):
        entry: dict[str, Any] = {
            "index": i,
            "title": item.get("title", ""),
            "source": item.get("source", ""),
        }
        if item_type == "opportunity":
            entry["agency"] = item.get("agency", "")
            entry["set_aside"] = item.get("set_aside", "")
            entry["naics"] = item.get("naics", "")
            entry["notice_type"] = item.get("notice_type", "")
            entry["response_deadline"] = item.get("response_deadline", "")
            entry["description"] = (item.get("description") or "")[:500]
        else:
            entry["summary"] = (item.get("summary") or "")[:400]
            entry["published"] = item.get("published", "")

        condensed.append(entry)

    return f"""Score the following {len(items)} {item_type} item(s).

Return a JSON array (no markdown, no commentary) with one object per item:
[
  {{
    "index": <int>,
    "relevance_score": <0-100>,
    "lead_type": "<opportunity|teaming|award_intel|market_intel|program_news>",
    "rationale": "<1-2 sentences>",
    "recommended_action": "<specific next step>",
    "tags": ["<tag1>", "<tag2>"]
  }},
  ...
]

Items:
{json.dumps(condensed, indent=2)}"""


def _parse_scores(raw: str) -> list[dict]:
    text = raw.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Drop first and last fence lines
        inner = [l for l in lines[1:] if not l.strip().startswith("```")]
        text = "\n".join(inner).strip()
    return json.loads(text)


def score_items(
    items: list[dict[str, Any]],
    item_type: str,  # "news" or "opportunity"
) -> list[dict[str, Any]]:
    """
    Score a list of news or opportunity dicts using Claude.
    Returns the same list with scoring fields added.
    """
    if not items:
        return []

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        log.error("ANTHROPIC_API_KEY not set — cannot score items")
        return _fallback_scores(items)

    profile = _load_profile()
    client = anthropic.Anthropic(api_key=api_key, timeout=60.0)
    system_prompt = _build_system_prompt(profile)

    scored: list[dict[str, Any]] = []

    for batch_start in range(0, len(items), BATCH_SIZE):
        batch = items[batch_start : batch_start + BATCH_SIZE]
        log.info(
            "Scoring %s batch %d-%d (%d items)",
            item_type,
            batch_start,
            batch_start + len(batch) - 1,
            len(batch),
        )

        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2500,
                system=system_prompt,
                messages=[{"role": "user", "content": _build_user_prompt(batch, item_type)}],
            )
            scores = _parse_scores(response.content[0].text)

            for score_data in scores:
                idx = score_data.get("index", 0)
                if 0 <= idx < len(batch):
                    enriched = dict(batch[idx])
                    enriched["relevance_score"] = int(score_data.get("relevance_score", 0))
                    enriched["lead_type"] = score_data.get("lead_type", "market_intel")
                    enriched["rationale"] = score_data.get("rationale", "")
                    enriched["recommended_action"] = score_data.get("recommended_action", "")
                    enriched["tags"] = score_data.get("tags", [])
                    scored.append(enriched)

        except Exception as exc:
            log.error("Scoring batch failed: %s", exc)
            scored.extend(_fallback_scores(batch))

    return scored


def _fallback_scores(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return items with zero scores when the API is unavailable."""
    result = []
    for item in items:
        copy = dict(item)
        copy.update(
            {
                "relevance_score": 0,
                "lead_type": "market_intel",
                "rationale": "(scoring unavailable)",
                "recommended_action": "",
                "tags": [],
            }
        )
        result.append(copy)
    return result
