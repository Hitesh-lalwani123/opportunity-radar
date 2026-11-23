"""Offline tests: build fake API/feed responses, run the collector twice, check the digest."""
import json
import sys
from email.utils import formatdate
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "radar"))
import collect  # noqa: E402

NOW = 1795000000  # fixed clock


def rss(items, ns=""):
    body = "".join(items)
    return f'<?xml version="1.0"?><rss version="2.0" {ns}><channel><title>x</title>{body}</channel></rss>'


def item(title, link, desc="", hours_ago=2, extra=""):
    return (f"<item><title>{title}</title><link>{link}</link><description>{desc}</description>"
            f"<pubDate>{formatdate(NOW - hours_ago * 3600)}</pubDate>{extra}</item>")


def make_fixtures(d: Path, variant=0):
    d.mkdir(parents=True, exist_ok=True)
    hits = [
        {"objectID": "101", "title": "Ask HN: Is there a tool to reconcile GST invoices automatically?", "story_text": "I'd pay for this, doing it in spreadsheets is a nightmare",
         "points": 120, "num_comments": 80, "created_at_i": NOW - 3600},
        {"objectID": "102", "title": "Ask HN: What are you working on?", "story_text": "", "points": 30, "num_comments": 200, "created_at_i": NOW - 7200},
        {"objectID": "103", "title": "Old post", "story_text": "", "points": 5, "num_comments": 1, "created_at_i": NOW - 3600},
    ]
    if variant == 1:
        hits.append({"objectID": "104", "title": "Ask HN: Why is there no simple invoice reminder app for small distributors?",
                     "story_text": "invoice reminder whatsapp", "points": 40, "num_comments": 22, "created_at_i": NOW - 1800})
    (d / "hn_ask_hn.json").write_text(json.dumps({"hits": hits}))
    (d / "hn_comment_0.json").write_text(json.dumps({"hits": [
        {"objectID": "201", "story_title": "Show HN: Billing tool", "comment_text": "Is there a tool like this for <i>clinics</i>? Would pay for it.", "created_at_i": NOW - 600}]}))
    (d / "yt_search_0.json").write_text(json.dumps({"items": [{"id": {"videoId": "vid1"}}, {"id": {"videoId": "vid2"}}]}))
    (d / "yt_videos_0.json").write_text(json.dumps({"items": [
        {"id": "vid1", "snippet": {"title": "10 business ideas for 2027", "description": "desc", "publishedAt": "2026-11-21T10:00:00Z"},
         "statistics": {"viewCount": "250000", "likeCount": "9000", "commentCount": "1200"}},
        {"id": "vid2", "snippet": {"title": "Seller problems on marketplaces", "description": "", "publishedAt": "2026-11-21T12:00:00Z"},
         "statistics": {"viewCount": "40000", "likeCount": "1000", "commentCount": "300"}}]}))
    (d / "yt_comments_vid1.json").write_text(json.dumps({"items": [
        {"id": "c1", "snippet": {"totalReplyCount": 12, "topLevelComment": {"snippet": {"textDisplay": "I wish there was an app that handles returns for small sellers, so frustrating", "likeCount": 340, "publishedAt": "2026-11-21T11:00:00Z"}}}},
        {"id": "c2", "snippet": {"totalReplyCount": 0, "topLevelComment": {"snippet": {"textDisplay": "nice video bro", "likeCount": 50, "publishedAt": "2026-11-21T11:00:00Z"}}}}]}))
    (d / "gnews_0_0.xml").write_text(rss([
        item("Agritech startup raises $5M to fix cold storage shortage", "https://example.com/a?utm_source=x", "cold storage shortage hits farmers"),
        item("Agritech startup raises $5M to fix cold storage shortage", "https://example.com/a", "duplicate"),
        item("Very old story", "https://example.com/old", hours_ago=100)]))
    ns = 'xmlns:ht="https://trends.google.com/trending/rss"'
    (d / "trends_IN.xml").write_text(rss([item("cold storage", "https://trends.google.com/x", extra="<ht:approx_traffic>20000+</ht:approx_traffic>")], ns))
    (d / "rss_0.xml").write_text(rss([item("D2C brands struggling with return costs", "https://inc42.com/r1", "Sellers say returns are too expensive")]))


def run(tmp, fx, now=NOW):
    out = tmp / "out"
    assert collect.main(["--fixtures", str(fx), "--out", str(out), "--now", str(now)]) == 0
    return out


def test_digest_end_to_end(tmp_path):
    fx = tmp_path / "fx"
    make_fixtures(fx)
    out = run(tmp_path, fx)
    md = (out / "digests" / "latest.md").read_text()
    assert "Ask HN: Is there a tool to reconcile GST invoices" in md
    assert "pain: is there a tool" in md
    assert "Comment on: 10 business ideas" in md          # pain comment kept
    assert "nice video bro" not in md                      # noise comment dropped
    assert md.count("Agritech startup raises") == 1        # duplicate URL (utm) removed
    assert "Very old story" not in md                      # outside window
    assert "20000+ searches" in md                         # trends traffic parsed
    assert "<i>" not in md                                 # html stripped
    # pain section comes first and the strongest pain signal is P1
    p1 = md.split("[P1]")[1].splitlines()[0]
    assert "GST invoices" in p1 or "returns" in p1


def test_second_day_dedupes_and_finds_rising(tmp_path):
    fx = tmp_path / "fx"
    make_fixtures(fx)
    out = run(tmp_path, fx)
    fx2 = tmp_path / "fx2"
    make_fixtures(fx2, variant=1)
    # add more 'invoice reminder' mentions on day 2 so the term rises
    extra = [item(f"Invoice reminder pain #{i}", f"https://ex.com/inv{i}", "invoice reminder for distributors") for i in range(4)]
    (fx2 / "rss_1.xml").write_text(rss(extra))
    out2 = tmp_path / "out"
    assert collect.main(["--fixtures", str(fx2), "--out", str(out2), "--now", str(NOW + 86400)]) == 0
    md = (out2 / "digests" / "latest.md").read_text()
    assert "reconcile GST invoices" not in md              # already sent yesterday
    assert "Why is there no simple invoice reminder" in md  # new item kept
    assert "**invoice reminder**" in md                     # rising term detected
    assert len(list((out2 / "digests").glob("20*.md"))) == 2


def test_missing_sources_do_not_crash(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = run(tmp_path, empty)
    md = (out / "digests" / "latest.md").read_text()
    assert "(none today)" in md and "Source status" in md


def test_url_key_ignores_tracking():
    assert collect.url_key("https://www.ex.com/a/?utm_source=t&id=3") == collect.url_key("https://ex.com/a?id=3")
