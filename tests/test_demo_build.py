"""The public demo's safety nets: a feed that's down falls back to the last setup that worked, a
damaged kept database starts again, data that looks wrong is not published, and the last-day
summary the Overview draws from is sound."""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import build_demo  # noqa: E402

from waterworks_desk import db as dbm  # noqa: E402
from waterworks_desk.config import Config  # noqa: E402
from waterworks_desk.server import App  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def run(work, out, overflows=FIX / "thames-overflows.json", keep=True, env=None):
    old = dict(os.environ)
    os.environ.update(env or {})
    try:
        build_demo.main(["--offline", "--work", str(work), "--out", str(out), "--overflows", str(overflows),
                         "--rain", str(FIX / "ea-rain-stations.json")] + (["--keep"] if keep else []))
    finally:
        os.environ.clear()
        os.environ.update(old)


def test_feed_down_damaged_db_and_publish_gate():
    tmp = Path(tempfile.mkdtemp(prefix="wwd-demo-"))
    work, out, gh = tmp / "demo", tmp / "site", tmp / "gh-output"
    run(work, out, env={"GITHUB_OUTPUT": str(gh)})
    assert (out / "index.html").exists() and (out / "api" / "pulse.json").exists()
    assert gh.read_text().strip().endswith("publish=true")
    assert (work / "data" / "last-setup.json").exists()
    page = (out / "index.html").read_text()
    assert '"built":' in page

    # the overflow feed is unreachable: the last setup that worked is used and the build goes on
    shutil.rmtree(out)
    run(work, out, overflows=tmp / "nowhere.json", env={"GITHUB_OUTPUT": str(gh)})
    assert (out / "index.html").exists()

    # a damaged kept database: started again, not a build that fails for ever after
    def damage():
        (work / "data" / "demo.db").write_bytes(b"this is not a database" * 100)
        for f in ("demo.db-wal", "demo.db-shm"):
            (work / "data" / f).unlink(missing_ok=True)
    damage()
    shutil.rmtree(out)
    run(work, out)
    assert (out / "index.html").exists()
    # ... and if the feed is down at the same time there is no data at all: nothing is published
    damage()
    shutil.rmtree(out)
    run(work, out, overflows=tmp / "nowhere.json", env={"GITHUB_OUTPUT": str(gh)})
    assert not (out / "index.html").exists() and gh.read_text().strip().endswith("publish=false")
    run(work, out)

    # data that looks wrong is not published
    cfg = Config(work / "config.toml")
    conn = dbm.open_db(cfg)
    end = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
    assert build_demo.check(conn, end) == []
    assert any("hours old" in p for p in build_demo.check(conn, end + 3 * 86400))
    conn.execute("DELETE FROM readings WHERE sensor_id IN (SELECT id FROM sensors WHERE source='thames')")
    conn.commit()
    assert any("overflow monitors have any reading" in p for p in build_demo.check(conn, end))


def test_pulse_summary():
    tmp = Path(tempfile.mkdtemp(prefix="wwd-pulse-"))
    run(tmp / "demo", tmp / "site", keep=False)
    p = App(Config(tmp / "demo" / "config.toml")).pulse(24)
    assert p["to"] - p["from"] == 24 * 3600
    assert {f["source"] for f in p["feeds"]} == {"thames", "ea"}
    assert all(set(x) == {"site", "start", "end"} and p["from"] <= x["start"] <= x["end"] <= p["to"] for x in p["spills"])
    assert all(x["rule"] in ("monitor_offline", "silent", "site_silent") for x in p["offline"])
    assert set(p["rain"]) == {"gauges", "hourly"}
    assert json.loads((tmp / "site" / "api" / "pulse.json").read_text())["hours"] == 24
