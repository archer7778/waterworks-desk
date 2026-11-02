"""Build the public L-Town demo end to end, with the same code a real installation runs.

    python tools/build_demo.py --data path/to/battledim --out site [--no-weather]

1. Set up L-Town from its EPANET model (tools/ltown_setup.py), plus a weather site.
2. Read 2018's SCADA files, then give each flow, pressure and tank sensor demo limits from its
   2018 readings: a warning just outside anything seen in 2018 (the lowest and highest values,
   widened by 5 % of the year's range), an alarm further out (15 %). They are labelled as demo
   limits: a utility sets its own.
3. Put everything live, read 2019, and replay 2019 hour by hour so the alerts it would have
   raised are in the history. The console is then judged as at the end of the data.
4. Export the read-only static console (waterworks_desk/export_static.py).

The data folder needs L-TOWN.inp and the 2018 and 2019 SCADA Flows, Levels and Pressures CSVs
from Zenodo (10.5281/zenodo.4017659, CC BY 4.0).
"""
import argparse
import json
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import london_setup  # noqa: E402
import ltown_setup  # noqa: E402

from waterworks_desk import alerts, export_static, ingest, model  # noqa: E402
from waterworks_desk.sources import openmeteo, stream_edm  # noqa: E402
from waterworks_desk import db as dbm  # noqa: E402
from waterworks_desk.config import Config  # noqa: E402

ACTOR = "Demo build"
THAMES = ("https://services2.arcgis.com/g6o32ZDQ33GpCIu3/arcgis/rest/services/"
          "Thames_Water_Storm_Overflow_Activity_(Production)_view/FeatureServer/0")
