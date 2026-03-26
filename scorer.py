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
from pathlib import Path
from typing import Any

import anthropic
import yaml

log = logging.getLogger(__name__)

BATCH_SIZE = 15
_FEEDBACK_FILE = Path("feedback_log.json")


def _load_feedback_context() -> str:
    """
    Read feedback_log.json and return a formatted string for the scoring prompt.
    Items the team thumbs-up'd will be scored higher; thumbs-down'd lower.
    Returns empty string if no feedback yet.
    """
    if not _FEEDBACK_FILE.exists():
        return ""
    try:
        data = json.loads(_FEEDBACK_FILE.read_text())
        entries = data.get("feedback", [])
        if not entries:
            return ""
        ups = [e["title"] for e in entries if e.get("vote") == "up"][-20:]
        downs = [e["title"] for e in entries if e.get("vote") == "down"][-20:]
        lines = []
        if ups:
            lines.append("PREVIOUSLY RATED USEFUL BY THE TEAM (score similar items higher):")
            lines.extend(f"  + {t}" for t in ups)
        if downs:
            lines.append("PREVIOUSLY RATED NOT RELEVANT (score similar items lower/penalize):")
            lines.extend(f"  - {t}" for t in downs)
        return "\n".join(lines)
    except Exception as exc:
        log.debug("Could not load feedback log: %s", exc)
        return ""  # Items per Claude call — balances cost vs. latency

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
    locs = c.get("locations", {})
    exclude = c.get("exclude_categories", [])
    penalties = c.get("scoring_penalties", [])
    depth = c.get("technical_depth", {})
    programs = c.get("priority_programs", [])
    ideal = c.get("ideal_lead", "")

    de_depth = ', '.join(depth.get("directed_energy", []))
    isr_depth = ', '.join(depth.get("isr_exploitation", []))

    hw_depth = ', '.join(depth.get("hardware_engineering", []))
    ai_depth = ', '.join(depth.get("ai_ml", []))
    te_depth = ', '.join(depth.get("test_evaluation", []))

    base = f"""You are a senior BD analyst for a small defense subcontractor. Score items for actionable sales lead potential.
Only scores of 75+ will be shown to the team — calibrate accordingly. Be strict: a 75 must be genuinely actionable today.

COMPANY PROFILE
  Stage: Early-stage (~$5M revenue), Huntsville AL HQ, also staffing {', '.join(locs.get('expanding_to', [])[:2])}
  Core capabilities:
    - Directed Energy: {de_depth}
    - ISR / Multi-INT Exploitation: {isr_depth}
    - AI/ML & Edge: {ai_depth}
    - Hardware / FPGA / Signal Processing: {hw_depth}
    - T&E (HWIL, SIL, live-fire, flight test): {te_depth}
    - Systems Integration, SETA, MBSE, Digital Engineering, Program Management
  Contract vehicles: {', '.join(c.get('contract_vehicles', []))}
  Clearances: {', '.join(c.get('personnel_clearances', []))}
  Eligible set-asides: {', '.join(c['contract_focus']['eligible_set_asides'])}
  NOT eligible as prime: {', '.join(c['contract_focus']['ineligible_set_asides'][:4])} (flag for sub angle)
  Primary focus: subcontracting under primes; SB set-aside primes $1M–$10M; SBIR/OTA/BAA

IDEAL LEAD (calibrate 90+ against this):
  {ideal}

HIGHEST-PRIORITY PROGRAMS (these alone can push a score to 85+):
  {chr(10).join('  - ' + p for p in programs)}

NEVER RELEVANT — score 0:
  {', '.join(exclude)}

CAP THESE AT 50 even if keywords match:
  {chr(10).join('  - ' + p for p in penalties)}

PRE-SOLICITATION SIGNALS (score these HIGHER — arrive before GovWin sees them):
  - Sources Sought notices, RFIs, Industry Day announcements, BAA pre-proposals
  - Add +10 to the base score when the notice type is Sources Sought, RFI, or Industry Day
  - These are the exact signals this company needs to act on before the RFP drops

ACTIVE CUSTOMER RELATIONSHIPS (maximum priority):
  - Army G-2 ISR Task Force: ACTIVE WORK under IRT subcontract. Any recompete signal = 90+.
  - MSIC (Missile and Space Intelligence Center): CGO has direct relationships. Any MSIC opportunity = 85+.
  - MDA (Missile Defense Agency, Huntsville): CGO has direct relationships. Any MDA engineering/ISR = 85+.

PRIMARY PRIME (IRT is the established teaming path):
  - Intuitive Research and Technology (IRT / IRTC) is our primary teaming partner.
  - We are invited onto IRT pursuits PRE-AWARD — this is the most likely path to near-term work.
  - If IRT/IRTC wins a contract or appears in an award, flag it 85+ and recommend contacting IRT BD.
  - recommended_action should say "Contact IRT/IRTC BD" when IRT is the likely entry point.

SCORING RULES — be precise, not generous:
  90-100 → G-2 ISR Task Force recompete signal OR named priority program (TITAN, Linchpin,
            DE-MSHORAD, HEL-D) + Huntsville/AFC/DEVCOM + clear immediate action → act TODAY
  85-89  → MSIC or MDA opportunity (CGO relationships) OR IRT/IRTC award in a relevant area
            OR open SBIR topic from Army/DEVCOM/MDA touching AI/ISR/DE
  80-89  → Strong capability match (HEL lethality, fiber laser, SIGINT/ELINT, data fusion)
            at a target agency, with a specific procurement vehicle or award signal
  75-79  → Solid match to capability or growth area + target agency + actionable angle
            (teaming, SBIR, BAA response, subcontract pursuit)
  60-74  → Relevant intel but no clear immediate action; worth monitoring
  40-59  → Weak match or too early-stage to act on; background only
  <40    → Omit

DO NOT score 75+ if:
  - The item is about budget/policy without a specific program action
  - No clear next step exists for a company of this size and stage
  - The technology area is right but the agency is not in our target list
  - The opportunity is over $50M unrestricted prime (not accessible as a sub without a prime relationship)

IMPORTANT
  - PRIMARY recommended_action: "Contact IRT/IRTC BD" when IRT is the natural entry point
  - Secondary primes to name when IRT is not the fit: Northrop Grumman (DE), Leidos/Dynetics
    (Army ISR), Torch Technologies (AFC small biz), COLSA (Redstone support), Raytheon (HEL/EW),
    L3Harris (ISR), Boeing (AFC/Army aviation), Booz Allen (intel programs)
  - SBIR Phase I/II and OTA/BAA at DEVCOM, ARL, AFC, or MDA are HIGH value — AI SME on staff
  - DOJ/law enforcement ISR work: score 65-75, note IRT relationship is the access path
  - recommended_action must name a specific office, program, or prime — never generic
  - tags: 2-5 short labels (program name, tech area, agency, contract type)"""

    feedback_ctx = _load_feedback_context()
    if feedback_ctx:
        return base + f"\n\nHISTORICAL FEEDBACK FROM YOUR TEAM:\n{feedback_ctx}"
    return base


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


