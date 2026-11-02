"""Build the public London demo end to end, with the same code a real installation runs.

    python tools/build_demo.py --out site                    # from the live feeds
    python tools/build_demo.py --out site --offline \\
        --overflows thames-overflows.json --rain ea-rain-stations.json   # from saved copies

1. Set up London from Thames Water's storm overflow feed (via Stream) and the Environment
   Agency's rainfall stations (tools/london_setup.py), and put it live.
2. Read the overflow monitors and the rain gauges.
3. Replay the last week every 15 minutes so recent spills are in the alert history; the console
   is then judged as at the newest reading.
   With --keep, the database from the last build is kept and only the time since then is
   replayed. Stream's feed gives each overflow's latest spill only, so keeping the database
   between runs is what builds up the months of spill history the Spills page counts.
4. Export the read-only static console (waterworks_desk/export_static.py).

Reliability: a feed that can't be reached falls back to the last setup that worked; a kept
database that is damaged is started again; and before anything is published the data is
checked (enough overflow monitors reporting, newest reading not too old). If the check
fails nothing is published, so the last good demo stays up, and on GitHub Actions the step
says why (publish=false in its outputs).

Data: Thames Water storm overflow activity via Stream (CC BY 4.0); Environment Agency rainfall
(Open Government Licence v3); map © OpenStreetMap contributors.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import london_setup  # noqa: E402

from waterworks_desk import alerts, export_static, ingest, model  # noqa: E402
from waterworks_desk import db as dbm  # noqa: E402
from waterworks_desk.config import Config  # noqa: E402

ACTOR = "Demo build"


MIN_REPORTING = 0.5     # at least half the overflow monitors must have a reading
MAX_AGE_H = 24          # and the newest overflow reading must be less than a day old


def load_setup(a, work):
    """The London setup from the live feeds, or the last one that worked if a feed is down."""
    saved = work / "data" / "last-setup.json"
    try:
        bundle = london_setup.build(london_setup.load_overflows(a.overflows), london_setup.load(a.rain))
        if not any(s["site_type"] == "overflow_weir" for s in bundle["sites"]):
            raise ValueError("the overflow feed returned no overflows")
        saved.write_text(json.dumps(bundle), encoding="utf-8")
        return bundle, None
    except Exception as e:
        if not saved.exists():
            raise
        return json.loads(saved.read_text(encoding="utf-8")), f"{type(e).__name__}: {e}"[:200]


def check(conn, now=None):
    """Problems that should stop a publish: [] when the data looks sound."""
    now = now or time.time()
    n, reporting, newest = conn.execute(
        "SELECT COUNT(*), SUM((SELECT COUNT(*) FROM readings r WHERE r.sensor_id=x.id) > 0), "
        "MAX((SELECT MAX(ts) FROM readings r WHERE r.sensor_id=x.id)) FROM sensors x WHERE x.source='thames' AND x.status='live'").fetchone()
    out = []
    if not n:
        out.append("no overflow monitors are set up")
    elif (reporting or 0) < MIN_REPORTING * n:
        out.append(f"only {reporting or 0} of {n} overflow monitors have any reading")
    if newest is None or now - newest > MAX_AGE_H * 3600:
        out.append("the newest overflow reading is " + ("missing" if newest is None else f"{(now - newest) / 3600:.0f} hours old"))
    return out


def say_publish(ok, why=""):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"publish={'true' if ok else 'false'}\n")
    if not ok:
        print(f"::warning title=Demo not published::{why}" if os.environ.get("GITHUB_ACTIONS") else f"Not published: {why}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default="site")
    ap.add_argument("--work", default="demo", help="working folder for the demo database")
    ap.add_argument("--overflows", default=london_setup.THAMES, help="overflow layer URL, or a saved query (JSON file)")
    ap.add_argument("--rain", default=london_setup.EA_STATIONS, help="rainfall station list URL, or a saved list (JSON file)")
    ap.add_argument("--offline", action="store_true", help="no network: skip live rain readings")
    ap.add_argument("--keep", action="store_true", help="keep the database from the last build and add to it")
    a = ap.parse_args(argv)
    t0 = time.time()
    work = Path(a.work).resolve()
    kept = a.keep and (work / "data" / "demo.db").exists()
    if kept:   # a damaged database from the cache would fail every build from now on: start again instead
        try:
            ok = sqlite3.connect(work / "data" / "demo.db").execute("PRAGMA quick_check").fetchone()[0] == "ok"
        except sqlite3.DatabaseError:
            ok = False
        if not ok:
            print("The kept database is damaged; starting a fresh history.")
            saved = work / "data" / "last-setup.json"
            keep_setup = saved.read_bytes() if saved.exists() else None
            shutil.rmtree(work)
            (work / "data").mkdir(parents=True)
            if keep_setup:
                saved.write_bytes(keep_setup)
            kept = False
    if work.exists() and not kept:
        saved = work / "data" / "last-setup.json"
        keep_setup = saved.read_bytes() if saved.exists() else None
        shutil.rmtree(work)
        (work / "data").mkdir(parents=True)
        if keep_setup:
            saved.write_bytes(keep_setup)
    (work / "data").mkdir(parents=True, exist_ok=True)
    live = str(a.overflows).startswith("http")
    (work / "config.toml").write_text(f"""
