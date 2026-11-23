#!/usr/bin/env python3
"""Opportunity Radar collector.

Pulls public, free sources (Hacker News, YouTube Data API, Google News RSS,
Google Trends RSS, startup-media RSS, Product Hunt RSS), keeps posts that
look like unmet needs or fast-moving trends, removes anything already sent
recently, finds terms that are rising versus the last 7 days, and writes a
compact Markdown digest for Claude to analyse.

A second "macro" layer (radar/macro.py) adds government policy, economy,
weather, air-quality and currency signals plus a 7-day sector pulse, so the
analysis can tell which ideas have the wind behind them and which don't.

Politeness rules (deliberate): official APIs and public feeds only, one
request at a time with a pause, a clear User-Agent, and a source that answers
403/429 is skipped for the day rather than retried around.

Usage:
    python radar/collect.py                # normal daily run
    python radar/collect.py --fixtures DIR  # offline run from saved JSON/XML (tests)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import macro  # noqa: E402
from common import Fetcher, make_signal, parse_feed, short, title_key, url_key  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent
MACRO_SOURCES = {"policy_news", "policy_rss", "weather", "air", "fx"}

STOPWORDS = set("""
a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having
he her here hers him his how i if in into is it its itself just let me more most my no nor not now of off
on once only or other our ours out over own same she should so some such than that the their theirs them
then there these they this those through to too under until up very was we were what when where which while
who whom why will with would you your yours new says said one two get got make like via amp us use using
year years day days today week month time people know think need want going way best top first last back
still really much many even well good great also could video watch subscribe channel shorts live news
https http www com rs crore lakh percent report reports says india indian 2025 2026 2027
""".split())

WORD_RE = re.compile(r"[a-z][a-z0-9+\-']{2,}")


# --------------------------------------------------------------------------- collectors
def collect_hn(cfg, f: Fetcher, since: int):
    base = "https://hn.algolia.com/api/v1/search_by_date"
    out = []
    jobs = [(tag, {"tags": tag, "numericFilters": f"created_at_i>{since}", "hitsPerPage": 100}, f"hn_{tag}.json") for tag in cfg["tags"]]
    for i, phrase in enumerate(cfg.get("comment_phrases", [])):
        jobs.append(("comment", {"query": f'"{phrase}"', "tags": "comment", "numericFilters": f"created_at_i>{since}", "hitsPerPage": 30}, f"hn_comment_{i}.json"))
    for kind, params, fx in jobs:
        raw = f.get(base, params, fixture=fx)
        if not raw:
            continue
        for h in json.loads(raw).get("hits", []):
            oid = h.get("objectID")
            if kind == "comment":
                title, text = h.get("story_title") or "HN comment", h.get("comment_text")
                eng, label = 0, "comment"
            else:
                title, text = h.get("title"), h.get("story_text") or ""
                eng = (h.get("points") or 0) + 2 * (h.get("num_comments") or 0)
                label = f"{h.get('points') or 0} pts · {h.get('num_comments') or 0} comments"
            out.append(make_signal("hn", title, text, f"https://news.ycombinator.com/item?id={oid}", h.get("created_at_i"), eng, label, f"hn/{kind}"))
    return out


def collect_youtube(cfg, f: Fetcher, since: int, key: str | None):
    if not key and f.fixtures is None:
        return [], "skipped (no YOUTUBE_API_KEY secret)"
    api = "https://www.googleapis.com/youtube/v3"
    after = dt.datetime.fromtimestamp(since, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ids: list[str] = []
    for i, s in enumerate(cfg["searches"][: cfg["max_searches"]]):
        raw = f.get(f"{api}/search", {"part": "snippet", "q": s["q"], "type": "video", "order": "viewCount",
                                      "publishedAfter": after, "regionCode": s.get("region", "IN"),
                                      "relevanceLanguage": "en", "maxResults": 15, "key": key}, fixture=f"yt_search_{i}.json")
        if raw:
            ids += [it["id"]["videoId"] for it in json.loads(raw).get("items", []) if it.get("id", {}).get("videoId")]
    ids = list(dict.fromkeys(ids))
    videos = []
    for start in range(0, len(ids), 50):
        raw = f.get(f"{api}/videos", {"part": "snippet,statistics", "id": ",".join(ids[start:start + 50]), "key": key},
                    fixture=f"yt_videos_{start // 50}.json")
        if raw:
            videos += json.loads(raw).get("items", [])
    out = []
    for v in videos:
        st, sn = v.get("statistics", {}), v.get("snippet", {})
        views, likes, comments = int(st.get("viewCount", 0)), int(st.get("likeCount", 0)), int(st.get("commentCount", 0))
        ts = dt.datetime.fromisoformat(sn.get("publishedAt", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
        out.append(make_signal("youtube_video", sn.get("title"), sn.get("description", "")[:400],
                               f"https://www.youtube.com/watch?v={v['id']}", ts, views // 100 + likes + comments,
                               f"{views:,} views · {comments:,} comments", "youtube/video"))
    top = sorted(videos, key=lambda v: int(v.get("statistics", {}).get("commentCount", 0)), reverse=True)[: cfg["comment_videos"]]
    for v in top:
        raw = f.get(f"{api}/commentThreads", {"part": "snippet", "videoId": v["id"], "order": "relevance",
                                              "maxResults": cfg["comments_per_video"], "textFormat": "plainText", "key": key},
                    fixture=f"yt_comments_{v['id']}.json")
        if not raw:
            continue
        vt = v.get("snippet", {}).get("title", "")
        for c in json.loads(raw).get("items", []):
            s = c["snippet"]["topLevelComment"]["snippet"]
            ts = dt.datetime.fromisoformat(s.get("publishedAt", "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp()
            out.append(make_signal("youtube_comment", f"Comment on: {vt}", s.get("textDisplay"),
                                   f"https://www.youtube.com/watch?v={v['id']}&lc={c['id']}", ts,
                                   int(s.get("likeCount", 0)) + 3 * int(c["snippet"].get("totalReplyCount", 0)),
                                   f"{s.get('likeCount', 0)} likes", "youtube/comment"))
    return out, f"ok ({len(ids)} videos)"


def collect_google_news(cfg, f: Fetcher, since: int):
    out = []
    for ei, ed in enumerate(cfg["editions"]):
        for qi, q in enumerate(cfg["queries"]):
            url = "https://news.google.com/rss/search"
            raw = f.get(url, {"q": f"{q} when:1d", **ed}, fixture=f"gnews_{ei}_{qi}.xml")
            if raw:
                out += parse_feed(raw, "google_news", f"news/{ed['gl']}", since)
    return out


def collect_trends(cfg, f: Fetcher, since: int):
    out = []
    for geo in cfg["geos"]:
        raw = f.get("https://trends.google.com/trending/rss", {"geo": geo}, fixture=f"trends_{geo}.xml")
        if raw:
            out += parse_feed(raw, "google_trends", f"trends/{geo}", since, traffic=True)
    return out


def collect_rss(cfg, f: Fetcher, since: int):
    out = []
    for i, feed in enumerate(cfg["feeds"]):
        raw = f.get(feed["url"], fixture=f"rss_{i}.xml")
        if raw:
            out += parse_feed(raw, feed.get("source", "rss"), feed["name"], since)
    return out


# --------------------------------------------------------------------------- scoring
def pain_hits(sig, phrases):
    blob = f"{sig['title']} {sig['text']}".lower()
    return [p for p in phrases if p in blob]


def score(sig, cfg, now):
    hits = pain_hits(sig, cfg["pain_phrases"])
    sig["pain"] = hits[:3]
    w = cfg["source_weights"].get(sig["source"], 1.0)
    age_h = max(0, (now - sig["ts"]) / 3600)
    recency = max(0.0, 1 - age_h / cfg["window_hours"])
    sig["score"] = round(4 * min(len(hits), 3) + w * math.log10(1 + sig["engagement"]) * 2 + recency, 3)
    return sig


def terms(sig):
    title = "" if sig["source"] == "youtube_comment" else sig["title"]   # comment titles repeat the video title
    words = [w.strip("'-") for w in WORD_RE.findall(f"{title} {sig['text'][:300]}".lower())]
    words = [w for w in words if w not in STOPWORDS and not w.isdigit() and len(w) > 2]
    grams = set(words) | {f"{a} {b}" for a, b in zip(words, words[1:])}
    return grams


def rising_terms(today: Counter, history: dict, today_str: str, limit=25):
    prior = [d for d in sorted(history) if d < today_str][-7:]
    if len(prior) < 1:
        return []          # nothing to compare against on the very first run
    rows = []
    for term, n in today.items():
        if n < 3:
            continue
        avg = sum(history[d].get(term, 0) for d in prior) / max(1, len(prior))
        ratio = n / (avg + 1)
        if ratio >= 2:
            rows.append((term, n, round(avg, 1), ratio))
    rows.sort(key=lambda r: (r[3], r[1]), reverse=True)
    # drop unigrams already covered by a bigram in the list
    chosen, covered = [], set()
    for r in rows:
        if r[0] in covered:
            continue
        chosen.append(r)
        covered.update(r[0].split())
        if len(chosen) >= limit:
            break
    return chosen


def select(signals, cfg):
    sel = cfg["select"]
    per_source = Counter()
    pains, trends = [], []
    for s in sorted(signals, key=lambda s: s["score"], reverse=True):
        if per_source[s["source"]] >= sel["max_per_source"]:
            continue
        bucket = pains if s["pain"] else trends
        cap = sel["pain_signals"] if s["pain"] else sel["trend_signals"]
        if s["source"] == "youtube_comment" and not s["pain"]:
            continue               # plain comments without a need are noise
        if len(bucket) < cap:
            bucket.append(s)
            per_source[s["source"]] += 1
    return pains, trends


# --------------------------------------------------------------------------- output
def render(date_str, pains, trends, rising, status, counts, cfg, macro_view=None):
    lim = cfg["max_text_chars"]
    L = [f"# Opportunity Radar digest — {date_str}", "",
         f"Generated {dt.datetime.now(dt.timezone.utc):%Y-%m-%d %H:%M} UTC · window {cfg['window_hours']}h · "
         f"collected {counts['raw']} → {counts['fresh']} new after dedupe → {len(pains) + len(trends)} selected", "",
         "## Source status", "", "| source | items | status |", "|---|---|---|"]
    for name, (n, st) in status.items():
        L.append(f"| {name} | {n} | {st} |")
    L += ["", "## Rising terms (today's mentions vs 7-day daily average)", ""]
    L += [f"- **{t}** — {n} today (avg {a})" for t, n, a, _ in rising] or ["- (not enough history yet — builds up over the first week)"]

    def block(prefix, items):
        rows = []
        for i, s in enumerate(items, 1):
            meta = " · ".join(x for x in [s["origin"], s["label"], ("pain: " + ", ".join(s["pain"])) if s["pain"] else ""] if x)
            body = short(s["text"], lim)
            rows.append(f"**[{prefix}{i}]** {s['title']}  \n_{meta}_  \n{body}  \n<{s['url']}>\n" if body
                        else f"**[{prefix}{i}]** {s['title']}  \n_{meta}_  \n<{s['url']}>\n")
        return rows or ["(none today)"]

    L += ["", "## Pain-point signals (people describing a problem or need)", ""] + block("P", pains)
    L += ["", "## Trend & news signals", ""] + block("T", trends)
    if macro_view is not None:
        L += render_macro(*macro_view, lim)
    return "\n".join(L) + "\n"


def render_macro(pulse, pulse_days, alerts, macro_items, lim):
    arrow = {"tailwind": "↑ tailwind", "headwind": "↓ headwind", "mixed": "↕ mixed", "neutral": "· neutral"}
    L = ["", "# Macro layer — policy, economy, weather, currency", "",
         "_Direction and sector tags are keyword hints made by the collector, not conclusions. Judge each item yourself._", "",
         f"## Sector pulse (policy/economy news, last {pulse_days} day(s) of history)", ""]
    if pulse:
        L += ["| sector | ↑ tailwind items | ↓ headwind items | net | net previous period |", "|---|---|---|---|---|"]
        for sec, up, down, net, prev in pulse[:20]:
            L.append(f"| {sec} | {up} | {down} | {net:+d} | {'—' if prev is None else f'{prev:+d}'} |")
    else:
        L.append("- (no sector-tagged policy or economy news today)")
    L += ["", "## Data alerts (weather, air quality, currency — next days)", ""]
    for i, s in enumerate(alerts, 1):
        L.append(f"**[D{i}]** {s['title']}  \n_{s['theme']} · sectors: {', '.join(s['sectors']) or '—'}_  \n{short(s['text'], lim)}  \n<{s['url']}>\n")
    if not alerts:
        L.append("(no weather, air or currency alerts above the thresholds)")
    L += ["", "## Policy & economy signals", ""]
    for i, s in enumerate(macro_items, 1):
        meta = " · ".join(x for x in [s["origin"], s["theme"], arrow[s["direction"]],
                                      ("sectors: " + ", ".join(s["sectors"])) if s["sectors"] else ""] if x)
        body = short(s["text"], lim)
        L.append(f"**[M{i}]** {s['title']}  \n_{meta}_  \n" + (f"{body}  \n" if body else "") + f"<{s['url']}>\n")
    if not macro_items:
        L.append("(none today)")
    return L


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def prune(folder: Path, keep_days: int, today: dt.date):
    for p in folder.glob("20*"):
        try:
            d = dt.date.fromisoformat(p.name[:10])
        except ValueError:
            continue
        if (today - d).days > keep_days:
            p.unlink()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fixtures", type=Path, help="offline mode: read responses from this folder")
    ap.add_argument("--out", type=Path, default=ROOT, help="where digests/ and data/ live")
    ap.add_argument("--now", type=int, help="override current unix time (tests)")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    now = args.now or int(time.time())
    since = now - cfg["window_hours"] * 3600
    today = dt.datetime.fromtimestamp(now, dt.timezone(dt.timedelta(hours=5, minutes=30))).date()  # IST date
    date_str = today.isoformat()
    f = Fetcher(args.fixtures)

    status, signals = {}, []
    runs = [
        ("Hacker News", "hackernews", lambda c: (collect_hn(c, f, since), None)),
        ("YouTube", "youtube", lambda c: collect_youtube(c, f, since, os.environ.get("YOUTUBE_API_KEY"))),
        ("Google News", "google_news", lambda c: (collect_google_news(c, f, since), None)),
        ("Google Trends", "google_trends", lambda c: (collect_trends(c, f, since), None)),
        ("RSS feeds", "rss", lambda c: (collect_rss(c, f, since), None)),
    ]
    mc = cfg.get("macro", {"enabled": False})
    if mc.get("enabled", True):
        runs += [
            ("Policy & economy news", "macro", lambda c: (macro.collect_policy_news(c, f, since), None)),
            ("Policy feeds (RBI, ET, Mint, BusinessLine)", "macro", lambda c: (macro.collect_policy_feeds(c, f, since), None)),
            ("Weather (Open-Meteo)", "macro", lambda c: macro.collect_weather(c["weather"], f, now)
             if c["weather"].get("enabled", True) else ([], "disabled")),
            ("Air quality (Open-Meteo)", "macro", lambda c: macro.collect_air(c["air_quality"], c["weather"]["cities"], f, now)
             if c["air_quality"].get("enabled", True) else ([], "disabled")),
            ("Currency (Frankfurter/ECB)", "macro", lambda c: (macro.collect_fx(c["fx"], f, now), None)
             if c["fx"].get("enabled", True) else ([], "disabled")),
        ]
    for name, key, fn in runs:
        if not cfg.get(key, {}).get("enabled", True):
            status[name] = (0, "disabled")
            continue
        try:
            items, note = fn(cfg[key])
            signals += items
            status[name] = (len(items), note or ("ok" if items else "no items (blocked or empty)"))
        except Exception as e:  # one broken source must never stop the run
            status[name] = (0, f"error: {type(e).__name__}: {str(e)[:80]}")
    if f.blocked:
        status["Refused today (skipped politely)"] = (0, ", ".join(sorted(f.blocked)))

    data, digests = args.out / "data", args.out / "digests"
    data.mkdir(parents=True, exist_ok=True)
    digests.mkdir(parents=True, exist_ok=True)

    # dedupe: within today, then against what was sent in the last N days
    seen = load_json(data / "seen.json", {})
    cutoff = (today - dt.timedelta(days=cfg["dedupe_days"])).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    fresh, today_keys, alerts = [], set(), []
    for s in signals:
        if not s["url"] or not s["title"]:
            continue
        if s["source"] in ("weather", "air", "fx"):
            alerts.append(s)           # forecasts are new every day: never deduped
            continue
        keys = {url_key(s["url"]), title_key(s["title"]) if s["source"] != "youtube_comment" else url_key(s["url"])}
        if keys & today_keys or any(k in seen and seen[k] < date_str for k in keys):
            continue
        if s["source"] in MACRO_SOURCES:
            macro.tag(s, mc)
            if not macro.relevant(s, mc):
                continue
            macro.macro_score(s, now, cfg["window_hours"], cfg["source_weights"])
        else:
            score(s, cfg, now)
        today_keys |= keys
        s["_keys"] = sorted(keys)
        fresh.append(s)

    # rising terms use every fresh signal, not only the selected ones
    tcount = Counter()
    for s in fresh:
        tcount.update(terms(s))
    history = load_json(data / "terms_history.json", {})
    rising = rising_terms(tcount, history, date_str)
    history[date_str] = dict(tcount.most_common(800))
    history = {d: v for d, v in history.items() if d >= (today - dt.timedelta(days=cfg["keep_days"])).isoformat()}

    micro = [s for s in fresh if s["source"] not in MACRO_SOURCES]
    macro_fresh = [s for s in fresh if s["source"] in MACRO_SOURCES]
    pains, trends = select(micro, cfg)
    macro_view, macro_sel = None, []
    if mc.get("enabled", True):
        macro_sel = macro.select_macro(macro_fresh, cfg["select"].get("macro_signals", 40), cfg["select"]["max_per_source"])
        alerts = sorted(alerts, key=lambda s: s["score"], reverse=True)[: cfg["select"].get("data_alerts", 20)]
        sec_hist = load_json(data / "sector_history.json", {})
        pulse, sec_hist, pulse_days = macro.sector_pulse(macro_fresh, sec_hist, date_str)
        sec_hist = {d: v for d, v in sec_hist.items() if d >= (today - dt.timedelta(days=cfg["keep_days"])).isoformat()}
        (data / "sector_history.json").write_text(json.dumps(sec_hist, ensure_ascii=False), encoding="utf-8")
        macro_view = (pulse, pulse_days, alerts, macro_sel)
    for s in pains + trends + macro_sel:
        for k in s.pop("_keys"):
            seen[k] = date_str
    for s in fresh:
        s.pop("_keys", None)

    md = render(date_str, pains, trends, rising, status, {"raw": len(signals), "fresh": len(fresh)}, cfg, macro_view)
    (digests / f"{date_str}.md").write_text(md, encoding="utf-8")
    (digests / "latest.md").write_text(md, encoding="utf-8")
    kept = pains + trends + macro_sel + (macro_view[2] if macro_view else [])
    (data / f"{date_str}-signals.json").write_text(json.dumps(kept, ensure_ascii=False, indent=1), encoding="utf-8")
    (data / "seen.json").write_text(json.dumps(seen, indent=0, sort_keys=True), encoding="utf-8")
    (data / "terms_history.json").write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
    prune(digests, cfg["keep_days"], today)
    prune(data, cfg["keep_days"], today)

    print(f"{date_str}: raw={len(signals)} fresh={len(fresh)} pains={len(pains)} trends={len(trends)} "
          f"macro={len(macro_sel)} alerts={len(macro_view[2]) if macro_view else 0} rising={len(rising)}")
    for name, (n, st) in status.items():
        print(f"  {name}: {n} — {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
