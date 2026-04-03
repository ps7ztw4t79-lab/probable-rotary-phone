"""
email_builder.py — Build the HTML daily digest and send it.

Delivery is automatic:
  • If SENDGRID_API_KEY is set  → send via SendGrid
  • Otherwise                   → send via SMTP (smtplib, no extra dependency)

For SMTP the simplest setup is a Gmail App Password:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USERNAME=you@gmail.com
  SMTP_PASSWORD=xxxx xxxx xxxx xxxx   ← 16-char App Password

Compatibility targets:
  • Outlook 2016/2019/365 (Word rendering engine)
  • Government webmail (OWA, Outlook Web)
  • Gmail / Apple Mail
  • Restricted HTML environments (plain-text fallback included)
"""

import copy
import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from urllib.parse import quote
from datetime import datetime
from typing import Any

from jinja2 import Environment, BaseLoader

try:
    import sendgrid
    from sendgrid.helpers.mail import Mail as SendGridMail, Content
    _SENDGRID_AVAILABLE = True
except ImportError:
    _SENDGRID_AVAILABLE = False

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

# Labels use only ASCII + safe HTML entities — no Unicode symbols or emoji
LEAD_TYPE_META: dict[str, dict] = {
    "opportunity":  {"label": "CONTRACT OPP",  "color": "#0d47a1"},
    "teaming":      {"label": "TEAMING",        "color": "#1b5e20"},
    "award_intel":  {"label": "AWARD INTEL",    "color": "#4a148c"},
    "market_intel": {"label": "MARKET INTEL",   "color": "#b45309"},
    "program_news": {"label": "PROGRAM NEWS",   "color": "#b91c1c"},
    "trend":        {"label": "TREND",          "color": "#0891b2"},
    "recompete":    {"label": "RECOMPETE",      "color": "#c2410c"},
}


def _score_color(score: int) -> str:
    if score >= 70:
        return "#15803d"   # green
    if score >= 50:
        return "#b45309"   # amber
    return "#6b7280"       # gray


def _score_label(score: int) -> str:
    if score >= 70:
        return "HIGH"
    if score >= 50:
        return "MED"
    return "LOW"


def _fmt_date(date_str: str) -> str:
    if not date_str:
        return ""
    clean = re.sub(r"[TZ]", " ", date_str[:19]).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            return datetime.strptime(clean.strip(), fmt).strftime("%b %d, %Y")
        except ValueError:
            continue
    return date_str[:10]


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text).strip()


def _feedback_urls(item: dict) -> tuple[str, str]:
    """Return (thumbs_up_url, thumbs_down_url) as mailto: links.
    Subject line uses [+]/[-] text markers — no emoji for compatibility.
    """
    to_email = os.getenv("DIGEST_TO_EMAILS", "").split(",")[0].strip()
    title = (item.get("title") or "")[:80]
    score = item.get("relevance_score", 0)

    def _url(marker: str) -> str:
        subject = f"digest-feedback: {marker} [{score}] {title}"
        return f"mailto:{to_email}?subject={quote(subject)}"

    return _url("[+]"), _url("[-]")


def _display_rationale(item: dict[str, Any]) -> str:
    """Return item rationale with a concise fallback for empty model output."""
    rationale = (item.get("rationale") or "").strip()
    if rationale:
        return rationale

    lead_type = (item.get("lead_type") or "").strip()
    if not lead_type and "days_remaining" in item:
        lead_type = "recompete"
    if lead_type == "opportunity":
        return "Rationale unavailable from scorer; review the notice details and fit for immediate pursuit."
    if lead_type == "recompete":
        return "Rationale unavailable from scorer; validate incumbent position, timing, and teaming path."
    return "Rationale unavailable from scorer; treat as watchlist intelligence pending deeper review."


# ──────────────────────────────────────────────────────────────────────────────
# Plain-text builder
# ──────────────────────────────────────────────────────────────────────────────

