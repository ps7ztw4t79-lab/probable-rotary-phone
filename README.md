# Defense BD Digest

A weekly email digest of curated defense contracting news and SAM.gov
contract opportunities, scored for relevance to your company using Claude AI
and delivered via SendGrid.

## What it does

Every week it:
1. Pulls the latest articles from **10+ defense RSS feeds** (Defense News,
   Breaking Defense, C4ISRNET, DoD News, AUSA, USNI, etc.)
2. Searches **SAM.gov** for new contract opportunities matching your
   capability keywords
3. Sends every item through **Claude AI**, which reads your company profile
   and returns a 0-100 relevance score, a rationale, and a specific
   recommended next action
4. Assembles a **formatted HTML email** sorted by score and sends it via
   **SendGrid**

---

## Quick start

### 1. Clone and install

```bash
git clone <repo-url>
cd probable-rotary-phone
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Copy and fill in your environment variables

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | Required | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | [console.anthropic.com](https://console.anthropic.com) |
| `SENDGRID_API_KEY` | **Yes** | [app.sendgrid.com](https://app.sendgrid.com) |
| `DIGEST_FROM_EMAIL` | **Yes** | Must be a verified SendGrid sender |
| `DIGEST_FROM_NAME` | No | Defaults to `Defense BD Digest` |
| `DIGEST_TO_EMAILS` | **Yes** | Comma-separated list of recipients |
| `SAM_GOV_API_KEY` | Recommended | Free at [sam.gov/profile/details](https://sam.gov/profile/details) — without this, no contract opportunities are included |
| `MIN_NEWS_SCORE` | No | Default `45`. Raise to tighten filter. |
| `MIN_OPPORTUNITY_SCORE` | No | Default `55`. |
| `LOOKBACK_DAYS` | No | Default `7` (weekly). |

### 3. Update your company profile

Edit `company_profile.yaml` — change at minimum:

- `company.name`
- `company.description`
- `company.eligible_set_asides` (remove SDVOSB/VOSB if you don't qualify)

The AI scorer reads this file directly, so keeping it accurate is the best
way to improve lead quality.

### 4. Test with a dry run

```bash
python digest.py --dry-run > preview.html
open preview.html   # macOS
xdg-open preview.html   # Linux
```

This builds the full email and writes HTML to `preview.html` without sending
anything.

### 5. Send it

```bash
python digest.py
```

---

## Scheduling (weekly automation)

### Linux / macOS — cron

Run every Monday at 7 AM:

```bash
crontab -e
```

Add:

```
0 7 * * 1 cd /path/to/probable-rotary-phone && /path/to/.venv/bin/python digest.py >> /var/log/defense_digest.log 2>&1
```

### Windows — Task Scheduler

Create a Basic Task → trigger Weekly (Monday) → action: start a program:

```
Program: C:\path\to\.venv\Scripts\python.exe
Arguments: C:\path\to\probable-rotary-phone\digest.py
Start in: C:\path\to\probable-rotary-phone
```

### Cloud (GitHub Actions)

Create `.github/workflows/digest.yml`:

```yaml
name: Weekly Defense Digest
on:
  schedule:
    - cron: '0 12 * * 1'   # Every Monday at 12:00 UTC
  workflow_dispatch:        # Allow manual trigger

jobs:
  digest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python digest.py
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          SENDGRID_API_KEY: ${{ secrets.SENDGRID_API_KEY }}
          DIGEST_FROM_EMAIL: ${{ secrets.DIGEST_FROM_EMAIL }}
          DIGEST_TO_EMAILS: ${{ secrets.DIGEST_TO_EMAILS }}
          SAM_GOV_API_KEY: ${{ secrets.SAM_GOV_API_KEY }}
```

---

## Tuning lead quality

### Raise or lower the score thresholds

In `.env`:
```
MIN_OPPORTUNITY_SCORE=65   # only show very strong contract matches
MIN_NEWS_SCORE=50          # only show clearly relevant articles
```

### Add keywords

In `company_profile.yaml`, add terms to `keywords.high_priority` or
`keywords.medium_priority`. The AI scorer receives the full keyword list
as context when evaluating each item.

### Add or remove news sources

In `company_profile.yaml`, edit the `rss_feeds` list. Any public RSS URL
works.

### Change SAM.gov search terms

In `company_profile.yaml`, edit `sam_search_terms`. Each term produces one
API call, so keep the list focused (10-15 terms is a good range).

---

## File overview

```
probable-rotary-phone/
├── digest.py              Main entry point
├── fetcher.py             RSS + SAM.gov data retrieval
├── scorer.py              Claude AI relevance scoring
├── email_builder.py       HTML email template + SendGrid delivery
├── company_profile.yaml   Your company config (edit this)
├── requirements.txt       Python dependencies
├── .env.example           Environment variable template
└── README.md
```

---

## Cost estimate

| Service | Typical weekly cost |
|---|---|
| Anthropic (Claude Opus) | ~$0.20–$0.60 depending on news volume |
| SendGrid | Free tier covers up to 100 emails/day |
| SAM.gov API | Free |
| RSS feeds | Free |

Total: **under $1/week** for a team of 5 recipients.
