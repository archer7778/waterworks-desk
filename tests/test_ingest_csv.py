"""End to end against mock CSV sources: wide and long layouts, decimal commas, time zones, unit
conversion, unmapped tags, bad rows, re-reads, rollups, retention and manual readings."""
import os
import time
from datetime import datetime, timezone

import pytest
from helpers import ACTOR, CSV_SOURCE, basic_network, make_env

from waterworks_desk import db as dbm
from waterworks_desk import ingest, units
from waterworks_desk.sources.csvfile import sniff, to_number


def write(path, text, age=60):
    path.write_text(text)
    t = time.time() - age
    os.utime(path, (t, t))


def utc(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def test_wide_csv_with_decimal_commas_and_local_time():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    # semicolons, decimal commas, local time (UTC+05:30), one unmapped tag, one bad value
    rows = ["Time;PS1_P1_PT;PS1_FT;PS1_LT;MYSTERY_TAG"]
    for i in range(120):   # two hours at 1-minute intervals, starting 07:00 local = 01:30 UTC
        hh, mm = 7 + i // 60, i % 60
        pt = f"{4 + i / 100:.2f}".replace(".", ",")
        rows.append(f"2026-11-02 {hh:02d}:{mm:02d}:00;{pt};{100 + i};1,5;9")
    rows[50] = rows[50].replace(";149;", ";oops;")
    write(tmp / "data/import/station.csv", "\n".join(rows) + "\n")
    n = ingest.run(cfg, conn, log=lambda m: None)
    imp = dict(conn.execute("SELECT * FROM imports ORDER BY id DESC").fetchone())
    assert imp["ok"] == 1 and imp["rows_seen"] == 120
    assert imp["skipped"] == 1 and "isn't a number" in imp["message"]
    assert "MYSTERY_TAG" in imp["message"] and "no sensor set up" in imp["message"]
    assert n == 120 * 3 - 1
    sid = conn.execute("SELECT id FROM sensors WHERE key='ps1-p1-dp'").fetchone()[0]
    first = conn.execute("SELECT ts, value FROM readings WHERE sensor_id=? ORDER BY ts LIMIT 1", (sid,)).fetchone()
    assert first[0] == utc("2026-11-02T01:30:00")                       # 07:00 at UTC+05:30
    assert first[1] == pytest.approx(4.0 * 10.19716, rel=1e-5)           # 4,00 bar -> metres head
    fid = conn.execute("SELECT id FROM sensors WHERE key='ps1-flow'").fetchone()[0]
    assert conn.execute("SELECT value FROM readings WHERE sensor_id=? ORDER BY ts LIMIT 1", (fid,)).fetchone()[0] == pytest.approx(360.0)  # 100 l/s
    # unmapped tags are remembered for the setup screen
    um = dbm.meta_get(conn, "unmapped:exports")
    assert um["MYSTERY_TAG"]["count"] == 120
    # rollups: hourly UTC buckets; two local hours at UTC+05:30 straddle three of them
    r = conn.execute("SELECT n, mean, lo, hi FROM rollups WHERE res=3600 AND sensor_id=? ORDER BY ts", (sid,)).fetchall()
    assert [x[0] for x in r] == [30, 60, 30]
    assert r[0][2] == pytest.approx(4.0 * 10.19716, rel=1e-5) and r[0][3] == pytest.approx(4.29 * 10.19716, rel=1e-5)
    # an unchanged file isn't read twice; a corrected one overwrites without duplicating
    assert ingest.run(cfg, conn, log=lambda m: None) == 0
    rows[1] = rows[1].replace("4,00", "4,10")
    write(tmp / "data/import/station.csv", "\n".join(rows) + "\n")
    ingest.run(cfg, conn, log=lambda m: None)
    assert conn.execute("SELECT COUNT(*) FROM readings WHERE sensor_id=?", (sid,)).fetchone()[0] == 120
    assert conn.execute("SELECT value FROM readings WHERE sensor_id=? ORDER BY ts LIMIT 1", (sid,)).fetchone()[0] == pytest.approx(41.8084, rel=1e-4)


def test_long_csv_with_quality_and_words():
    src = CSV_SOURCE.replace('layout = "wide"', 'layout = "long"').replace('"Asia/Kolkata"', '"UTC"')
    tmp, cfg, conn = make_env(src)
    basic_network(conn)
    model_state = """time,tag,value,quality
2026-11-02T00:00:00Z,PS1_LT,1.25,good
2026-11-02T00:05:00Z,PS1_LT,1.30,uncertain
2026-11-02T00:10:00Z,PS1_LT,1.35,bad
2026-11-02T00:15:00+07:00,PS1_LT,1.40,good
2099-01-01T00:00:00Z,PS1_LT,9.99,good
"""
    write(tmp / "data/import/levels.csv", model_state)
    ingest.run(cfg, conn, log=lambda m: None)
    sid = conn.execute("SELECT id FROM sensors WHERE key='ps1-wl'").fetchone()[0]
    rows = conn.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? ORDER BY ts", (sid,)).fetchall()
    assert [r[2] for r in rows] == [dbm.GOOD, dbm.GOOD, dbm.SUSPECT, dbm.BAD]
    assert rows[0][0] == utc("2026-11-01T17:15:00")                   # +07:00 offset honoured
    imp = conn.execute("SELECT skipped, message FROM imports ORDER BY id DESC").fetchone()
    assert imp[0] == 1 and "future" in imp[1]
    # bad readings are kept (with their flag) but left out of rollups
    r = conn.execute("SELECT n FROM rollups WHERE res=3600 AND sensor_id=? AND ts=?", (sid, utc("2026-11-02T00:00:00"))).fetchone()
    assert r[0] == 2


def test_a_broken_file_doesnt_stop_the_others():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    write(tmp / "data/import/a_good.csv", "Timestamp,PS1_FT\n2026-11-02 07:00:00,50\n")
    write(tmp / "data/import/b_empty.csv", "")
    write(tmp / "data/import/c_wrongtime.csv", "Timestamp,PS1_FT\nyesterday,50\n")
    ingest.run(cfg, conn, log=lambda m: None)
    imps = {r["item"]: dict(r) for r in conn.execute("SELECT * FROM imports")}
    assert imps["a_good.csv"]["readings_stored"] == 1
    assert imps["b_empty.csv"]["ok"] == 0 and "empty" in imps["b_empty.csv"]["message"]
    assert imps["c_wrongtime.csv"]["ok"] == 0 and "can't read the time" in imps["c_wrongtime.csv"]["message"]


def test_file_still_being_written_waits():
    src = CSV_SOURCE.replace("settle_seconds = 0", "settle_seconds = 30")
    tmp, cfg, conn = make_env(src)
    basic_network(conn)
    (tmp / "data/import/new.csv").write_text("Timestamp,PS1_FT\n2026-11-02 07:00:00,50\n")
    assert ingest.run(cfg, conn, log=lambda m: None) == 0
    assert conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0] == 0


