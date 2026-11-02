"""Shared test setup: a throwaway config and database in a temp folder."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from waterworks_desk import db as dbm  # noqa: E402
from waterworks_desk.config import Config  # noqa: E402

ACTOR = "Test Operator"


def make_env(sources_toml="", extra=""):
    tmp = Path(tempfile.mkdtemp(prefix="wwd-test-"))
    (tmp / "data" / "import").mkdir(parents=True)
    (tmp / "config.toml").write_text(f"""
[server]
port = 0
[storage]
db = "data/test.db"
raw_days = 0
minute_days = 0
{extra}
{sources_toml}
""")
    cfg = Config(tmp / "config.toml")
    conn = dbm.open_db(cfg)
    return tmp, cfg, conn


CSV_SOURCE = """
[[sources]]
key = "exports"
type = "csv"
path = "data/import"
layout = "wide"
timezone = "Asia/Kolkata"
settle_seconds = 0
"""


def basic_network(conn, known=("exports",)):
    """A small but realistic drinking-water setup: one region, one system, a pumping station
    with a wet well, two pumps and their sensors."""
    from waterworks_desk import model
    known = set(known)
    model.save(conn, "region", {"key": "testcity", "name": "Test City", "timezone": "Asia/Kolkata",
                                "datum": "National datum", "lat": 45.7769, "lon": 7.7009, "radius_km": 60}, ACTOR)
    model.save(conn, "system", {"key": "dw", "region": "testcity", "name": "Central drinking water",
                                "network_type": "drinking_water", "departments": ["operations", "maintenance"]}, ACTOR)
    model.save(conn, "site", {"key": "ps1", "system": "dw", "name": "Riverside pumping station",
                              "site_type": "pumping_station", "lat": "45°46'37\"N", "lon": "7°42'03\"E",
                              "ground_level_m": 2.5, "floor_level_m": {"ref": "below_ground", "value": 4.0},
                              "overflow_level_m": {"ref": "above_floor", "value": 3.5},
                              "area_curve_json": [[0, 12.0], [3.5, 12.0]], "criticality": 1}, ACTOR)
    model.save(conn, "component", {"key": "ps1-p1", "site": "ps1", "name": "Pump 1", "component_type": "centrifugal_pump",
                                   "install_date": "2015-03-01"}, ACTOR)
    model.save(conn, "component", {"key": "ps1-p2", "site": "ps1", "name": "Pump 2", "component_type": "centrifugal_pump",
                                   "install_date": "2019-06-30"}, ACTOR)
    model.save(conn, "sensor", {"key": "ps1-p1-dp", "site": "ps1", "component": "ps1-p1",
                                "name": "Pump 1 discharge pressure", "quantity": "pressure", "source": "exports",
                                "source_tag": "PS1_P1_PT", "source_unit": "bar", "display_unit": "bar",
                                "mount_level_m": {"ref": "above_ground", "value": 0.5},
                                "range_lo": 0, "range_hi": 10, "warn_lo": 2.0, "alarm_lo": 1.5, "warn_hi": 6, "alarm_hi": 7}, ACTOR, known)
    model.save(conn, "sensor", {"key": "ps1-flow", "site": "ps1", "name": "Station outlet flow", "quantity": "flow",
                                "source": "exports", "source_tag": "PS1_FT", "source_unit": "l/s", "display_unit": "l/s"}, ACTOR, known)
    model.save(conn, "sensor", {"key": "ps1-wl", "site": "ps1", "name": "Wet well level", "quantity": "level",
                                "source": "exports", "source_tag": "PS1_LT", "source_unit": "m",
                                "mount_level_m": {"ref": "above_floor", "value": 0.2}}, ACTOR, known)
