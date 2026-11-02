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

import ltown_setup  # noqa: E402

from waterworks_desk import alerts, export_static, ingest, model  # noqa: E402
from waterworks_desk import db as dbm  # noqa: E402
from waterworks_desk.config import Config  # noqa: E402

ACTOR = "Demo build"
ORIGIN = (35.0, 33.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, help="folder with L-TOWN.inp and the SCADA CSVs")
    ap.add_argument("--out", default="site")
    ap.add_argument("--work", default="demo", help="working folder for the demo database")
    ap.add_argument("--no-weather", action="store_true", help="skip Open-Meteo (no network)")
    ap.add_argument("--step", type=int, default=3600, help="seconds between alert replay steps")
    a = ap.parse_args(argv)
    t0 = time.time()
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
title = "Waterworks Desk · L-Town demo"

[map]
tile_url = ""        # L-Town is at a stand-in location: no real streets under it

[alerts]
clock = "data_end"

[[credits]]
text = "BattLeDIM L-Town, Vrachimis et al. 2020, CC BY 4.0"
url = "https://doi.org/10.5281/zenodo.4017659"
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
    alerts.replay(conn, start, end, a.step, log=lambda m: print("   ", m))
    alerts.evaluate(conn, cfg)
    n = dict(conn.execute("SELECT severity, COUNT(*) FROM alerts GROUP BY 1").fetchall())
    print(f"3/4 Put {res['live']} items live, read 2019 and replayed it: {n}")

    export_static.export(cfg, a.out, log=lambda m: print("4/4", m))
    print(f"Done in {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    main()
