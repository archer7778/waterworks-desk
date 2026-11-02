"""Alerts: severity from the rule and criticality, two readings in a row to open or close, plain
sentences with what/where/since/why/check, one alert for a silent site, overflow and tank levels,
acknowledge and shelve into the audit trail, failing sources, and pending changes to live items."""
import pytest
from helpers import ACTOR, CSV_SOURCE, basic_network, make_env

from waterworks_desk import alerts, ingest, logchain, model

T0 = 1_800_000_000 // 3600 * 3600   # a fixed hour, well in the future of nothing


def setup_live():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    model.go_live(conn, "system", "dw", ACTOR, {"exports"})
    return tmp, cfg, conn


def put(conn, key, pairs, unit=None):
    """Store (minutes after T0, value in the sensor's source unit) readings for one sensor."""
    r = conn.execute("SELECT id, quantity, source_unit, unit FROM sensors WHERE key=?", (key,)).fetchone()
    from waterworks_desk import units
    conv = units.converter(r["quantity"], unit or r["source_unit"] or r["unit"])
    stored, _, spans = ingest.store(conn, "t", [("x", T0 + m * 60, v, 0) for m, v in pairs], {"x": (r["id"], conv)})
    ingest.rollup(conn, spans)
    conn.commit()


def open_alerts(conn):
    return {a["rule"] + ":" + (a["sensor"] or a["site"] or ""): a for a in alerts.active(conn, now_ms=(T0 + 10**6) * 1000)}


def test_limit_alert_opens_after_two_readings_and_clears_after_two():
    tmp, cfg, conn = setup_live()
    put(conn, "ps1-p1-dp", [(0, 4.0), (5, 4.0), (10, 1.8)])          # one reading below warn (2.0 bar): not yet
    alerts.evaluate(conn, None, T0 + 600)
    assert open_alerts(conn) == {}
    put(conn, "ps1-p1-dp", [(15, 1.7)])                                # two in a row: warning, since 00:10
    alerts.evaluate(conn, None, T0 + 900)
    a = open_alerts(conn)["limit_low:ps1-p1-dp"]
    # Pump 1 sits at a criticality-1 site, but warnings aren't raised (only advisories are)
    assert a["severity"] == "warning" and a["start_ms"] == (T0 + 600) * 1000
    assert a["message"].startswith("Pump 1 discharge pressure at Riverside pumping station is 1.70 bar, below its warning limit of 2.00 bar, since ")
    assert "Low pressure can mean a burst main" in a["message"] and "First check: pump status" in a["message"]
    put(conn, "ps1-p1-dp", [(20, 1.4), (25, 1.3)])                     # past the alarm limit: critical, same alert
    ch = alerts.evaluate(conn, None, T0 + 1500)
    a2 = open_alerts(conn)["limit_low:ps1-p1-dp"]
    assert a2["id"] == a["id"] and a2["severity"] == "critical" and ch["escalated"]
    assert "alarm limit of 1.50 bar" in a2["message"]
    put(conn, "ps1-p1-dp", [(30, 4.0)])                                # one good reading: still open
    alerts.evaluate(conn, None, T0 + 1800)
    assert "limit_low:ps1-p1-dp" in open_alerts(conn)
    put(conn, "ps1-p1-dp", [(35, 4.1)])
    ch = alerts.evaluate(conn, None, T0 + 2100)
    assert ch["closed"] and "limit_low:ps1-p1-dp" not in open_alerts(conn)
    ended = conn.execute("SELECT end_ms FROM alerts WHERE id=?", (a["id"],)).fetchone()[0]
    assert ended == (T0 + 1800) * 1000                                   # when it came back inside


def test_criticality_moves_severity():
    tmp, cfg, conn = setup_live()
    model.save(conn, "site", {"key": "ps1", "criticality": 3}, ACTOR)
    put(conn, "ps1-p1-dp", [(0, 1.0), (5, 1.0)])                       # alarm limit -> critical, lowered to warning
    alerts.evaluate(conn, None, T0 + 300)
    a = open_alerts(conn)["limit_low:ps1-p1-dp"]
    assert a["severity"] == "warning" and a["adjusted"] == "lowered: criticality 3"


