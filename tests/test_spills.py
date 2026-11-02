"""Spill counting by the 12/24 method (Ofwat, Storm overflows PC definition 1.2) and the
Environment Agency's dry-day definition (no rain above 0.25 mm that day and the 24 h before)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from helpers import ACTOR, make_env

from waterworks_desk import ingest, model, spills

H = 3600
T = 1_800_000_000 // 86400 * 86400      # a midnight UTC


def test_12_24_worked_examples():
    c = spills.count_12_24
    assert c([]) == 0
    assert c([(T, T + 1 * H)]) == 1                                   # one short spill
    assert c([(T, T + 1 * H), (T + 5 * H, T + 6 * H)]) == 1           # two inside the first 12 h: still one
    assert c([(T, T + 13 * H)]) == 2                                  # runs into the next 24 h block
    assert c([(T, T + 36 * H)]) == 2                                  # 12 h + one full 24 h block
    assert c([(T, T + 36 * H + 1)]) == 3                              # a second into the block after
    assert c([(T, T + H), (T + 20 * H, T + 21 * H)]) == 2             # a new discharge in the 24 h block
    assert c([(T, T + H), (T + 37 * H, T + 38 * H)]) == 2             # block 12-36 h empty: start again
    assert c([(T, T + H), (T + 35 * H, T + 36 * H), (T + 59 * H, T + 60 * H)]) == 3   # chained blocks
    week = [(T + d * 24 * H, T + d * 24 * H + H) for d in range(7)]  # an hour a day for a week
    assert c(week) == 7


def setup():
    tmp, cfg, conn = make_env("""
