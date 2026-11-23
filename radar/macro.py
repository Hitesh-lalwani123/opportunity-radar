"""Macro layer for Opportunity Radar: government policy, economy, weather, air quality, currency.

Pain/trend signals say *what people want*. Macro signals say *which way the wind is blowing*:
a new subsidy, a ban, a rate cut, a heatwave next week or a falling rupee can make an idea go
ahead or kill it. This module collects those signals from free, official or keyless sources,
tags each with sectors and a tailwind/headwind hint, and keeps a 7-day "sector pulse".

The tags are keyword hints only. The daily Claude run makes the actual judgement.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import statistics
from collections import Counter, defaultdict

from common import Fetcher, make_signal, parse_feed


# --------------------------------------------------------------------------- tagging
_RX_CACHE: dict[tuple, re.Pattern] = {}


def word_rx(words) -> re.Pattern:
    """Whole-word / whole-phrase matcher ("ban" must not match "urban" or "bank").

    A plural s/es is allowed. A keyword ending in "*" is a prefix ("manufactur*").
    """
    key = tuple(words)
    if key not in _RX_CACHE:
        parts = []
        for w in words:
            w = w.strip().lower()
            if not w:
                continue
            parts.append(re.escape(w[:-1]) + r"[a-z]*" if w.endswith("*") else re.escape(w) + r"(?:s|es)?")
        _RX_CACHE[key] = re.compile(r"(?<![a-z0-9])(?:" + "|".join(parts or ["(?!)"]) + r")(?![a-z0-9])")
    return _RX_CACHE[key]


def hits(words, blob):
    return word_rx(words).findall(blob)


def tag(sig, mcfg):
    """Attach theme, direction hint and sectors to a macro signal (in place)."""
    blob = f"{sig['title']} {sig['text'][:600]}".lower()
    sig["sectors"] = [name for name, words in mcfg["sectors"].items() if hits(words, blob)][:4]
    up, down = len(hits(mcfg["direction"]["tailwind"], blob)), len(hits(mcfg["direction"]["headwind"], blob))
    sig["direction"] = "tailwind" if up > down else "headwind" if down > up else ("mixed" if up else "neutral")
    sig.setdefault("theme", next((t for t, words in mcfg["themes"].items() if hits(words, blob)), "economy"))
    sig["pain"] = []
    return sig


def relevant(sig, mcfg):
    blob = f"{sig['title']} {sig['text'][:300]}".lower()
    if hits(mcfg.get("exclude_phrases", []), blob):
        return False
    return bool(sig["sectors"]) or sig["direction"] != "neutral"


def macro_score(sig, now, window_hours, weights):
    age_h = max(0, (now - sig["ts"]) / 3600)
    recency = max(0.0, 1 - age_h / window_hours)
    strength = {"tailwind": 2, "headwind": 2, "mixed": 1, "neutral": 0}[sig["direction"]]
    sig["score"] = round(weights.get(sig["source"], 1.0) * (2 * min(len(sig["sectors"]), 3) + strength) + recency, 3)
    return sig


# --------------------------------------------------------------------------- news / feeds
def collect_policy_news(mcfg, f: Fetcher, since: int):
    out = []
    for gi, (geo, queries) in enumerate(mcfg["news_queries"].items()):
        ed = mcfg["editions"][geo]
        for qi, q in enumerate(queries):
            raw = f.get("https://news.google.com/rss/search", {"q": f"{q} when:1d", **ed}, fixture=f"macro_news_{gi}_{qi}.xml")
            if raw:
                out += parse_feed(raw, "policy_news", f"policy-news/{geo}", since)[: mcfg.get("max_per_query", 8)]
    return out


def collect_policy_feeds(mcfg, f: Fetcher, since: int):
    out = []
    for i, feed in enumerate(mcfg["feeds"]):
        raw = f.get(feed["url"], fixture=f"macro_rss_{i}.xml")
        if raw:
            out += parse_feed(raw, "policy_rss", feed["name"], since)
    return out


# --------------------------------------------------------------------------- data alerts
def _fmt_day(iso):
    return dt.date.fromisoformat(iso[:10]).strftime("%d %b")


def collect_weather(wcfg, f: Fetcher, now: int):
    """Heat, cold and heavy-rain alerts for the next N days from Open-Meteo (free, no key)."""
    cities = wcfg["cities"]
    params = {"latitude": ",".join(str(c["lat"]) for c in cities), "longitude": ",".join(str(c["lon"]) for c in cities),
              "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
              "forecast_days": wcfg["forecast_days"], "timezone": "Asia/Kolkata"}
    raw = f.get("https://api.open-meteo.com/v1/forecast", params, fixture="weather.json")
    if not raw:
        return [], "no response"
    data = json.loads(raw)
    data = data if isinstance(data, list) else [data]
    th, hints, out = wcfg["thresholds"], wcfg["sector_hints"], []
    for city, d in zip(cities, data):
        daily = d.get("daily", {})
        days = daily.get("time", [])
        checks = [
            ("heat", "temperature_2m_max", lambda v: v >= th["heat_max_c"], max, "°C max"),
            ("cold", "temperature_2m_min", lambda v: v <= th["cold_min_c"], min, "°C min"),
            ("heavy_rain", "precipitation_sum", lambda v: v >= th["heavy_rain_mm"], max, "mm rain/day"),
        ]
        for kind, field, hit, pick, unit in checks:
            vals = [(day, v) for day, v in zip(days, daily.get(field, [])) if v is not None]
            bad = [(day, v) for day, v in vals if hit(v)]
            if not bad:
                continue
            peak_day, peak = pick(bad, key=lambda x: x[1])
            title = (f"{kind.replace('_', ' ').title()} alert: {city['name']} — {len(bad)} of next {len(vals)} days "
                     f"(peak {peak}{unit} on {_fmt_day(peak_day)})")
            s = make_signal("weather", title, f"Forecast window {_fmt_day(days[0])}–{_fmt_day(days[-1])}. "
                            f"Who is affected: {hints[kind]['note']}",
                            f"https://open-meteo.com/en/docs#latitude={city['lat']}&longitude={city['lon']}", now,
                            len(bad), f"{len(bad)} alert days", "open-meteo")
            s.update(theme="weather", direction="mixed", sectors=hints[kind]["sectors"], pain=[], score=5 + len(bad))
            out.append(s)
    return out, f"ok ({len(data)} cities checked, {len(out)} alerts)"


def collect_air(acfg, cities, f: Fetcher, now: int):
    """Very poor / severe PM2.5 days (daily mean of hourly forecast) from Open-Meteo air quality."""
    params = {"latitude": ",".join(str(c["lat"]) for c in cities), "longitude": ",".join(str(c["lon"]) for c in cities),
              "hourly": "pm2_5", "forecast_days": acfg["forecast_days"], "timezone": "Asia/Kolkata"}
    raw = f.get("https://air-quality-api.open-meteo.com/v1/air-quality", params, fixture="air.json")
    if not raw:
        return [], "no response"
    data = json.loads(raw)
    data = data if isinstance(data, list) else [data]
    out = []
    for city, d in zip(cities, data):
        h = d.get("hourly", {})
        per_day = defaultdict(list)
        for t, v in zip(h.get("time", []), h.get("pm2_5", [])):
            if v is not None:
                per_day[t[:10]].append(v)
        means = {day: round(statistics.mean(v)) for day, v in per_day.items() if v}
        bad = {day: m for day, m in means.items() if m >= acfg["very_poor_pm25"]}
        if not bad:
            continue
        peak_day = max(bad, key=bad.get)
        level = "SEVERE" if bad[peak_day] >= acfg["severe_pm25"] else "very poor"
        s = make_signal("air", f"Air quality {level}: {city['name']} — PM2.5 daily mean up to {bad[peak_day]} µg/m³ "
                        f"({len(bad)} of next {len(means)} days)", f"Who is affected: {acfg['note']}",
                        "https://open-meteo.com/en/docs/air-quality-api", now, len(bad), f"{len(bad)} bad-air days", "open-meteo-air")
        s.update(theme="weather", direction="mixed", sectors=acfg["sectors"], pain=[], score=5 + len(bad))
        out.append(s)
    return out, f"ok ({len(data)} cities checked, {len(out)} alerts)"


def collect_fx(xcfg, f: Fetcher, now: int):
    """Currency moves vs INR over the last N days (ECB reference rates via Frankfurter, free, no key)."""
    start = (dt.datetime.fromtimestamp(now, dt.timezone.utc).date() - dt.timedelta(days=xcfg["lookback_days"])).isoformat()
    out = []
    for i, base in enumerate(xcfg["bases"]):
        raw = f.get(f"https://api.frankfurter.app/{start}..", {"from": base, "to": "INR"}, fixture=f"fx_{i}.json")
        if not raw:
            continue
        rates = json.loads(raw).get("rates", {})
        series = [v["INR"] for _, v in sorted(rates.items()) if "INR" in v]
        if len(series) < 2:
            continue
        first, last = series[0], series[-1]
        pct = round((last - first) / first * 100, 2)
        big = abs(pct) >= xcfg["alert_pct"]
        move = "rupee weaker" if pct > 0 else "rupee stronger"
        note = xcfg["hints"]["rupee_weaker" if pct > 0 else "rupee_stronger"]
        s = make_signal("fx", f"{base}/INR {last} — {pct:+.2f}% in {xcfg['lookback_days']} days ({move})"
                        + (" — BIG MOVE" if big else ""), note["note"],
                        f"https://www.frankfurter.app/", now, int(abs(pct) * 100), "ECB reference rate", "frankfurter")
        s.update(theme="economy", direction="mixed" if big else "neutral", sectors=note["sectors"] if big else [],
                 pain=[], score=(6 if big else 1) + abs(pct))
        out.append(s)
    return out


# --------------------------------------------------------------------------- sector pulse
def sector_pulse(today_items, history, today_str, days=7):
    """Return rows (sector, up7, down7, net7, net_prev7, today_ids) and the updated history.

    history = {date: {sector: [tailwinds, headwinds]}}; built from every relevant macro item, not
    only the ones printed, so the trend is not distorted by the digest size limits.
    """
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in today_items:
        for sec in s["sectors"]:
            if s["direction"] == "tailwind":
                counts[sec][0] += 1
            elif s["direction"] == "headwind":
                counts[sec][1] += 1
    history = dict(history)
    history[today_str] = {k: v for k, v in counts.items()}
    dates = sorted(d for d in history if d <= today_str)
    recent, prev = dates[-days:], dates[-2 * days:-days]

    def window(ds):
        tot: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for d in ds:
            for sec, (u, w) in history[d].items():
                tot[sec][0] += u
                tot[sec][1] += w
        return tot

    cur, old = window(recent), window(prev)
    rows = []
    for sec, (u, w) in cur.items():
        if u + w == 0:
            continue
        pu, pw = old.get(sec, [0, 0])
        rows.append((sec, u, w, u - w, (pu - pw) if prev else None))
    rows.sort(key=lambda r: (abs(r[3]), r[1] + r[2]), reverse=True)
    return rows, history, len(recent)


def _tokens(title):
    title = re.sub(r"\s+-\s+[^-]+$", "", title.lower())        # drop " - Publisher" suffix from Google News titles
    return {w for w in re.findall(r"[a-z0-9]{3,}", title)}


def select_macro(items, cap, per_source, similar=0.5):
    """Best-scored first, capped per source, and the same story told by several outlets kept once."""
    chosen, seen_src, kept_tokens = [], Counter(), []
    for s in sorted(items, key=lambda s: s["score"], reverse=True):
        if seen_src[s["source"]] >= per_source:
            continue
        tok = _tokens(s["title"])
        if any(len(tok & k) / max(1, len(tok | k)) >= similar for k in kept_tokens):
            continue
        kept_tokens.append(tok)
        chosen.append(s)
        seen_src[s["source"]] += 1
        if len(chosen) >= cap:
            break
    return chosen
