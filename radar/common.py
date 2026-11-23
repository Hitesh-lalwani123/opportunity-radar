"""Shared helpers for the Opportunity Radar collectors (HTTP, feeds, signal records)."""
from __future__ import annotations

import calendar
import hashlib
import html
import re
import time
import urllib.parse

import feedparser
import requests

UA = "OpportunityRadar/1.0 (personal non-commercial research digest)"
PAUSE_SECONDS = 1.0
HOST_PAUSE = {"news.google.com": 2.5}   # Google rate-limits bursts; go slower there

TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


# --------------------------------------------------------------------------- helpers
def clean(text: str | None) -> str:
    if not text:
        return ""
    text = html.unescape(TAG_RE.sub(" ", text))
    return SPACE_RE.sub(" ", text).strip()


def short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def url_key(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
    norm = urllib.parse.urlunsplit((p.scheme.lower(), p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), urllib.parse.urlencode(q), ""))
    return hashlib.sha1(norm.encode()).hexdigest()[:16]


def title_key(title: str) -> str:
    words = re.findall(r"\w+", title.lower())
    return hashlib.sha1(" ".join(words[:12]).encode()).hexdigest()[:16]


def parse_traffic(value: str | None) -> int:
    if not value:
        return 0
    digits = re.sub(r"[^\d]", "", str(value))
    return int(digits) if digits else 0


class Fetcher:
    """One polite HTTP client. In fixture mode it reads files instead of the network."""

    def __init__(self, fixtures: Path | None = None):
        self.fixtures = fixtures
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self.blocked: set[str] = set()
        self._last: dict[str, float] = {}

    def get(self, url: str, params: dict | None = None, fixture: str | None = None) -> str | None:
        if self.fixtures is not None:
            path = self.fixtures / fixture if fixture else None
            return path.read_text(encoding="utf-8") if path and path.exists() else None
        host = urllib.parse.urlsplit(url).netloc
        if host in self.blocked:
            return None
        wait = HOST_PAUSE.get(host, PAUSE_SECONDS) - (time.time() - self._last.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        for attempt in range(2):
            try:
                r = self.session.get(url, params=params, timeout=20)
                self._last[host] = time.time()
                if r.status_code in (403, 429):
                    self.blocked.add(host)       # respect the refusal; try again tomorrow
                    return None
                if r.status_code >= 500 and attempt == 0:
                    time.sleep(3)
                    continue
                return r.text if r.ok else None
            except requests.RequestException:
                if attempt == 0:
                    time.sleep(3)
        return None


def make_signal(source, title, text, url, ts, engagement=0, label="", origin=""):
    return {
        "source": source, "origin": origin or source, "title": clean(title), "text": clean(text),
        "url": url, "ts": int(ts or time.time()), "engagement": int(engagement or 0), "label": label,
    }


def parse_feed(raw: str, source: str, origin: str, since: int, traffic=False):
    out = []
    for e in feedparser.parse(raw).entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        ts = calendar.timegm(t) if t else time.time()
        if ts < since:
            continue
        eng, label = 0, ""
        if traffic:
            eng = parse_traffic(e.get("ht_approx_traffic"))
            label = f"{e.get('ht_approx_traffic', '?')} searches"
            news = e.get("ht_news_item_title")
            text = f"Related news: {news}" if news else ""
        else:
            text = e.get("summary", "")
        out.append(make_signal(source, e.get("title"), text, e.get("link", ""), ts, eng, label, origin))
    return out