def test_retention_keeps_rollups():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    cfg.data["storage"]["raw_days"] = 30
    basic_network(conn)
    old = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 100 * 86400))
    new = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 86400))
    write(tmp / "data/import/x.csv", f"Timestamp,PS1_FT\n{old},50\n{new},60\n")
    ingest.run(cfg, conn, log=lambda m: None)
    fid = conn.execute("SELECT id FROM sensors WHERE key='ps1-flow'").fetchone()[0]
    assert conn.execute("SELECT COUNT(*) FROM readings WHERE sensor_id=?", (fid,)).fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM rollups WHERE res=3600 AND sensor_id=?", (fid,)).fetchone()[0] == 2


def test_manual_reading():
    tmp, cfg, conn = make_env(CSV_SOURCE)
    basic_network(conn)
    r = ingest.manual_reading(conn, "ps1-wl", 120, "Field Tech", unit="cm")
    assert r["value"] == pytest.approx(1.2) and r["unit"] == "m"
    q = conn.execute("SELECT quality FROM readings r JOIN sensors s ON s.id=r.sensor_id WHERE s.key='ps1-wl'").fetchone()[0]
    assert q == dbm.MANUAL
    assert "Field Tech" in conn.execute("SELECT message FROM imports WHERE source='manual'").fetchone()[0]


def test_sniffing_and_numbers():
    assert sniff(["Timestamp;a;b", "2018-01-01 00:00:00;28,92;33,87"]) == (";", ",")
    assert sniff(["t,a,b", "2018-01-01,28.92,33.87"]) == (",", ".")
    assert sniff(["t\ta", "2018-01-01\t1,5"]) == ("\t", ",")
    assert to_number("1.234,5", ",") == 1234.5 and to_number("28,92", ",") == 28.92
    assert to_number("Running", ".") == 1.0 and to_number("stopped", ".") == 0.0
    assert to_number("", ".") is None
    with pytest.raises(ValueError):
        to_number("nan", ".")


def test_units():
    assert units.to_canonical("pressure", "bar", 1) == pytest.approx(10.19716, rel=1e-5)
    assert units.to_canonical("pressure", "kPa", 100) == pytest.approx(10.19716, rel=1e-5)
    assert units.to_canonical("pressure", "psi", 1) == pytest.approx(0.70307)
    assert units.to_canonical("flow", "l/s", 1) == pytest.approx(3.6)
    assert units.to_canonical("flow", "MLD", 24) == pytest.approx(1000)
    assert units.to_canonical("flow", "gpm", 1) == pytest.approx(0.2271247)
    assert units.to_canonical("temperature", "degF", 212) == pytest.approx(100)
    assert units.to_canonical("level", "ft", 10) == pytest.approx(3.048)
    assert units.from_canonical("pressure", "bar", units.to_canonical("pressure", "bar", 3.3)) == pytest.approx(3.3)
    with pytest.raises(ValueError):
        units.converter("flow", "bar")
