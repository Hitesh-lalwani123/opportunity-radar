# Opportunity Radar

A free daily system that finds business opportunities and emails you a scored report.

```
06:00 IST  GitHub Actions (free)          07:30 IST  Claude scheduled task (your Pro plan)
────────────────────────────────────      ─────────────────────────────────────────────
DEMAND  Hacker News · YouTube · Google    reads digests/latest.md (one file)
        News · Google Trends · Inc42 ·    reads 👍/👎 replies to tune itself
        YourStory · Entrackr · TechCrunch groups problems + macro forces → ideas → scores
        · ET Small Biz · Product Hunt  →  (analysis/PROMPT.md)
MACRO   PIB/cabinet/GST/RBI/SEBI news ·   ≤8 web searches (competitors, Reddit/X/LinkedIn,
        RBI feeds · ET/Mint/BusinessLine   official confirmation of the top policy item)
        economy · weather + air quality   emails the HTML report from your Gmail
        (15 cities) · USD/EUR→INR         incl. "Macro radar: going ahead / stalling"
filter, dedupe 14 days, rising terms,
7-day sector pulse → digests/latest.md
```

**Two kinds of signal.** *Demand* signals show what people want (complaints, "is there a tool…",
search spikes). *Macro* signals show which way the wind blows: a subsidy, a new mandatory rule, an
export ban, a duty hike, inflation, a heatwave next week, a falling rupee. Every idea in the email is
scored on both, so you see what can go ahead and what is likely to stall.

Cost: ₹0. GitHub Actions is free for public repositories, the YouTube API key is free,
and Claude's daily run uses your Pro plan's normal usage allowance.

---

## Setup (about 20 minutes, one time)

### 1. Create the GitHub repository
1. Sign in at github.com (create a free account if needed).
2. Click **+ → New repository**. Name: `opportunity-radar`. Choose **Public**
   (Actions minutes are unlimited for public repos; nothing private is stored here — recipients
   live only in the Claude task). Don't add a README. Click **Create repository**.
3. On the new empty repo page click **uploading an existing file**, drag in **everything inside**
   the unzipped `opportunity-radar` folder (including the hidden `.github` folder — on Windows,
   drag the folder contents from File Explorer with "Hidden items" turned on), then **Commit changes**.
   Check that `.github/workflows/daily.yml` appears in the repo.

### 2. Get a free YouTube API key (optional but recommended)
1. Go to console.cloud.google.com → create a project (any name). No billing needed.
2. **APIs & Services → Library** → search **YouTube Data API v3** → **Enable**.
3. **APIs & Services → Credentials → Create credentials → API key**. Copy it.
   Click **Edit API key → Restrict key → YouTube Data API v3** → Save.
4. In GitHub: your repo → **Settings → Secrets and variables → Actions → New repository secret**.
   Name `YOUTUBE_API_KEY`, paste the key, **Add secret**.

Without the key everything else still runs; the digest just shows "YouTube: skipped".

### 3. Turn on Actions and do a first run
1. Repo → **Settings → Actions → General → Workflow permissions** → choose
   **Read and write permissions** → Save.
2. Repo → **Actions** tab → enable workflows if asked → **Daily opportunity digest** → **Run workflow**.
3. After ~2 minutes a green tick appears and a new file `digests/latest.md` is in the repo. Open it to
   see what was collected.

### 4. Tell Claude
Send Claude the repo URL (e.g. `https://github.com/yourname/opportunity-radar`) and the recipient
emails. Claude creates the daily scheduled task from `analysis/TASK.md` (Gmail must be connected
in Claude). You can also create it yourself: paste TASK.md's prompt into a new scheduled task.

---

## Changing things

| Want to… | Edit |
|---|---|
| Add/remove news feeds, YouTube searches, news queries | `config.yaml` |
| Catch different "pain" phrases | `pain_phrases` in `config.yaml` |
| Policy/economy news searches, policy feeds | `macro.news_queries`, `macro.feeds` in `config.yaml` |
| Sectors and their keywords, tailwind/headwind words | `macro.sectors`, `macro.direction` in `config.yaml` |
| Weather cities and thresholds, air quality, currencies | `macro.weather`, `macro.air_quality`, `macro.fx` in `config.yaml` |
| Turn the whole macro layer off | `macro: enabled: false` in `config.yaml` |
| Bigger/smaller digest (more detail vs. less Claude usage) | `select:` and `max_text_chars` in `config.yaml` |
| Change your budget, skills, goals, scoring, email layout | `analysis/PROMPT.md` |
| Change collection time | `cron:` in `.github/workflows/daily.yml` (time is UTC; IST = UTC + 5:30) |
| Add/remove recipients or change send time | ask Claude to update the scheduled task |

Edit files directly on github.com (open file → pencil icon → Commit).

## Why no Reddit / X / LinkedIn / Instagram collector?
Reddit refuses requests from servers like GitHub's and closed its public feeds, X has no free API,
and LinkedIn and Instagram forbid scraping and pursue scrapers. Instead, Claude's daily run does a few
`site:reddit.com` / `site:x.com` / `site:linkedin.com` searches on the problems that are already rising
— a search engine has already indexed those public posts, so there is nothing to block and nothing
against the rules.

## Macro sources (all free, no key)
| Signal | Source |
|---|---|
| Cabinet, PIB, GST, duties, PLI, SEBI/IRDAI/TRAI rules, RBI, inflation, fuel, monsoon, tenders | Google News RSS searches (`site:pib.gov.in` included — PIB's own feed is Hindi-only and undated) |
| RBI press releases and notifications | rbi.org.in RSS |
| Economy & policy reporting | Economic Times Policy, BusinessLine Economy, Mint Economy RSS |
| Heat, cold, heavy rain for 15 cities and farm belts (14 days ahead) | Open-Meteo forecast API |
| Very poor / severe air (4 days ahead) | Open-Meteo air-quality API |
| USD/INR and EUR/INR, 30-day move | Frankfurter (European Central Bank reference rates) |

The collector tags each item with sectors and a ↑ tailwind / ↓ headwind hint using simple keyword
lists, and keeps a 7-day **sector pulse** (`data/sector_history.json`). The tags are only hints —
Claude makes the real call (an export ban hurts exporters but helps domestic buyers).

## Politeness built in
Public feeds and official APIs only, one request at a time with a pause (2.5 s for Google News), a clear User-Agent, and any
site answering "forbidden" or "too many requests" is skipped for the day — never retried around.

## Troubleshooting
- **No digest today:** Actions tab → open the failed run → read the red step. A single broken feed
  never stops the run; it only shows as "error" in the digest's Source status table.
- **Workflow stopped running:** GitHub pauses scheduled workflows in repos with no activity for 60 days.
  The daily commit normally prevents this; if it happens, Actions tab → **Enable workflow**.
- **Run tests locally:** `pip install -r requirements.txt && python -m pytest -q tests`