EA_STATIONS = "https://environment.data.gov.uk/flood-monitoring/id/stations?parameter=rainfall&lat=51.5&long=-0.12&dist=20"
ORIGIN = (35.0, 33.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, help="folder with L-TOWN.inp and the SCADA CSVs")
    ap.add_argument("--out", default="site")
    ap.add_argument("--work", default="demo", help="working folder for the demo database")
    ap.add_argument("--no-weather", action="store_true", help="skip Open-Meteo (no network)")
    ap.add_argument("--step", type=int, default=3600, help="seconds between alert replay steps")
    ap.add_argument("--london-overflows", default=THAMES, help="Thames overflow layer URL, or a saved query (JSON file)")
    ap.add_argument("--london-rain", default=EA_STATIONS, help="EA rainfall station list URL, or a saved list (JSON file)")
    ap.add_argument("--offline", action="store_true", help="no network: no weather and no live rain readings")
    ap.add_argument("--no-london", action="store_true")
    a = ap.parse_args(argv)
    t0 = time.time()
    if a.offline:
        a.no_weather = True
    london_live = not str(a.london_overflows).endswith(".json")
    data, work = Path(a.data).resolve(), Path(a.work).resolve()
    if work.exists():
        shutil.rmtree(work)
    (work / "data").mkdir(parents=True)
    bundle = ltown_setup.build(data / "L-TOWN.inp", *ORIGIN)
    c_lat, c_lon = bundle["regions"][0]["lat"], bundle["regions"][0]["lon"]
    weather = "" if a.no_weather else f"""
[[sources]]
key = "weather"
type = "open_meteo"
mode = "archive"
lat = {c_lat}
lon = {c_lon}
start = "2018-01-01"
end = "2019-12-31"
"""
    (work / "config.toml").write_text(f"""
[storage]
db = "data/demo.db"
raw_days = 0
minute_days = 0

[console]
title = "Waterworks Desk · demo"
default_system = "{'' if a.no_london else 'region:london'}"

[map]
tile_url = "https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png"   # L-Town's region turns it off: no real location
attribution = "© OpenStreetMap contributors"

[alerts]
clock = "data_end"

[[credits]]
text = "BattLeDIM L-Town, Vrachimis et al. 2020, CC BY 4.0"
url = "https://doi.org/10.5281/zenodo.4017659"
{'' if a.no_london else '''
[[credits]]
text = "Storm overflows: Thames Water via Stream, CC BY 4.0"
url = "https://portal-streamwaterdata.hub.arcgis.com/datasets/thameswater::thames-water-storm-overflow-activity"

[[credits]]
text = "Rainfall: Environment Agency real-time data API (Beta), Open Government Licence v3"
url = "https://environment.data.gov.uk/flood-monitoring/doc/reference"
'''}
{'' if a.no_weather else '''
[[credits]]
text = "Weather data by Open-Meteo.com, CC BY 4.0 (at the stand-in location)"
url = "https://open-meteo.com/"
'''}
[[sources]]
key = "ltown"
type = "csv"
path = "{data}"
pattern = "2018_SCADA_[FLP]*.csv"
timezone = "UTC"
settle_seconds = 0
{weather}
{'' if a.no_london else f'''
[[sources]]
key = "thames"
type = "stream_edm"
{"url" if london_live else "path"} = "{a.london_overflows if london_live else Path(a.london_overflows).resolve()}"
min_minutes = 0
'''}
{'' if a.no_london else '''
[[sources]]
key = "ea"
type = "ea_rain"
'''}
""")
    cfg = Config(work / "config.toml")
    conn = dbm.open_db(cfg)
    known = {s["key"] for s in cfg.sources}
    if not a.no_weather:
        bundle["sites"].append({"key": "ltown-weather", "system": "ltown-dw", "name": "Weather (Open-Meteo, stand-in location)",
                                "site_type": "rain_gauge", "lat": c_lat, "lon": c_lon, "criticality": 3})
        bundle["sensors"] += [
            {"key": "ltown-weather-rain", "site": "ltown-weather", "name": "Rainfall", "quantity": "rain", "source": "weather",
             "source_tag": "precipitation", "source_unit": "mm", "expected_interval_s": 3600},
            {"key": "ltown-weather-temp", "site": "ltown-weather", "name": "Air temperature", "quantity": "temperature",
             "source": "weather", "source_tag": "temperature_2m", "source_unit": "degC", "expected_interval_s": 3600}]
    model.import_bundle(conn, bundle, ACTOR, known, source_name="tools/ltown_setup.py")
    print("1/4 Set up L-Town:", len(bundle["sites"]), "sites,", len(bundle["sensors"]), "sensors")

    ltown_only = [s for s in cfg.sources if s["key"] in ("ltown", "weather")]
    london_sources = [s for s in cfg.sources if s["key"] in (("thames",) if a.offline else ("thames", "ea"))]
    cfg.sources = ltown_only
    ingest.run(cfg, conn, log=lambda m: print("   ", m))
    for x in conn.execute("SELECT * FROM sensors WHERE source='ltown'").fetchall():
        lo, hi = conn.execute("SELECT MIN(value), MAX(value) FROM readings WHERE sensor_id=?", (x["id"],)).fetchone()
        span = max(hi - lo, 0.1)
        lim = {"key": x["key"], "limits_unit": x["unit"],
               "warn_lo": round(lo - 0.05 * span, 2), "warn_hi": round(hi + 0.05 * span, 2),
               "alarm_lo": round(lo - 0.15 * span, 2), "alarm_hi": round(hi + 0.15 * span, 2),
               "note": "Demo limits: just outside anything this sensor read in 2018. Not a utility's limits."}
        if x["quantity"] == "flow":   # flows can't go below zero
            lim["warn_lo"], lim["alarm_lo"] = max(lim["warn_lo"], 0) or None, None
        if x["quantity"] == "level":   # the tank has its own low-level rule; keep the band clear of the floor
            lim["warn_lo"] = max(lim["warn_lo"] or 0, 0.5)
        if lim["alarm_lo"] is not None and x["range_lo"] is not None:
            lim["alarm_lo"] = max(lim["alarm_lo"], x["range_lo"])
        if lim["alarm_hi"] is not None and x["range_hi"] is not None:
            lim["alarm_hi"] = min(lim["alarm_hi"], x["range_hi"])
            lim["warn_hi"] = min(lim["warn_hi"], round(lim["alarm_hi"] - 0.05 * span, 2))
        model.save(conn, "sensor", lim, ACTOR, known)
    print("2/4 Read 2018 and set demo limits from it")

    res = model.go_live(conn, "system", "ltown-dw", ACTOR, known)
    for s in cfg.sources:
        if s["key"] == "ltown":
            s["pattern"] = "2019_SCADA_[FLP]*.csv"
    ingest.run(cfg, conn, log=lambda m: print("   ", m))
    conn.execute("DELETE FROM alerts")      # the live run judged 2019's end; rebuild the year properly
    conn.commit()
    start = conn.execute("SELECT MIN(ts) FROM readings WHERE ts>=?", (1546300800,)).fetchone()[0]
    end = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    alerts.replay(conn, start, end, a.step, log=lambda m: print("   ", m), system="ltown-dw")
    n = dict(conn.execute("SELECT severity, COUNT(*) FROM alerts GROUP BY 1").fetchall())
    print(f"3/4 Put {res['live']} items live, read 2019 and replayed it: {n}")

    if not a.no_london:
        load = lambda src: openmeteo.get_json(src) if str(src).startswith("http") else json.load(open(src, encoding="utf-8"))
        over = {"features": stream_edm.fetch(a.london_overflows)} if london_live else load(a.london_overflows)
        lb = london_setup.build(over, load(a.london_rain))
        model.import_bundle(conn, lb, ACTOR, known, make_live=True, source_name="tools/london_setup.py")
        cfg.sources = london_sources
        ingest.run(cfg, conn, force=True, log=lambda m: print("   ", m))
        conn.execute("DELETE FROM alerts WHERE system_id IN (SELECT id FROM systems WHERE region_id=(SELECT id FROM regions WHERE key='london'))")
        conn.commit()
        lend = conn.execute("SELECT MAX(r.ts) FROM readings r JOIN sensors x ON x.id=r.sensor_id WHERE x.source='thames'").fetchone()[0]
        for sk in ("thames-overflows", "london-rain"):
            alerts.replay(conn, lend - 7 * 86400, lend, 900, system=sk)
        cfg.sources = ltown_only + london_sources
        n = dict(conn.execute("SELECT rule, COUNT(*) FROM alerts a JOIN systems y ON y.id=a.system_id WHERE y.key='thames-overflows' GROUP BY 1").fetchall())
        print(f"   London: {len(lb['sites'])} sites ({'live feed' if london_live else 'saved snapshot'}); last 7 days: {n}")
    alerts.evaluate(conn, cfg)

    export_static.export(cfg, a.out, log=lambda m: print("4/4", m))
    print(f"Done in {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    main()
