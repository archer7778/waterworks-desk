"""Storage load test: does SQLite keep up with a mid-sized town, and how much disk does it need?

Simulates live ingestion the way it really arrives: every cycle (default 5 minutes of data),
every sensor reports one reading per interval, interleaved across sensors, followed by the
rollups for what just arrived. Then measures disk use per reading, cycle times and the
queries the console makes, and projects a year under the retention policy.

    python tools/loadtest.py --sensors 3000 --interval 60 --days 1 --db /tmp/load.db
"""
import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from waterworks_desk import db as dbm  # noqa: E402
from waterworks_desk import ingest  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--sensors", type=int, default=3000)
    ap.add_argument("--interval", type=int, default=60, help="seconds between readings per sensor")
    ap.add_argument("--days", type=float, default=1)
    ap.add_argument("--cycle", type=int, default=300, help="seconds of data per ingest run")
    ap.add_argument("--raw-days", type=int, default=90)
    ap.add_argument("--db", default="data/loadtest.db")
    a = ap.parse_args(argv)
    path = Path(a.db)
    for suf in ("", "-wal", "-shm"):
        Path(str(path) + suf).unlink(missing_ok=True)
    conn = dbm.connect(path)
    t_ms = dbm.now_ms()
    conn.execute("INSERT INTO regions(key,name,created_ms) VALUES('r','Load test',?)", (t_ms,))
    conn.execute("INSERT INTO systems(key,region_id,name,network_type,created_ms,updated_ms) VALUES('s',1,'S','drinking_water',?,?)", (t_ms, t_ms))
    conn.execute("INSERT INTO sites(key,system_id,name,site_type,lat,lon,created_ms,updated_ms) VALUES('site',1,'Site','other',0,0,?,?)", (t_ms, t_ms))
    conn.executemany("INSERT INTO sensors(key,site_id,name,quantity,unit,source,source_tag,expected_interval_s,created_ms,updated_ms)"
                     " VALUES(?,1,?,'pressure','m','load',?,?,?,?)",
                     [(f"s{i}", f"Sensor {i}", f"T{i}", a.interval, t_ms, t_ms) for i in range(a.sensors)])
    conn.commit()
    smap = ingest.sensor_map(conn, "load")
    start = int(time.time()) - int(a.days * 86400)
    start -= start % a.cycle
    rnd = random.Random(1)
    base = [rnd.uniform(20, 60) for _ in range(a.sensors)]
    cycles, worst, total, t0 = 0, 0.0, 0, time.time()
    steps = int(a.days * 86400 / a.cycle)
    for c in range(steps):
        c0 = time.time()
        cs = start + c * a.cycle

        def gen():
            for ts in range(cs, cs + a.cycle, a.interval):
                day = math.sin(2 * math.pi * (ts % 86400) / 86400)
                for i in range(a.sensors):
                    yield f"T{i}", ts, base[i] + 3 * day + rnd.gauss(0, 0.2), 0
        stored, _, spans = ingest.store(conn, "load", gen(), smap)
        ingest.rollup(conn, spans)
        conn.commit()
        total += stored
        cycles += 1
        worst = max(worst, time.time() - c0)
    elapsed = time.time() - t0
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    sizes = {}
    try:
        sizes = {n: s for n, s in conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name")}
    except Exception:
        pass
    raw_b = sizes.get("readings", path.stat().st_size)
    roll_b = sizes.get("rollups", 0)
    per_reading = raw_b / total
    per_day = total / a.days
    # queries the console makes
    sid = smap["T0"][0]
    q = {}
    t = time.time()
    rows = conn.execute("SELECT ts, value FROM readings WHERE sensor_id=? AND ts BETWEEN ? AND ? ORDER BY ts",
                        (sid, start, start + 86400)).fetchall()
    q["one sensor, one day raw"] = (time.time() - t, len(rows))
    t = time.time()
    rows = conn.execute("SELECT ts, mean FROM rollups WHERE res=900 AND sensor_id=? AND ts BETWEEN ? AND ?",
                        (sid, start, start + 86400 * 400)).fetchall()
    q["one sensor, all 15-min rollups"] = (time.time() - t, len(rows))
    t = time.time()
    n = conn.execute("SELECT COUNT(*) FROM sensors s WHERE (SELECT ts FROM readings r WHERE r.sensor_id=s.id ORDER BY ts DESC LIMIT 1) IS NOT NULL").fetchone()[0]
    q[f"latest reading of all {n} sensors"] = (time.time() - t, n)
    roll_per_day = roll_b / a.days
    year_raw = per_reading * per_day * min(a.raw_days or 365, 365)
    year_roll = roll_per_day * 365
    print(f"Simulated {a.sensors} sensors every {a.interval} s for {a.days:g} day(s): {total:,} readings in {cycles} ingest runs")
    print(f"  Ingest: {total / elapsed:,.0f} readings/s overall; slowest run {worst:.2f} s for {a.cycle} s of data "
          f"({a.sensors * a.cycle // a.interval:,} readings)")
    print(f"  Disk: {per_reading:.1f} bytes per raw reading; raw {raw_b / 1e6 / a.days:,.0f} MB/day, "
          f"rollups {roll_per_day / 1e6:,.0f} MB/day")
    print(f"  A year with raw kept {a.raw_days} days: raw {year_raw / 1e9:,.1f} GB + rollups {year_roll / 1e9:,.1f} GB "
          f"= {(year_raw + year_roll) / 1e9:,.1f} GB (plus the WAL and free pages)")
    for k, (s, n) in q.items():
        print(f"  Query, {k}: {s * 1000:.0f} ms ({n:,} rows)")


if __name__ == "__main__":
    main()
