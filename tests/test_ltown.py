"""Against the real BattLeDIM L-Town data, when it's on this machine (it isn't in the repo).

    WATERWORKS_LTOWN_DIR=/path/to/battledim python -m pytest tests/test_ltown.py -q

The folder needs L-TOWN.inp and the 2018 SCADA CSVs from Zenodo (10.5281/zenodo.4017659).
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from helpers import ACTOR, ROOT, make_env

from waterworks_desk import ingest, model

DIR = Path(os.environ.get("WATERWORKS_LTOWN_DIR", ROOT / "data" / "ltown"))
pytestmark = pytest.mark.skipif(not (DIR / "L-TOWN.inp").is_file(), reason="L-Town data not present")


def test_ltown_setup_and_import():
    sys.path.insert(0, str(ROOT / "tools"))
    import ltown_setup
    bundle = ltown_setup.build(DIR / "L-TOWN.inp", 35.0, 33.0)
    assert len(bundle["sensors"]) == 37      # 33 pressures, 2 district meters, the pump, the tank
    tmp, cfg, conn = make_env(f"""
[[sources]]
key = "ltown"
type = "csv"
path = "{DIR}"
pattern = "2018_SCADA_[FL]*.csv"
timezone = "UTC"
settle_seconds = 0
""")
    model.import_bundle(conn, bundle, ACTOR, {"ltown"}, make_live=True)
    assert conn.execute("SELECT COUNT(*) FROM sensors WHERE status='live'").fetchone()[0] == 37
    n = ingest.run(cfg, conn, log=lambda m: None)
    assert n == 105_120 * 4                  # a year at 5 minutes: 3 flows and the tank level
    imps = conn.execute("SELECT item, rows_seen, skipped, message FROM imports").fetchall()
    assert all(r["skipped"] == 0 and r["message"] == "" for r in imps), [dict(r) for r in imps]
    # decimal commas read correctly: first pump flow is 44,04 m³/h, first tank level 3,5 m
    v = conn.execute("SELECT r.value FROM readings r JOIN sensors s ON s.id=r.sensor_id WHERE s.source_tag='PUMP_1' "
                     "ORDER BY r.ts LIMIT 1").fetchone()[0]
    assert v == pytest.approx(44.04)
    t1 = conn.execute("SELECT r.value FROM readings r JOIN sensors s ON s.id=r.sensor_id WHERE s.source_tag='T1' "
                      "ORDER BY r.ts LIMIT 1").fetchone()[0]
    assert t1 == pytest.approx(3.5)
    # 5-minute data gets 15-minute and hourly rollups, not 1-minute copies
    res = dict(conn.execute("SELECT res, COUNT(*) FROM rollups GROUP BY res").fetchall())
    assert res == {900: 35_040 * 4, 3600: 8_760 * 4}
