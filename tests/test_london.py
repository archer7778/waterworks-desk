"""London: real Thames Water overflow monitors (via Stream) and Environment Agency rain gauges,
from small saved samples in tests/fixtures. Spill and monitor-offline alerts; each system judged
at its own clock in demo mode."""
import json
import sys

from helpers import ROOT, make_env

from waterworks_desk import alerts, db as dbm, ingest, model
from waterworks_desk.sources import ea_rain, stream_edm

sys.path.insert(0, str(ROOT / "tools"))
import london_setup  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
SRC = f"""
[[sources]]
key = "thames"
type = "stream_edm"
path = "{FIX / 'thames-overflows.json'}"
[[sources]]
key = "ea"
type = "ea_rain"
"""


def setup():
    tmp, cfg, conn = make_env(SRC)
    over = json.loads((FIX / "thames-overflows.json").read_text())
    rain = json.loads((FIX / "ea-rain-stations.json").read_text())
    b = london_setup.build(over, rain)
    res = model.import_bundle(conn, b, "Test", {"thames", "ea"}, make_live=True)
    return cfg, conn, over, res


def test_setup_from_the_feeds():
    cfg, conn, over, res = setup()
    assert res["counts"] == {"region": 1, "system": 2, "site": 3 + 4, "sensor": 3 + 4}   # one duplicate gauge dropped
    s = conn.execute("SELECT * FROM sites WHERE site_type='overflow_weir' ORDER BY key LIMIT 1").fetchone()
    a = next(f["attributes"] for f in over["features"] if f"twl-{f['attributes']['Id'].lower()}" == s["key"])
    assert (round(s["lat"], 5), round(s["lon"], 5)) == (round(a["Latitude"], 5), round(a["Longitude"], 5))
    assert conn.execute("SELECT basemap FROM regions WHERE key='london'").fetchone()[0] == 1


def test_overflow_readings_and_alerts():
    cfg, conn, over, res = setup()
    cfg.sources = [s for s in cfg.sources if s["key"] == "thames"]
    ingest.run(cfg, conn, log=lambda m: None)
    spill = over["features"][1]["attributes"]
    sid = conn.execute("SELECT id FROM sensors WHERE source_tag=?", (spill["Id"],)).fetchone()[0]
    rows = conn.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? ORDER BY ts", (sid,)).fetchall()
    assert (spill["LatestEventStart"] // 1000, 1.0) in [(r[0], r[1]) for r in rows]
    assert (spill["LatestEventEnd"] // 1000, 0.0) in [(r[0], r[1]) for r in rows]
    open_now = {a["rule"]: a for a in alerts.active(conn, now_ms=10**13)}
    off = open_now["monitor_offline"]
    assert off["severity"] == "advisory" and "has been offline since" in off["message"]
    # during the spill, the overflow alerts as discharging into its river
    alerts.evaluate(conn, None, spill["LatestEventStart"] // 1000 + 60)
    sp = [a for a in alerts.active(conn, now_ms=10**13) if a["rule"] == "spilling"]
    assert sp and sp[0]["severity"] == "warning"
    assert f"is discharging into {spill['ReceivingWaterCourse']}" in sp[0]["message"]


def test_rain_readings_parse():
    items = [{"dateTime": "2026-11-02T07:15:00Z", "value": 0.4,
              "measure": "http://environment.data.gov.uk/flood-monitoring/id/measures/287283TP-rainfall-tipping_bucket_raingauge-t-15_min-mm"},
             {"dateTime": "2026-11-02T07:30:00Z", "value": [0.2, 0.2],
              "measure": "http://environment.data.gov.uk/flood-monitoring/id/measures/E8290-rainfall-tipping_bucket_raingauge-t-15_min-mm"},
             {"dateTime": "bad", "value": 1, "measure": "x"}]
    got = list(ea_rain.parse_items(items))
    assert got == [("287283TP", 1793603700, 0.4, 0), ("E8290", 1793604600, 0.2, 0)]
    assert ea_rain.station_of(items[0]["measure"]) == "287283TP"


def test_each_system_judged_at_its_own_clock():
    cfg, conn, over, res = setup()
    cfg.data["alerts"]["clock"] = "data_end"
    cfg.sources = [s for s in cfg.sources if s["key"] == "thames"]
    ingest.run(cfg, conn, log=lambda m: None)
    # an old test network beside it, ending years earlier
    model.save(conn, "system", {"key": "old", "region": "london", "name": "Old test system", "network_type": "drinking_water"}, "T")
    model.save(conn, "site", {"key": "o1", "system": "old", "name": "Old site", "site_type": "tank", "lat": 51.5, "lon": -0.1}, "T")
    model.save(conn, "sensor", {"key": "o1-l", "site": "o1", "name": "Level", "quantity": "level", "source": "manual",
                                "expected_interval_s": 300}, "T")
    model.go_live(conn, "system", "old", "T")
    oid = conn.execute("SELECT id FROM sensors WHERE key='o1-l'").fetchone()[0]
    ingest.store(conn, "manual", [("x", 1_546_300_800 + i * 300, 1.0, 0) for i in range(3)], {"x": (oid, lambda v: v)})
    conn.commit()
    clocks = alerts.system_clocks(cfg, conn)
    old_id = conn.execute("SELECT id FROM systems WHERE key='old'").fetchone()[0]
    assert clocks[old_id] == 1_546_300_800 + 600
    alerts.evaluate(conn, cfg)
    assert not [a for a in alerts.active(conn, now_ms=10**13) if a["rule"] == "silent"]   # not silent at its own clock


def test_spill_alert_ends_when_the_monitor_said_it_stopped():
    cfg, conn, over, res = setup()
    cfg.sources = [s for s in cfg.sources if s["key"] == "thames"]
    ingest.run(cfg, conn, log=lambda m: None)
    spill = over["features"][1]["attributes"]
    start, stop = spill["LatestEventStart"] // 1000, spill["LatestEventEnd"] // 1000
    alerts.evaluate(conn, None, start + 60)
    alerts.evaluate(conn, None, stop + 3 * 3600)          # we only look again three hours after it stopped
    a = [x for x in alerts.active(conn, now_ms=(stop + 4 * 3600) * 1000, include_ended_hours=24) if x["rule"] == "spilling"][0]
    assert a["end_ms"] == stop * 1000


def test_on_off_readings_carry_in_their_state():
    from waterworks_desk import server
    cfg, conn, over, res = setup()
    cfg.sources = [s for s in cfg.sources if s["key"] == "thames"]
    ingest.run(cfg, conn, log=lambda m: None)
    spill = over["features"][1]["attributes"]
    app = server.App(cfg)
    key = f"twl-{spill['Id'].lower()}-edm"
    mid = (spill["LatestEventStart"] + spill["LatestEventEnd"]) // 2000
    r = app.readings(key, mid, mid + 60, 3600)             # a window inside the spill, asking for averages
    assert r["res"] == "raw" and r["prev"][1] == 1.0       # raw anyway, and it knows it was spilling