[storage]
db = "data/demo.db"
raw_days = 0
minute_days = 0

[console]
title = "Waterworks Desk · London demo"

[map]
tile_url = "https://tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png"
attribution = "© OpenStreetMap contributors"

[alerts]
clock = "data_end"

[[credits]]
text = "Storm overflows: Thames Water via Stream, CC BY 4.0"
url = "https://portal-streamwaterdata.hub.arcgis.com/datasets/thameswater::thames-water-storm-overflow-activity"

[[credits]]
text = "Rainfall: Environment Agency real-time data API (Beta), Open Government Licence v3"
url = "https://environment.data.gov.uk/flood-monitoring/doc/reference"

[[sources]]
key = "thames"
type = "stream_edm"
{"url" if live else "path"} = "{a.overflows if live else Path(a.overflows).resolve()}"
min_minutes = 0

[[sources]]
key = "ea"
type = "ea_rain"
""")
    cfg = Config(work / "config.toml")
    conn = dbm.open_db(cfg)
    known = {s["key"] for s in cfg.sources}
    bundle, fell_back = load_setup(a, work)
    if fell_back:
        print(f"    A feed couldn't be read ({fell_back}); using the last setup that worked.")
    model.import_bundle(conn, bundle, ACTOR, known, make_live=True, source_name="tools/london_setup.py")
    print(f"1/4 Set up London: {len(bundle['sites'])} sites ({'live feeds' if live else 'saved copies'})"
          + (", added to the last build's history" if kept else ""))

    if a.offline:
        cfg.sources = [s for s in cfg.sources if s["key"] == "thames"]
    ingest.run(cfg, conn, force=True, log=lambda m: print("   ", m))
    print("2/4 Read the overflow monitors" + ("" if a.offline else " and the rain gauges"))

    end = conn.execute("SELECT MAX(r.ts) FROM readings r JOIN sensors x ON x.id=r.sensor_id WHERE x.source='thames'").fetchone()[0]
    if end is None:
        say_publish(False, "there are no overflow readings at all. The last good demo stays up.")
        print(f"Done in {time.time() - t0:.0f}s (not exported).")
        return 0
    done = dbm.meta_get(conn, "demo:replayed_to") if kept else None
    if done and end - 7 * 86400 <= done <= end:
        start = done - 3 * 3600    # a little overlap: rain and spill readings can arrive late
    else:
        conn.execute("DELETE FROM alerts")
        conn.commit()
        start = end - 7 * 86400
    for system in ("thames-overflows", "london-rain"):
        alerts.replay(conn, start, end, 900, system=system)
    alerts.evaluate(conn, cfg)
    dbm.meta_set(conn, "demo:replayed_to", end)
    conn.commit()
    n = dict(conn.execute("SELECT rule, COUNT(*) FROM alerts GROUP BY 1").fetchall())
    print(f"3/4 Replayed {(end - start) / 3600:.0f} hours: {n}")

    problems = check(conn, None if live else end)
    if problems:
        say_publish(False, "; ".join(problems) + ". The last good demo stays up.")
        print(f"Done in {time.time() - t0:.0f}s (not exported).")
        return 0
    export_static.export(cfg, a.out, log=lambda m: print("4/4", m))
    say_publish(True)
    print(f"Done in {time.time() - t0:.0f}s.")


if __name__ == "__main__":
    main()
