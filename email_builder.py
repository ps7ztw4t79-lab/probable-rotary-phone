"""
email_builder.py — Build the HTML weekly digest and send it.

Delivery is automatic:
  • If SENDGRID_API_KEY is set  → send via SendGrid
  • Otherwise                   → send via SMTP (smtplib, no extra dependency)

For SMTP the simplest setup is a Gmail App Password:
  SMTP_HOST=smtp.gmail.com
  SMTP_PORT=587
  SMTP_USERNAME=you@gmail.com
  SMTP_PASSWORD=xxxx xxxx xxxx xxxx   ← 16-char App Password
"""

import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from typing import Any

from jinja2 import Environment, BaseLoader

try:
    import sendgrid
    from sendgrid.helpers.mail import Mail as SendGridMail
    _SENDGRID_AVAILABLE = True
except ImportError:
    _SENDGRID_AVAILABLE = False

log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

LEAD_TYPE_META: dict[str, dict] = {
    "opportunity":  {"label": "CONTRACT OPP",  "color": "#0d47a1"},
    "teaming":      {"label": "TEAMING",        "color": "#1b5e20"},
    "award_intel":  {"label": "AWARD INTEL",    "color": "#4a148c"},
    "market_intel": {"label": "MARKET INTEL",   "color": "#b45309"},
    "program_news": {"label": "PROGRAM NEWS",   "color": "#b91c1c"},
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
        return "MEDIUM"
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


# ──────────────────────────────────────────────────────────────────────────────
# Jinja2 HTML Template
# ──────────────────────────────────────────────────────────────────────────────

_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Defense BD Digest — {{ week_label }}</title>
</head>
<body style="margin:0;padding:0;background:#f0f2f5;font-family:'Segoe UI',Arial,sans-serif;">

<!-- ── Wrapper ── -->
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;">
<tr><td align="center" style="padding:28px 16px;">

  <!-- ── Card ── -->
  <table width="680" cellpadding="0" cellspacing="0" style="max-width:680px;width:100%;">

    <!-- ══ HEADER ══ -->
    <tr>
      <td style="background:#0f1e36;border-radius:10px 10px 0 0;padding:28px 36px 22px;">
        <div style="color:#93c5fd;font-size:11px;font-weight:700;letter-spacing:1.5px;
                    text-transform:uppercase;margin-bottom:4px;">Weekly Intelligence Report</div>
        <div style="color:#ffffff;font-size:26px;font-weight:800;line-height:1.2;
                    margin-bottom:2px;">Defense BD Digest</div>
        <div style="color:#94a3b8;font-size:13px;">{{ week_label }}</div>
        <!-- Stats bar -->
        <table style="margin-top:18px;" cellpadding="0" cellspacing="0">
          <tr>
            <td style="padding-right:22px;">
              <div style="color:#38bdf8;font-size:28px;font-weight:800;line-height:1;">{{ high_count }}</div>
              <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.8px;">High-Priority Leads</div>
              {% if trend_delta is not none %}
              <div style="color:{% if trend_delta > 0 %}#34d399{% elif trend_delta < 0 %}#f87171{% else %}#94a3b8{% endif %};font-size:11px;font-weight:700;margin-top:2px;">
                {% if trend_delta > 0 %}&#8593;{{ trend_delta }}{% elif trend_delta < 0 %}&#8595;{{ trend_delta|abs }}{% else %}&mdash;{% endif %} vs last week
              </div>
              {% endif %}
            </td>
            <td style="padding-right:22px;">
              <div style="color:#34d399;font-size:28px;font-weight:800;line-height:1;">{{ opp_count }}</div>
              <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.8px;">Contract Opps</div>
            </td>
            <td>
              <div style="color:#f59e0b;font-size:28px;font-weight:800;line-height:1;">{{ news_count }}</div>
              <div style="color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:.8px;">News Items</div>
            </td>
          </tr>
        </table>
      </td>
    </tr>

    <!-- ══ BODY ══ -->
    <tr>
      <td style="background:#ffffff;padding:0 36px 36px;border-radius:0 0 10px 10px;">

        {% if executive_summary %}
        <!-- ── Section: Executive Summary ── -->
        <div style="padding-top:24px;">
          <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;
                      letter-spacing:1.2px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;
                      margin-bottom:14px;">
            &#128203; BD Executive Briefing
          </div>
          <div style="background:#f0f7ff;border-left:4px solid #2563eb;border-radius:0 8px 8px 0;
                      padding:14px 18px;font-size:14px;color:#1e293b;line-height:1.65;">
            {{ executive_summary }}
          </div>
        </div>
        {% endif %}

        {% if opportunities %}
        <!-- ── Section: Contract Opportunities ── -->
        <div style="padding-top:30px;">
          <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;
                      letter-spacing:1.2px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;
                      margin-bottom:18px;">
            &#128196; Contract Opportunities — SAM.gov
          </div>

          {% for opp in opportunities %}
          {% set meta = lead_type_meta.get(opp.lead_type, lead_type_meta['opportunity']) %}
          {% set sc = score_color(opp.relevance_score) %}
          <!-- Opp card -->
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #e2e8f0;border-radius:8px;margin-bottom:14px;
                        background:#fafbff;border-left:4px solid {{ sc }};">
            <tr>
              <td style="padding:16px 20px;">
                <!-- badges row -->
                <div style="margin-bottom:8px;">
                  <span style="background:{{ meta.color }};color:#fff;font-size:10px;font-weight:700;
                               padding:2px 9px;border-radius:4px;letter-spacing:.6px;">
                    {{ meta.label }}
                  </span>
                  <span style="background:{{ sc }};color:#fff;font-size:10px;font-weight:700;
                               padding:2px 9px;border-radius:4px;margin-left:6px;letter-spacing:.6px;">
                    {{ score_label(opp.relevance_score) }} &nbsp;{{ opp.relevance_score }}/100
                  </span>
                  {% if opp.set_aside %}
                  <span style="background:#f1f5f9;color:#475569;font-size:10px;
                               padding:2px 9px;border-radius:4px;margin-left:6px;">
                    {{ opp.set_aside }}
                  </span>
                  {% endif %}
                </div>
                <!-- Title -->
                <a href="{{ opp.url }}" style="color:#0f1e36;font-size:15.5px;font-weight:700;
                           text-decoration:none;line-height:1.35;display:block;margin-bottom:6px;">
                  {{ opp.title | truncate(120) }}
                </a>
                <!-- Meta row -->
                <div style="color:#64748b;font-size:12px;margin-bottom:10px;">
                  {% if opp.agency %}<strong>Agency:</strong> {{ opp.agency }} &nbsp;|&nbsp;{% endif %}
                  {% if opp.naics %}<strong>NAICS:</strong> {{ opp.naics }} &nbsp;|&nbsp;{% endif %}
                  {% if opp.notice_type %}<strong>Type:</strong> {{ opp.notice_type }}{% endif %}
                </div>
                {% if opp.response_deadline %}
                <div style="color:#dc2626;font-size:12px;font-weight:600;margin-bottom:8px;">
                  &#9200; Response Due: {{ fmt_date(opp.response_deadline) }}
                </div>
                {% endif %}
                <!-- Rationale -->
                {% if opp.rationale %}
                <div style="background:#f0f7ff;border-left:3px solid {{ sc }};
                            padding:9px 13px;border-radius:0 5px 5px 0;
                            font-size:13px;color:#334155;margin-bottom:9px;line-height:1.5;">
                  <strong>Why it matters:</strong> {{ opp.rationale }}
                </div>
                {% endif %}
                <!-- Action -->
                {% if opp.recommended_action %}
                <div style="font-size:13px;color:#15803d;font-weight:600;margin-bottom:8px;">
                  &#8594; {{ opp.recommended_action }}
                </div>
                {% endif %}
                <!-- Tags -->
                {% if opp.tags %}
                <div>
                  {% for tag in opp.tags[:5] %}
                  <span style="background:#dbeafe;color:#1d4ed8;font-size:11px;padding:2px 8px;
                               border-radius:20px;margin-right:4px;">{{ tag }}</span>
                  {% endfor %}
                </div>
                {% endif %}
                <!-- Link -->
                <div style="margin-top:10px;">
                  <a href="{{ opp.url }}"
                     style="color:#2563eb;font-size:12px;text-decoration:none;">
                    View on SAM.gov &#8599;
                  </a>
                </div>
              </td>
            </tr>
          </table>
          {% endfor %}
        </div>
        {% endif %}

        {% if news_items %}
        <!-- ── Section: Defense News & Market Intel ── -->
        <div style="padding-top:{% if opportunities %}24px{% else %}30px{% endif %};">
          <div style="font-size:11px;font-weight:700;color:#64748b;text-transform:uppercase;
                      letter-spacing:1.2px;border-bottom:2px solid #e2e8f0;padding-bottom:8px;
                      margin-bottom:18px;">
            &#128240; Defense News &amp; Market Intelligence
          </div>

          {% for item in news_items %}
          {% set meta = lead_type_meta.get(item.lead_type, lead_type_meta['market_intel']) %}
          {% set sc = score_color(item.relevance_score) %}
          <table width="100%" cellpadding="0" cellspacing="0"
                 style="border:1px solid #e2e8f0;border-radius:8px;margin-bottom:12px;
                        background:#fffdf7;border-left:4px solid {{ sc }};">
            <tr>
              <td style="padding:14px 18px;">
                <!-- badges -->
                <div style="margin-bottom:7px;">
                  <span style="background:{{ meta.color }};color:#fff;font-size:10px;font-weight:700;
                               padding:2px 9px;border-radius:4px;letter-spacing:.6px;">
                    {{ meta.label }}
                  </span>
                  <span style="background:{{ sc }};color:#fff;font-size:10px;font-weight:700;
                               padding:2px 9px;border-radius:4px;margin-left:6px;">
                    {{ score_label(item.relevance_score) }} &nbsp;{{ item.relevance_score }}/100
                  </span>
                  <span style="color:#94a3b8;font-size:11px;margin-left:10px;">
                    {{ item.source }}{% if item.published %} &mdash; {{ fmt_date(item.published) }}{% endif %}
                  </span>
                </div>
                <!-- Headline -->
                <a href="{{ item.url }}" style="color:#0f1e36;font-size:14.5px;font-weight:700;
                           text-decoration:none;line-height:1.35;display:block;margin-bottom:7px;">
                  {{ item.title | truncate(130) }}
                </a>
                <!-- Rationale -->
                {% if item.rationale %}
                <div style="background:#fef9ec;border-left:3px solid {{ sc }};
                            padding:8px 12px;border-radius:0 5px 5px 0;
                            font-size:12.5px;color:#374151;margin-bottom:7px;line-height:1.5;">
                  {{ item.rationale }}
                </div>
                {% endif %}
                <!-- Action -->
                {% if item.recommended_action %}
                <div style="font-size:12.5px;color:#15803d;font-weight:600;margin-bottom:7px;">
                  &#8594; {{ item.recommended_action }}
                </div>
                {% endif %}
                <!-- Tags + link -->
                <div style="display:flex;flex-wrap:wrap;align-items:center;gap:4px;">
                  {% for tag in item.tags[:4] %}
                  <span style="background:#dcfce7;color:#166534;font-size:11px;padding:2px 8px;
                               border-radius:20px;">{{ tag }}</span>
                  {% endfor %}
                  <a href="{{ item.url }}"
                     style="color:#2563eb;font-size:11px;text-decoration:none;margin-left:auto;">
                    Read more &#8599;
                  </a>
                </div>
              </td>
            </tr>
          </table>
          {% endfor %}
        </div>
        {% endif %}

        {% if not opportunities and not news_items %}
        <div style="padding:40px 0;text-align:center;color:#94a3b8;font-size:14px;">
          No relevant items found this week. Try lowering MIN_NEWS_SCORE / MIN_OPPORTUNITY_SCORE in .env.
        </div>
        {% endif %}

      </td>
    </tr>

    <!-- ══ FOOTER ══ -->
    <tr>
      <td style="padding:16px 36px;text-align:center;">
        <div style="color:#94a3b8;font-size:11px;line-height:1.7;">
          Generated by Defense BD Digest &mdash; {{ run_date }}<br>
          Powered by Claude AI &bull; USASpending.gov &bull; Defense RSS feeds<br>
          Edit <code>company_profile.yaml</code> to tune relevance scoring.
        </div>
      </td>
    </tr>

  </table>
</td></tr>
</table>
</body>
</html>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def build_email(
    news_items: list[dict[str, Any]],
    opportunities: list[dict[str, Any]],
    run_dt: datetime,
    executive_summary: str = "",
    trend_delta: int | None = None,
) -> str:
    """Render the Jinja2 HTML template and return the email body string."""
    env = Environment(loader=BaseLoader(), autoescape=True)
    env.globals["score_color"] = _score_color
    env.globals["score_label"] = _score_label
    env.globals["fmt_date"] = _fmt_date
    env.globals["lead_type_meta"] = LEAD_TYPE_META

    tmpl = env.from_string(_TEMPLATE)
    high_count = sum(
        1 for i in (opportunities + news_items) if i.get("relevance_score", 0) >= 70
    )
    return tmpl.render(
        week_label=run_dt.strftime("Week of %B %d, %Y"),
        run_date=run_dt.strftime("%B %d, %Y %H:%M UTC"),
        opportunities=opportunities,
        news_items=news_items,
        high_count=high_count,
        opp_count=len(opportunities),
        news_count=len(news_items),
        executive_summary=executive_summary,
        trend_delta=trend_delta,
    )


def send_email(html: str, subject: str | None = None) -> None:
    """
    Send the digest. Automatically selects the delivery method:
      - SendGrid if SENDGRID_API_KEY is set
      - SMTP otherwise (requires SMTP_USERNAME + SMTP_PASSWORD)
    """
    from_email = os.getenv("DIGEST_FROM_EMAIL", "").strip()
    from_name = os.getenv("DIGEST_FROM_NAME", "Defense BD Digest").strip()
    to_raw = os.getenv("DIGEST_TO_EMAILS", "").strip()

    if not to_raw:
        raise EnvironmentError("DIGEST_TO_EMAILS is not set")

    to_list = [e.strip() for e in to_raw.split(",") if e.strip()]
    if not subject:
        subject = f"Defense BD Digest — {datetime.utcnow().strftime('%B %d, %Y')}"

    sendgrid_key = os.getenv("SENDGRID_API_KEY", "").strip()
    if sendgrid_key and _SENDGRID_AVAILABLE:
        _send_sendgrid(html, subject, from_email, from_name, to_list, sendgrid_key)
    else:
        _send_smtp(html, subject, from_email, from_name, to_list)


def _send_sendgrid(
    html: str,
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
            html_content=html,
        )
        response = sg.send(message)
        log.info("SendGrid → %s  status=%s", recipient, response.status_code)


def _send_smtp(
    html: str,
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
            msg.attach(MIMEText(html, "html", "utf-8"))
            server.send_message(msg)
            log.info("SMTP → %s", recipient)