def test_silent_sensor_and_silent_site():
    tmp, cfg, conn = setup_live()
    put(conn, "ps1-p1-dp", [(0, 4.0), (5, 4.0)])
    put(conn, "ps1-flow", [(0, 100), (5, 100), (60, 100)])
    put(conn, "ps1-wl", [(0, 1.0), (5, 1.0), (60, 1.0)])
    alerts.evaluate(conn, None, T0 + 3600)
    a = open_alerts(conn)
    assert list(a) == ["silent:ps1-p1-dp"]
    assert "No readings from Pump 1 discharge pressure" in a["silent:ps1-p1-dp"]["message"] and "every 5 min" in a["silent:ps1-p1-dp"]["message"]
    alerts.evaluate(conn, None, T0 + 3 * 3600)                        # now everything is quiet: one site alert
    a = open_alerts(conn)
    assert list(a) == ["site_silent:ps1"]
    s = a["site_silent:ps1"]
    assert s["severity"] == "critical"                                 # a criticality-1 site going silent
    assert "Nothing has been heard from Riverside pumping station" in s["message"] and "all 3 of its sensors" in s["message"]


def test_overflow_levels():
    tmp, cfg, conn = setup_live()
    # floor -1.5, overflow 2.0, sensor zero at -1.3: a reading of 3.3 m is at the overflow
    put(conn, "ps1-wl", [(0, 3.0), (5, 3.05)])                         # level 1.75: within 0.35 m of overflow
    alerts.evaluate(conn, None, T0 + 300)
    a = open_alerts(conn)
    assert a["near_overflow:ps1-wl"]["severity"] == "warning"
    assert "within 0.35 m of the overflow level" in a["near_overflow:ps1-wl"]["message"]
    put(conn, "ps1-wl", [(10, 3.35)])
    alerts.evaluate(conn, None, T0 + 600)
    a = open_alerts(conn)
    assert "near_overflow:ps1-wl" not in a and a["over_overflow:ps1-wl"]["severity"] == "critical"
    assert "It may be spilling" in a["over_overflow:ps1-wl"]["message"]


def test_tank_low():
    tmp, cfg, conn = setup_live()
    model.save(conn, "site", {"key": "t1", "system": "dw", "name": "Hill tank", "site_type": "tank", "lat": 45.8, "lon": 7.72,
                              "ground_level_m": 50, "floor_level_m": 50, "overflow_level_m": {"ref": "above_floor", "value": 5}}, ACTOR)
    model.save(conn, "sensor", {"key": "t1-l", "site": "t1", "name": "Hill tank level", "quantity": "level", "source": "manual",
                                "mount_level_m": {"ref": "above_floor", "value": 0}}, ACTOR)
    model.go_live(conn, "site", "t1", ACTOR, {"exports"})
    put(conn, "t1-l", [(0, 0.4), (5, 0.35)])
    alerts.evaluate(conn, None, T0 + 300)
    a = open_alerts(conn)["tank_low:t1-l"]
    assert a["message"].startswith("Hill tank is down to 0.35 m (7% full)")


def test_drafts_never_alert():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)                                                # still drafts
    put(conn, "ps1-p1-dp", [(0, 0.5), (5, 0.5)])
    alerts.evaluate(conn, None, T0 + 300)
    assert open_alerts(conn) == {}


def test_ack_and_shelve_are_signed():
    tmp, cfg, conn = setup_live()
    put(conn, "ps1-p1-dp", [(0, 1.0), (5, 1.0)])
    alerts.evaluate(conn, None, T0 + 300)
    aid = open_alerts(conn)["limit_low:ps1-p1-dp"]["id"]
    alerts.acknowledge(conn, aid, "Duty Engineer", "crew on the way")
    with pytest.raises(ValueError, match="say why"):
        alerts.shelve(conn, aid, "Duty Engineer", 4, "")
    with pytest.raises(ValueError, match="up to 7 days"):
        alerts.shelve(conn, aid, "Duty Engineer", 500, "planned works")
    alerts.shelve(conn, aid, "Duty Engineer", 4, "planned works on the main")
    assert "limit_low:ps1-p1-dp" not in {a["rule"] + ":" + a["sensor"] for a in alerts.active(conn)}
    assert any(a["id"] == aid for a in alerts.active(conn, include_shelved=True))
    log = logchain.entries(conn)
    assert [e["kind"] for e in log[-2:]] == ["ack", "shelve"] and log[-1]["actor"] == "Duty Engineer"
    assert "planned works on the main" in log[-1]["msg"] and logchain.verify(conn)["ok"]