def build_plain_text(
    news_items: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    recompetes: list[dict[str, Any]] | None = None,
    run_dt: datetime | None = None,
    trend_delta: int | None = None,
    quiet_day: bool = False,
) -> str:
    """Generate a clean plain-text version of the digest."""
    recompetes = recompetes or []
    run_dt = run_dt or datetime.utcnow()

    high_count = sum(
        1 for i in (recompetes + opportunities + news_items)
        if i.get("relevance_score", 0) >= 70
    )
    lines: list[str] = []
    SEP = "=" * 62
    SEC = "-" * 62

    lines += [
        SEP,
        "DEFENSE BD DIGEST",
        f"Week of {run_dt.strftime('%B %d, %Y')}",
        f"Generated: {run_dt.strftime('%Y-%m-%d %H:%M UTC')}",
        SEP,
        f"High-Priority Leads: {high_count}  |  "
        f"Contract Opps: {len(opportunities)}  |  News Items: {len(news_items)}",
    ]
    if trend_delta is not None:
        direction = "up" if trend_delta > 0 else ("down" if trend_delta < 0 else "flat")
        lines.append(f"Trend vs last week: {direction} {abs(trend_delta) if trend_delta else ''}")
    lines.append("")

    if quiet_day:
        lines += [
            SEP,
            "QUIET DAY",
            SEP,
            "No defense news or contract opportunities scored above the",
            "relevance threshold today. Pipeline ran normally.",
            "",
        ]
        lines.append(SEP)
        lines.append("Defense BD Digest | Powered by Claude AI")
        return "\n".join(lines)

    # Contract opportunities
    # News
    if news_items:
        lines += [SEP, "DEFENSE NEWS & MARKET INTELLIGENCE", SEP, ""]
        for item in news_items:
            score = item.get("relevance_score", 0)
            meta = LEAD_TYPE_META.get(item.get("lead_type", ""), LEAD_TYPE_META["market_intel"])
            source_date = item.get("source", "")
            if item.get("published"):
                source_date += f" -- {_fmt_date(item['published'])}"
            lines.append(f"[{meta['label']}] Score: {score} ({_score_label(score)})  {source_date}")
            lines.append(item.get("title", "")[:100])
            lines.append(f"  Why we care: {_display_rationale(item)}")
            if item.get("recommended_action"):
                lines.append(f"  Action: {item['recommended_action']}")
            if item.get("tags"):
                lines.append(f"  Tags: {', '.join(item['tags'][:4])}")
            if item.get("url"):
                lines.append(f"  URL: {item['url']}")
            if item.get("constituent_titles"):
                lines.append("  Based on:")
                for title in item["constituent_titles"][:4]:
                    lines.append(f"    - {title[:90]}")
            lines.append("")

    if opportunities:
        lines += [SEP, "CONTRACT OPPORTUNITIES -- SAM.gov", SEP, ""]
        for opp in opportunities:
            score = opp.get("relevance_score", 0)
            meta = LEAD_TYPE_META.get(opp.get("lead_type", ""), LEAD_TYPE_META["opportunity"])
            lines.append(f"[{meta['label']}] Score: {score} ({_score_label(score)})")
            if opp.get("notice_type"):
                lines.append(f"Notice: {opp['notice_type']}")
            if opp.get("response_deadline"):
                lines.append(f"Due: {_fmt_date(opp['response_deadline'])}")
            lines.append(opp.get("title", "")[:100])
            meta_parts = []
            if opp.get("agency"):
                meta_parts.append(opp["agency"])
            if opp.get("set_aside"):
                meta_parts.append(opp["set_aside"])
            if meta_parts:
                lines.append("  " + " | ".join(meta_parts))
            lines.append(f"  Why we care: {_display_rationale(opp)}")
            if opp.get("recommended_action"):
                lines.append(f"  Action: {opp['recommended_action']}")
            if opp.get("tags"):
                lines.append(f"  Tags: {', '.join(opp['tags'][:4])}")
            if opp.get("url"):
                lines.append(f"  URL: {opp['url']}")
            lines.append("")

    # Recompetes
    if recompetes:
        lines += [SEP, "RECOMPETE WATCH -- 6-Month to 2-Year Horizon", SEP, ""]
        for rc in recompetes:
            score = rc.get("relevance_score", 0)
            lines.append(f"[RECOMPETE] Score: {score} ({_score_label(score)})")
            lines.append(rc.get("title", "")[:130])
            if rc.get("agency"):
                parts = [rc["agency"]]
                if rc.get("award_amount"):
                    parts.append(f"${rc['award_amount']:,.0f}")
                lines.append("  " + " | ".join(parts))
            if rc.get("end_date"):
                lines.append(f"  Expiration date: {_fmt_date(rc['end_date'])}")
            lines.append(f"  Why we care: {_display_rationale(rc)}")
            if rc.get("recommended_action"):
                lines.append(f"  Action: {rc['recommended_action']}")
            if rc.get("tags"):
                lines.append(f"  Tags: {', '.join(rc['tags'][:4])}")
            if rc.get("url"):
                lines.append(f"  URL: {rc['url']}")
            if rc.get("related_news"):
                lines.append("  Related news:")
                for headline in rc["related_news"]:
                    lines.append(f"    - {headline[:100]}")
            lines.append("")

    lines += [SEP, "Defense BD Digest | Powered by Claude AI | USASpending.gov | Defense RSS", SEP]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Jinja2 HTML Template