[[sources]]
key = "x"
type = "csv"
path = "data/import"
""")
    model.save(conn, "region", {"key": "r", "name": "R", "timezone": "UTC", "lat": 51.5, "lon": -0.1, "radius_km": 200}, ACTOR)
    model.save(conn, "system", {"key": "s", "region": "r", "name": "Sewers", "network_type": "combined_sewer"}, ACTOR)
    model.save(conn, "system", {"key": "g", "region": "r", "name": "Rain", "network_type": "storm_drain"}, ACTOR)
    model.save(conn, "site", {"key": "o", "system": "s", "name": "Overflow O", "site_type": "overflow_weir", "lat": 51.5, "lon": -0.1,
                              "note": "Storm overflow discharging to River Test."}, ACTOR)
    model.save(conn, "sensor", {"key": "o-edm", "site": "o", "name": "Spill monitor", "quantity": "state", "source": "manual"}, ACTOR)
    model.save(conn, "site", {"key": "near", "system": "g", "name": "Near gauge", "site_type": "rain_gauge", "lat": 51.52, "lon": -0.1}, ACTOR)
    model.save(conn, "sensor", {"key": "near-r", "site": "near", "name": "Rain", "quantity": "rain", "source": "manual",
                                "expected_interval_s": 900}, ACTOR)
    model.save(conn, "site", {"key": "far", "system": "g", "name": "Far gauge", "site_type": "rain_gauge", "lat": 52.5, "lon": -0.1}, ACTOR)
    model.save(conn, "sensor", {"key": "far-r", "site": "far", "name": "Rain", "quantity": "rain", "source": "manual",
                                "expected_interval_s": 900}, ACTOR)
    model.go_live(conn, "system", "s", ACTOR)
    model.go_live(conn, "system", "g", ACTOR)
    return conn


def put(conn, key, pairs):
    sid = conn.execute("SELECT id FROM sensors WHERE key=?", (key,)).fetchone()[0]
    ingest.store(conn, "manual", [("x", ts, v, q) for ts, v, q in pairs], {"x": (sid, lambda v: v)})
    conn.commit()
    return sid


def rain(conn, key, t0, t1, mm_each=0.0, wet_at=None, wet_mm=0.0):
    put(conn, key, [(ts, wet_mm if wet_at is not None and ts == wet_at else mm_each, 0) for ts in range(t0 + 900, t1 + 1, 900)])


def test_dry_day_check():
    conn = setup()
    start = T + 10 * H                                                 # 10:00 on day T
    sid = put(conn, "o-edm", [(start, 1, 0), (start + 2 * H, 0, 0)])
    # no gauge data yet: can't tell
    assert spills.dry_check(conn, 51.5, -0.1, start, start + 3 * H, "UTC")["dry"] is None
    rain(conn, "near-r", T - 24 * H, T + 24 * H)                       # 0 mm all through the window
    d = spills.dry_check(conn, 51.5, -0.1, start, T + 24 * H, "UTC")
    assert d["dry"] is True and d["mm"] == 0 and d["gauges"][0]["name"] == "Near gauge"   # the far gauge (110 km) isn't used
    rain(conn, "near-r", T - 24 * H, T + 24 * H, wet_at=T - 6 * H, wet_mm=0.4)            # 0.4 mm the evening before
    assert spills.dry_check(conn, 51.5, -0.1, start, T + 24 * H, "UTC")["dry"] is False
    assert spills.periods(conn, sid, T, T + 24 * H) == [(start, start + 2 * H)]
    r = spills.report(conn, T - 24 * H, T + 24 * H, T + 24 * H, tz="UTC")
    o = r["overflows"][0]
    assert (o["name"], o["water"], o["spills"], o["hours"], o["dry"]) == ("Overflow O", "River Test", 1, 2.0, 0)
    assert r["complete_from"] is None                                  # a continuous source: complete from data_from
    from waterworks_desk import db as dbm
    dbm.record_import(conn, "thames", "feed", True, 1, 1, 0, "")
    r = spills.report(conn, T - 24 * H, T + 24 * H, T + 24 * H, tz="UTC", snapshot_sources=["thames"])
    assert r["complete_from"] and abs(r["complete_from"] - dbm.now_ms() // 1000) < 60   # a snapshot feed: from its first read


def test_spill_already_running_and_offline():
    conn = setup()
    sid = put(conn, "o-edm", [(T - 5 * H, 1, 0), (T + 3 * H, 0, 0), (T + 5 * H, 0, 2), (T + 7 * H, 1, 0)])
    assert spills.periods(conn, sid, T, T + 10 * H) == [(T, T + 3 * H), (T + 7 * H, T + 10 * H)]   # offline isn't a spill


def test_dry_spill_alert():
    from waterworks_desk import alerts
    conn = setup()
    start = T + 10 * H
    put(conn, "o-edm", [(start, 1, 0)])
    rain(conn, "near-r", T - 24 * H, start + H)                        # dry so far
    alerts.evaluate(conn, None, start + H)
    got = {a["rule"]: a for a in alerts.active(conn, now_ms=(start + H) * 1000)}
    assert got["spilling"]["severity"] == "warning"
    d = got["dry_spill"]
    assert "on what looks like a dry day" in d["message"] and "Near gauge, 2.2 km" in d["message"] and "at most 0 mm" in d["message"]
    put(conn, "o-edm", [(start + 2 * H, 0, 0)])                       # it stops
    alerts.evaluate(conn, None, start + 3 * H)
    ended = {a["rule"]: a for a in alerts.active(conn, now_ms=(start + 3 * H) * 1000, include_ended_hours=2)}
    assert ended["dry_spill"]["end_ms"] == (start + 2 * H) * 1000


def test_a_wet_spill_raises_no_dry_alert():
    from waterworks_desk import alerts
    conn = setup()
    start = T + 10 * H
    put(conn, "o-edm", [(start, 1, 0)])
    rain(conn, "near-r", T - 24 * H, start + H, wet_at=T + 8 * H, wet_mm=6.0)
    alerts.evaluate(conn, None, start + H)
    assert {a["rule"] for a in alerts.active(conn, now_ms=(start + H) * 1000)} == {"spilling"}