def rescore_top_items(
    items: list[dict[str, Any]],
    top_n: int = 10,
) -> list[dict[str, Any]]:
    """
    Take the already-scored top N items and run them through Claude Opus for
    deeper, more actionable BD analysis. Haiku scores are kept; only the
    rationale, recommended_action, and tags are upgraded.

    This adds one Opus API call but produces significantly richer lead narratives
    for the items that actually matter.
    """
    if not items:
        return items

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return items

    profile = _load_profile()
    c = profile["company"]
    top = sorted(items, key=lambda x: x.get("relevance_score", 0), reverse=True)[:top_n]
    rest = items[top_n:] if len(items) > top_n else []

    condensed = [
        {
            "index": i,
            "title": item.get("title", ""),
            "source": item.get("source", ""),
            "agency": item.get("agency", ""),
            "score": item.get("relevance_score", 0),
            "lead_type": item.get("lead_type", ""),
            "set_aside": item.get("set_aside", ""),
            "description": (item.get("description") or item.get("summary", ""))[:400],
            "url": item.get("url", ""),
        }
        for i, item in enumerate(top)
    ]

    prompt = f"""You are the BD director for a small defense subcontractor with these attributes:
  Capabilities: {', '.join(c['capabilities'][:6])}
  Contract vehicles: {', '.join(c.get('contract_vehicles', ['GSA Schedule', 'SeaPort-NxG']))}
  Clearances: {', '.join(c.get('personnel_clearances', ['Secret', 'TS/SCI']))}
  Target agencies: {', '.join(c['target_agencies'][:8])}
  Primary focus: subcontracting under large primes; small business set-aside primes as secondary

These are the week's TOP {len(top)} leads (already screened for relevance). For each:
1. Write a 2-3 sentence rationale explaining the specific BD angle for THIS company
2. Write a concrete recommended_action with a named office, specific person type to call,
   or exact teaming move (e.g. "Contact Leidos' NAVAIR division BD team — they hold the
   incumbent IDIQ and are likely to need a systems integration sub")
3. List 3-5 precise tags

Return JSON array only:
[{{"index": 0, "rationale": "...", "recommended_action": "...", "tags": [...]}}]

Items:
{json.dumps(condensed, indent=2)}"""

    try:
        client = anthropic.Anthropic(api_key=api_key, timeout=90.0)
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        upgrades = _parse_scores(response.content[0].text)
        upgrade_map = {u["index"]: u for u in upgrades}

        upgraded_top = []
        for i, item in enumerate(top):
            enriched = dict(item)
            if i in upgrade_map:
                u = upgrade_map[i]
                enriched["rationale"] = u.get("rationale", item.get("rationale", ""))
                enriched["recommended_action"] = u.get(
                    "recommended_action", item.get("recommended_action", "")
                )
                enriched["tags"] = u.get("tags", item.get("tags", []))
            upgraded_top.append(enriched)

        log.info("Opus deep-scored top %d items", len(upgraded_top))
        return upgraded_top + rest

    except Exception as exc:
        log.warning("Opus rescore failed, keeping Haiku results: %s", exc)
        return items


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