def test_source_failing():
    tmp, cfg, conn = setup_live()
    from waterworks_desk import db as dbm
    for _ in range(3):
        dbm.record_import(conn, "exports", "x.csv", False, message="the file is empty")
    alerts.evaluate(conn, cfg, T0)
    a = [x for x in alerts.active(conn, now_ms=T0 * 1000) if x["rule"] == "source_failing"][0]
    assert "The data source exports has failed 3 times in a row" in a["message"] and "the file is empty." in a["message"]


def test_changes_to_live_items_wait_until_applied():
    tmp, cfg, conn = setup_live()
    res = model.propose(conn, "sensor", {"key": "ps1-p1-dp", "warn_lo": 2.5}, "Minh Tran", {"exports"})
    assert res["pending"]
    live = conn.execute("SELECT warn_lo FROM sensors WHERE key='ps1-p1-dp'").fetchone()[0]
    assert live == pytest.approx(2.0 * 10.19716, rel=1e-5)               # unchanged until applied
    p = model.pending(conn)
    assert p[0]["key"] == "ps1-p1-dp" and p[0]["by"] == "Minh Tran"
    with pytest.raises(model.SetupError):                               # a bad change is refused up front
        model.propose(conn, "sensor", {"key": "ps1-p1-dp", "warn_lo": 99}, "Minh Tran", {"exports"})
    model.apply_pending(conn, "sensor", "ps1-p1-dp", "Lan Pham", {"exports"})
    assert conn.execute("SELECT warn_lo FROM sensors WHERE key='ps1-p1-dp'").fetchone()[0] == pytest.approx(2.5 * 10.19716, rel=1e-5)
    assert model.pending(conn) == []
    e = logchain.entries(conn)
    assert "proposed by Minh Tran" in e[-1]["msg"] and e[-1]["actor"] == "Lan Pham"
    it = model.item(conn, "sensor", "ps1-p1-dp")
    assert it["warn_lo"] == pytest.approx(2.5) and it["limits_unit"] == "bar"


def test_same_problem_back_within_a_day_reopens_its_alert():
    tmp, cfg, conn = setup_live()
    put(conn, "ps1-p1-dp", [(0, 1.8), (5, 1.8)])                       # warning opens at 00:00
    alerts.evaluate(conn, None, T0 + 300)
    first = open_alerts(conn)["limit_low:ps1-p1-dp"]
    put(conn, "ps1-p1-dp", [(10, 4.0), (15, 4.0)])                     # clears
    alerts.evaluate(conn, None, T0 + 900)
    assert open_alerts(conn) == {}
    put(conn, "ps1-p1-dp", [(600, 1.8), (605, 1.8)])                   # ten hours later, the same dip
    ch = alerts.evaluate(conn, None, T0 + 605 * 60)
    again = open_alerts(conn)["limit_low:ps1-p1-dp"]
    assert again["id"] == first["id"] and again["returns"] == 1 and ch["opened"][0]["returned"] == 1
    assert again["start_ms"] == first["start_ms"] and again["last_clear_ms"] == (T0 + 600) * 1000
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    put(conn, "ps1-p1-dp", [(610, 4.0), (615, 4.0)])
    alerts.evaluate(conn, None, T0 + 615 * 60)
    put(conn, "ps1-p1-dp", [(3000, 1.8), (3005, 1.8)])                 # two days later: a new alert
    alerts.evaluate(conn, None, T0 + 3005 * 60)
    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2