#
# Compatibility rules applied throughout:
#   - Width: 600px via HTML attribute (Outlook ignores style max-width)
#   - MSO conditional wrapper constrains width in Outlook Word renderer
#   - Accent bars: colored <td> column instead of border-left on <table>
#   - Tags/actions row: two-cell <table> instead of display:flex + gap
#   - Section icons: HTML entities only, no emoji code points
#   - Feedback links: [+] / [-] text, no emoji
# ──────────────────────────────────────────────────────────────────────────────

_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<meta http-equiv="X-UA-Compatible" content="IE=edge">
<title>Defense BD Digest</title>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:Arial,Helvetica,sans-serif;-webkit-text-size-adjust:100%;-ms-text-size-adjust:100%;">

<!-- ── Outer wrapper ── -->
<table width="100%" cellpadding="0" cellspacing="0" border="0" style="background:#f0f2f5;">
<tr><td align="center" style="padding:24px 12px;">

<!--[if mso]>
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"><tr><td>
<![endif]-->

<!-- ── 600px card ── -->
<table width="600" cellpadding="0" cellspacing="0" border="0" align="center"
       style="width:100%;max-width:600px;">

  <!-- ══ HEADER ══ -->
  <tr>
    <td style="background:#0f1e36;padding:26px 32px 20px;">
      <div style="color:#93c5fd;font-size:10px;font-weight:bold;letter-spacing:1.5px;
                  text-transform:uppercase;margin-bottom:4px;">Daily Intelligence Report</div>
      <div style="color:#ffffff;font-size:24px;font-weight:bold;line-height:1.2;
                  margin-bottom:2px;">Defense BD Digest</div>
      <div style="color:#94a3b8;font-size:12px;">{{ week_label }}</div>
      <!-- Stats row -->
      <table cellpadding="0" cellspacing="0" border="0" style="margin-top:16px;">
        <tr>
          <td style="padding-right:24px;">
            <div style="color:#38bdf8;font-size:26px;font-weight:bold;line-height:1;">{{ high_count }}</div>
            <div style="color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:.8px;">High-Priority</div>
            {% if trend_delta is not none %}
            <div style="font-size:10px;font-weight:bold;margin-top:2px;color:{% if trend_delta > 0 %}#34d399{% elif trend_delta < 0 %}#f87171{% else %}#94a3b8{% endif %};">
              {% if trend_delta > 0 %}+{{ trend_delta }}{% elif trend_delta < 0 %}-{{ trend_delta|abs }}{% else %}&#8212;{% endif %} vs last week
            </div>
            {% endif %}
          </td>
          <td style="padding-right:24px;">
            <div style="color:#34d399;font-size:26px;font-weight:bold;line-height:1;">{{ opp_count }}</div>
            <div style="color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:.8px;">Contract Opps</div>
          </td>
          <td>
            <div style="color:#f59e0b;font-size:26px;font-weight:bold;line-height:1;">{{ news_count }}</div>
            <div style="color:#94a3b8;font-size:10px;text-transform:uppercase;letter-spacing:.8px;">News Items</div>
          </td>
        </tr>
      </table>
    </td>
  </tr>

  <!-- ══ BODY ══ -->
  <tr>
    <td style="background:#ffffff;padding:0 32px 32px;">

      {% if news_items %}
      <!-- ── Defense News ── -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="padding-top:28px;padding-bottom:10px;border-bottom:2px solid #e2e8f0;">
            <span style="font-size:10px;font-weight:bold;color:#475569;text-transform:uppercase;
                         letter-spacing:1.2px;">&#9658; Defense News &amp; Market Intelligence</span>
          </td>
        </tr>
      </table>

      {% for item in news_items %}
      {% set meta = lead_type_meta.get(item.lead_type, lead_type_meta['market_intel']) %}
      {% set sc = score_color(item.relevance_score) %}
      <table width="100%" cellpadding="0" cellspacing="0" border="0"
             style="border:1px solid #e2e8f0;margin-top:12px;background:#fffdf7;">
        <tr>
          <!-- Accent bar -->
          <td width="4" style="background:{{ sc }};font-size:0;line-height:0;">&nbsp;</td>
          <td style="padding:12px 16px;">
            <!-- Badges + source -->
            <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:7px;">
              <tr>
                <td style="padding-right:6px;">
                  <span style="background:{{ meta.color }};color:#ffffff;font-size:9px;font-weight:bold;
                               padding:2px 8px;">{{ meta.label }}</span>
                </td>
                <td style="padding-right:6px;">
                  <span style="background:{{ sc }};color:#ffffff;font-size:9px;font-weight:bold;
                               padding:2px 8px;">{{ score_label(opp.relevance_score) }} {{ opp.relevance_score }}</span>
                </td>
                {% if opp.set_aside %}
                <td>
                  <span style="background:#f1f5f9;color:#475569;font-size:9px;
                               padding:2px 8px;">{{ opp.set_aside }}</span>
                </td>
                {% endif %}
              </tr>
            </table>
            <!-- Title -->
            <a href="{{ opp.url }}" style="color:#0f1e36;font-size:14px;font-weight:bold;
                       text-decoration:none;line-height:1.4;display:block;margin-bottom:5px;">
              {{ opp.title | truncate(120) }}
            </a>
            <!-- Meta -->
            <div style="color:#64748b;font-size:11px;margin-bottom:6px;">
              {% if opp.agency %}{{ opp.agency }}{% endif %}
              {% if opp.notice_type %} &nbsp;&bull;&nbsp; {{ opp.notice_type }}{% endif %}
              {% if opp.response_deadline %} &nbsp;&bull;&nbsp;
                <span style="color:#dc2626;font-weight:bold;">Due {{ fmt_date(opp.response_deadline) }}</span>
              {% endif %}
            </div>
            <div style="font-size:12px;color:#374151;margin-bottom:5px;">{{ display_rationale(opp) }}</div>
            {% if opp.recommended_action %}
            <div style="font-size:12px;color:#15803d;font-weight:bold;margin-bottom:6px;">
              &#8594; {{ opp.recommended_action }}
            </div>
            {% endif %}
            <!-- Tags + actions row -->
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td>
                  {% for tag in opp.tags[:4] %}
                  <span style="display:inline-block;background:#dbeafe;color:#1d4ed8;font-size:10px;
                               padding:2px 7px;margin-right:4px;">{{ tag }}</span>
                  {% endfor %}
                </td>
                <td align="right" style="white-space:nowrap;font-size:11px;">
                  <a href="{{ opp.url }}" style="color:#2563eb;text-decoration:none;font-weight:bold;">View</a>
                  &nbsp;&nbsp;
                  <a href="{{ opp._feedback_up | safe }}" style="color:#15803d;text-decoration:none;font-weight:bold;">[+]</a>
                  &nbsp;
                  <a href="{{ opp._feedback_down | safe }}" style="color:#9ca3af;text-decoration:none;">[&#8722;]</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
      {% endfor %}
      {% endif %}

      {% if opportunities %}
      <!-- ── Contract Opportunities ── -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="padding-top:{% if news_items %}24px{% else %}28px{% endif %};
                     padding-bottom:10px;border-bottom:2px solid #e2e8f0;">
            <span style="font-size:10px;font-weight:bold;color:#475569;text-transform:uppercase;
                         letter-spacing:1.2px;">&#9658; Contract Opportunities &mdash; SAM.gov</span>
          </td>
        </tr>
      </table>

      {% for opp in opportunities %}
      {% set meta = lead_type_meta.get(opp.lead_type, lead_type_meta['opportunity']) %}
      {% set sc = score_color(opp.relevance_score) %}
      <table width="100%" cellpadding="0" cellspacing="0" border="0"
             style="border:1px solid #e2e8f0;margin-top:14px;background:#fafbff;">
        <tr>
          <!-- Accent bar -->
          <td width="4" style="background:{{ sc }};font-size:0;line-height:0;">&nbsp;</td>
          <td style="padding:14px 16px;">
            <!-- Badges -->
            <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
              <tr>
                <td style="padding-right:6px;">
                  <span style="background:{{ meta.color }};color:#ffffff;font-size:9px;font-weight:bold;
                               padding:2px 8px;">{{ meta.label }}</span>
                </td>
                <td style="padding-right:10px;">
                  <span style="background:{{ sc }};color:#ffffff;font-size:9px;font-weight:bold;
                               padding:2px 8px;">{{ score_label(item.relevance_score) }} {{ item.relevance_score }}</span>
                </td>
                <td>
                  <span style="color:#94a3b8;font-size:11px;">
                    {{ item.source }}{% if item.published %} &mdash; {{ fmt_date(item.published) }}{% endif %}
                  </span>
                </td>
              </tr>
            </table>
            <!-- Headline -->
            <a href="{{ item.url }}" style="color:#0f1e36;font-size:14px;font-weight:bold;
                       text-decoration:none;line-height:1.4;display:block;margin-bottom:6px;">
              {{ item.title | truncate(130) }}
            </a>
            <div style="font-size:12px;color:#374151;margin-bottom:5px;">{{ display_rationale(item) }}</div>
            {% if item.recommended_action %}
            <div style="font-size:12px;color:#15803d;font-weight:bold;margin-bottom:6px;">
              &#8594; {{ item.recommended_action }}
            </div>
            {% endif %}
            <!-- Tags + actions row -->
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td>
                  {% for tag in item.tags[:4] %}
                  <span style="display:inline-block;background:#dcfce7;color:#166534;font-size:10px;
                               padding:2px 7px;margin-right:4px;">{{ tag }}</span>
                  {% endfor %}
                </td>
                <td align="right" style="white-space:nowrap;font-size:11px;">
                  <a href="{{ item.url }}" style="color:#2563eb;text-decoration:none;font-weight:bold;">Read</a>
                  &nbsp;&nbsp;
                  <a href="{{ item._feedback_up | safe }}" style="color:#15803d;text-decoration:none;font-weight:bold;">[+]</a>
                  &nbsp;
                  <a href="{{ item._feedback_down | safe }}" style="color:#9ca3af;text-decoration:none;">[&#8722;]</a>
                </td>
              </tr>
            </table>
            <!-- Constituent articles (trend items only) -->
            {% if item.constituent_titles %}
            <table width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:8px;">
              <tr>
                <td style="padding:7px 10px;background:#f1f5f9;">
                  <div style="font-size:10px;color:#64748b;font-weight:bold;margin-bottom:4px;">
                    BASED ON {{ item.constituent_titles | length }} ARTICLES:
                  </div>
                  {% for title, url in zip(item.constituent_titles, item.constituent_urls) %}
                  <div style="font-size:11px;color:#475569;margin-bottom:2px;">
                    &bull; <a href="{{ url }}" style="color:#2563eb;text-decoration:none;">{{ title | truncate(90) }}</a>
                  </div>
                  {% endfor %}
                </td>
              </tr>
            </table>
            {% endif %}
          </td>
        </tr>
      </table>
      {% endfor %}
      {% endif %}

      {% if recompetes %}
      <!-- ── Recompete Watch ── -->
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td style="padding-top:{% if news_items or opportunities %}24px{% else %}28px{% endif %};
                     padding-bottom:10px;border-bottom:2px solid #fed7aa;">
            <span style="font-size:10px;font-weight:bold;color:#c2410c;text-transform:uppercase;
                         letter-spacing:1.2px;">&#9658; Recompete Watch &mdash; 6-Month to 2-Year Horizon</span>
          </td>
        </tr>
      </table>

      {% for rc in recompetes %}
      {% set sc = score_color(rc.relevance_score) %}
      <table width="100%" cellpadding="0" cellspacing="0" border="0"
             style="border:1px solid #fed7aa;margin-top:14px;background:#fff7ed;">
        <tr>
          <!-- Accent bar -->
          <td width="4" style="background:#c2410c;font-size:0;line-height:0;">&nbsp;</td>
          <td style="padding:14px 16px;">
            <!-- Badges -->
            <table cellpadding="0" cellspacing="0" border="0" style="margin-bottom:8px;">
              <tr>
                <td style="padding-right:6px;">
                  <span style="background:#c2410c;color:#ffffff;font-size:9px;font-weight:bold;
                               padding:2px 8px;">RECOMPETE</span>
                </td>
                <td style="padding-right:10px;">
                  <span style="background:{{ sc }};color:#ffffff;font-size:9px;font-weight:bold;
                               padding:2px 8px;">{{ rc.relevance_score }}</span>
                </td>
                <td>
                  <span style="color:#92400e;font-size:11px;font-weight:bold;">
                    {{ rc.days_remaining }}d remaining
                  </span>
                </td>
              </tr>
            </table>
            <!-- Title: incumbent — short description — expiration -->
            <a href="{{ rc.url }}" style="color:#7c2d12;font-size:14px;font-weight:bold;
                       text-decoration:none;line-height:1.4;display:block;margin-bottom:5px;">
              {{ rc.title | truncate(130) }}
            </a>
            <!-- Meta -->
            <div style="color:#92400e;font-size:11px;margin-bottom:6px;">
              {% if rc.agency %}{{ rc.agency }}{% endif %}
              {% if rc.award_amount %} &nbsp;&bull;&nbsp; ${{ "{:,.0f}".format(rc.award_amount) }}{% endif %}
              {% if rc.end_date %} &nbsp;&bull;&nbsp; Expiration date: {{ fmt_date(rc.end_date) }}{% endif %}
            </div>
            <div style="font-size:12px;color:#374151;margin-bottom:5px;">{{ display_rationale(rc) }}</div>
            {% if rc.recommended_action %}
            <div style="font-size:12px;color:#15803d;font-weight:bold;margin-bottom:6px;">
              &#8594; {{ rc.recommended_action }}
            </div>
            {% endif %}
            {% if rc.related_news %}
            <table width="100%" cellpadding="0" cellspacing="0" border="0"
                   style="margin-bottom:8px;background:#ffedd5;">
              <tr><td style="padding:6px 10px;">
                <div style="font-size:9px;color:#92400e;font-weight:bold;
                            text-transform:uppercase;letter-spacing:.6px;margin-bottom:3px;">Related News</div>
                {% for headline in rc.related_news %}
                <div style="font-size:11px;color:#7c2d12;">&bull; {{ headline | truncate(100) }}</div>
                {% endfor %}
              </td></tr>
            </table>
            {% endif %}
            <!-- Tags + actions row -->
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td>
                  {% for tag in rc.tags[:4] %}
                  <span style="display:inline-block;background:#fee2e2;color:#991b1b;font-size:10px;
                               padding:2px 7px;margin-right:4px;">{{ tag }}</span>
                  {% endfor %}
                </td>
                <td align="right" style="white-space:nowrap;font-size:11px;">
                  <a href="{{ rc.url }}" style="color:#c2410c;text-decoration:none;font-weight:bold;">View</a>
                  &nbsp;&nbsp;
                  <a href="{{ rc._feedback_up | safe }}" style="color:#15803d;text-decoration:none;font-weight:bold;">[+]</a>
                  &nbsp;
                  <a href="{{ rc._feedback_down | safe }}" style="color:#9ca3af;text-decoration:none;">[&#8722;]</a>
                </td>
              </tr>
            </table>
          </td>
        </tr>
      </table>
      {% endfor %}
      {% endif %}

      {% if quiet_day %}
      <table width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
          <td align="center" style="padding:40px 0;">
            <div style="color:#0f1e36;font-size:15px;font-weight:bold;margin-bottom:8px;">[ Quiet Day ]</div>
            <div style="color:#64748b;font-size:13px;line-height:1.6;">
              No defense news or contract opportunities scored above the<br>
              relevance threshold today. Pipeline ran normally.
            </div>
          </td>
        </tr>
      </table>
      {% endif %}

    </td>
  </tr>

  <!-- ══ FOOTER ══ -->
  <tr>
    <td style="padding:14px 32px;text-align:center;">
      <div style="color:#94a3b8;font-size:10px;line-height:1.8;">
        Defense BD Digest &mdash; {{ run_date }}<br>
        Powered by Claude AI &bull; USASpending.gov &bull; Defense RSS feeds<br>
        Edit company_profile.yaml to tune relevance scoring.
      </div>
    </td>
  </tr>

</table>
<!-- end 600px card -->

<!--[if mso]>
</td></tr></table>
<![endif]-->

</td></tr>
</table>
</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def build_email(
    news_items: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    run_dt: datetime,
    recompetes: list[dict[str, Any]] | None = None,
    executive_summary: str = "",
    trend_delta: int | None = None,
    quiet_day: bool = False,
) -> str:
    """Render the Jinja2 HTML template and return the email body string."""
    def _with_feedback(items: list[dict]) -> list[dict]:
        result = []
        for item in items:
            enriched = copy.copy(item)
            up, down = _feedback_urls(item)
            enriched["_feedback_up"] = up
            enriched["_feedback_down"] = down
            result.append(enriched)
        return result

    recompetes = _with_feedback(recompetes or [])
    opportunities = _with_feedback(opportunities)
    news_items = _with_feedback(news_items)

    env = Environment(loader=BaseLoader(), autoescape=True)
    env.globals["score_color"] = _score_color
    env.globals["score_label"] = _score_label
    env.globals["fmt_date"] = _fmt_date
    env.globals["lead_type_meta"] = LEAD_TYPE_META
    env.globals["display_rationale"] = _display_rationale
    env.globals["zip"] = zip

    tmpl = env.from_string(_TEMPLATE)
    high_count = sum(
        1 for i in (recompetes + opportunities + news_items)
        if i.get("relevance_score", 0) >= 70
    )
    return tmpl.render(
        week_label=run_dt.strftime("Week of %B %d, %Y"),
        run_date=run_dt.strftime("%B %d, %Y %H:%M UTC"),
        opportunities=opportunities,
        news_items=news_items,
        high_count=high_count,
        opp_count=len(opportunities),
        news_count=len(news_items),
        recompetes=recompetes,
        executive_summary=executive_summary,
        trend_delta=trend_delta,
        quiet_day=quiet_day,
    )


def send_email(
    html: str,
    subject: str | None = None,
    plain_text: str | None = None,
) -> None:
    """
    Send the digest. Automatically selects the delivery method:
      - SendGrid if SENDGRID_API_KEY is set
      - SMTP otherwise (requires SMTP_USERNAME + SMTP_PASSWORD)

    plain_text: optional plain-text fallback; generated from html if omitted
    """
    from_email = os.getenv("DIGEST_FROM_EMAIL", "").strip()
    from_name = os.getenv("DIGEST_FROM_NAME", "Defense BD Digest").strip()
    to_raw = os.getenv("DIGEST_TO_EMAILS", "").strip()

    if not to_raw:
        raise EnvironmentError("DIGEST_TO_EMAILS is not set")

    to_list = [e.strip() for e in to_raw.split(",") if e.strip()]
    if not subject:
        subject = f"Defense BD Digest — {datetime.utcnow().strftime('%B %d, %Y')}"

    # Generate a basic plain-text fallback from HTML if none was provided
    if not plain_text:
        plain_text = re.sub(r"<[^>]+>", " ", html)
        plain_text = re.sub(r" {2,}", " ", plain_text).strip()

    sendgrid_key = os.getenv("SENDGRID_API_KEY", "").strip()
    if sendgrid_key and _SENDGRID_AVAILABLE:
        _send_sendgrid(html, plain_text, subject, from_email, from_name, to_list, sendgrid_key)
    else:
        _send_smtp(html, plain_text, subject, from_email, from_name, to_list)


def _send_sendgrid(
    html: str,
    plain_text: str,
    subject: str,
    from_email: str,
    from_name: str,
    to_list: list[str],
    api_key: str,
) -> None:
    if not from_email:
        raise EnvironmentError("DIGEST_FROM_EMAIL is required for SendGrid")
    sg = sendgrid.SendGridAPIClient(api_key=api_key)
    for recipient in to_list:
        message = SendGridMail(
            from_email=(from_email, from_name),
            to_emails=recipient,
            subject=subject,
        )
        message.content = [
            Content("text/plain", plain_text),
            Content("text/html", html),
        ]
        response = sg.send(message)
        log.info("SendGrid -> %s  status=%s", recipient, response.status_code)


def _send_smtp(
    html: str,
    plain_text: str,
    subject: str,
    from_email: str,
    from_name: str,
    to_list: list[str],
) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USERNAME", "").strip()
    smtp_pass = os.getenv("SMTP_PASSWORD", "").strip()

    if not smtp_user:
        raise EnvironmentError(
            "No email delivery configured. Set either SENDGRID_API_KEY "
            "or SMTP_USERNAME + SMTP_PASSWORD in your .env file."
        )
    if not smtp_pass:
        raise EnvironmentError("SMTP_PASSWORD is not set")

    sender_addr = from_email or smtp_user
    display_from = f"{from_name} <{sender_addr}>"

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        for recipient in to_list:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = display_from
            msg["To"] = recipient
            # text/plain must come first; clients render the last matching part
            msg.attach(MIMEText(plain_text, "plain", "utf-8"))
            msg.attach(MIMEText(html, "html", "utf-8"))
            server.send_message(msg)
            log.info("SMTP -> %s", recipient)
