"""Offline tests for the macro layer (policy, economy, weather, air quality, currency)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "radar"))
import collect  # noqa: E402
import macro  # noqa: E402
from test_collect import NOW, item, make_fixtures, rss  # noqa: E402

CFG = collect.yaml.safe_load((collect.ROOT / "config.yaml").read_text(encoding="utf-8"))
MC = CFG["macro"]


def sig(title, text=""):
    return collect.make_signal("policy_news", title, text, "https://ex.com/" + title[:10], NOW)


def make_macro_fixtures(d: Path, day=0):
    (d / "macro_news_0_0.xml").write_text(rss([
        item(f"Cabinet approves Rs {5000 + day * 1000} crore subsidy scheme for rooftop solar phase {day + 1}", f"https://ex.com/solar{day}", "MNRE scheme"),
        item(f"Government bans export of onions to cool prices {'' if day == 0 else 'again in December'}", f"https://ex.com/onion{day}", "export ban hits farmers"),
        item(f"Government bans export of onions to cool prices {'' if day == 0 else 'again in December'} - Another Paper", f"https://ex2.com/onion{day}", "same story"),
        item("Urban gardens see record footfall", f"https://ex.com/urban{day}", "a suburban gardening club meets"),
    ]), encoding="utf-8")
    (d / "macro_rss_0.xml").write_text(rss([
        item("Money Market Operations as on November 20, 2026", f"https://rbi.org.in/mmo{day}", "auction details"),
        item("RBI issues draft guidelines on digital lending apps", f"https://rbi.org.in/dl{day}", "stricter norms for loan apps"),
    ]), encoding="utf-8")
    days = [f"2026-11-{22 + i}" for i in range(5)]
    city = lambda tmax, rain: {"daily": {"time": days, "temperature_2m_max": tmax, "temperature_2m_min": [20] * 5, "precipitation_sum": rain}}
    n = len(MC["weather"]["cities"])
    weather = [city([44, 45, 39, 30, 30], [0] * 5), city([30] * 5, [0, 120, 80, 0, 0])] + [city([30] * 5, [0] * 5)] * (n - 2)
    (d / "weather.json").write_text(json.dumps(weather), encoding="utf-8")
    hours = [f"2026-11-22T{h:02d}:00" for h in range(24)]
    (d / "air.json").write_text(json.dumps([{"hourly": {"time": hours, "pm2_5": [300] * 24}}] + [{"hourly": {"time": hours, "pm2_5": [40] * 24}}] * (n - 1)), encoding="utf-8")
    (d / "fx_0.json").write_text(json.dumps({"rates": {"2026-10-23": {"INR": 90.0}, "2026-11-20": {"INR": 92.7}}}), encoding="utf-8")


def run(tmp, fx, now):
    out = tmp / "out"
    assert collect.main(["--fixtures", str(fx), "--out", str(out), "--now", str(now)]) == 0
    return (out / "digests" / "latest.md").read_text(encoding="utf-8")


def test_word_matching_is_whole_word():
    assert macro.hits(["ban"], "urban banking news") == []
    assert macro.hits(["ban"], "govt bans exports") == ["bans"]
    assert macro.hits(["manufactur*"], "manufacturing pmi") == ["manufacturing"]


def test_tag_direction_and_sectors():
    s = macro.tag(sig("Cabinet approves subsidy for rooftop solar"), MC)
    assert s["direction"] == "tailwind" and "solar & energy" in s["sectors"] and s["theme"] == "policy"
    s = macro.tag(sig("Government bans onion exports"), MC)
    assert s["direction"] == "headwind" and "msme & exports" in s["sectors"]
    s = macro.tag(sig("CPI inflation rises to 6%"), MC)
    assert s["direction"] == "neutral"          # a number moving is not a policy direction


def test_macro_section_end_to_end(tmp_path):
    fx = tmp_path / "fx"
    make_fixtures(fx)
    make_macro_fixtures(fx)
    md = run(tmp_path, fx, NOW)
    macro_part = md.split("# Macro layer")[1]
    assert "Cabinet approves Rs 5000 crore subsidy" in macro_part and "↑ tailwind" in macro_part
    assert macro_part.count("bans export of onions") == 1        # same story from two outlets kept once
    assert "Money Market Operations" not in md                    # routine RBI item excluded
    assert "Urban gardens" not in md                         # no sector, no direction -> dropped
    assert "digital lending" in macro_part
    assert "Heat alert: Delhi" in macro_part and "Heavy Rain alert: Mumbai" in macro_part
    assert "Air quality SEVERE: Delhi" in macro_part
    assert "USD/INR 92.7 — +3.00%" in macro_part and "BIG MOVE" in macro_part
    assert "| solar & energy | 1 | 0 | +1 |" in macro_part
    # micro sections still work and macro items don't leak into them
    assert "Cabinet approves" not in md.split("# Macro layer")[0]


def test_second_day_keeps_alerts_and_builds_pulse_history(tmp_path):
    fx = tmp_path / "fx"
    make_fixtures(fx)
    make_macro_fixtures(fx)
    run(tmp_path, fx, NOW)
    fx2 = tmp_path / "fx2"
    make_fixtures(fx2, variant=1)
    make_macro_fixtures(fx2, day=1)
    md = run(tmp_path, fx2, NOW + 86400)
    assert "Heat alert: Delhi" in md                               # forecasts are never deduped
    assert "| solar & energy | 2 | 0 | +2 |" in md                 # 2 days of history summed
    assert "last 2 day(s)" in md


def test_macro_can_be_disabled(tmp_path, monkeypatch):
    fx = tmp_path / "fx"
    make_fixtures(fx)
    make_macro_fixtures(fx)
    cfg = dict(CFG, macro=dict(MC, enabled=False))
    monkeypatch.setattr(collect.yaml, "safe_load", lambda _text: cfg)
    md = run(tmp_path, fx, NOW)
    assert "# Macro layer" not in md
