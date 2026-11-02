"""Rainfall from the Environment Agency's real-time flood-monitoring API (England): tipping-bucket
gauges reporting millimetres per 15 minutes. No sign-up; Open Government Licence v3 (credit:
"This uses Environment Agency rainfall data from the real-time data API (Beta)"). Read-only.

Each run asks for every rainfall reading since the last one it has (the API keeps four weeks),
for the stations set up as sensors (tag = the station reference, e.g. 287283TP).

config.toml:
    [[sources]]
    key = "ea"
    type = "ea_rain"
    # api = "https://environment.data.gov.uk/flood-monitoring"   (the default)

tools/london_setup.py makes rain-gauge sites from a station list
(GET /id/stations?parameter=rainfall&lat=..&long=..&dist=..).
"""
import time
import urllib.parse
from datetime import datetime, timezone

from ..db import GOOD
from .base import Adapter
from .openmeteo import get_json

API = "https://environment.data.gov.uk/flood-monitoring"


def station_of(measure):
    """'.../measures/287283TP-rainfall-tipping_bucket_raingauge-t-15_min-mm' -> '287283TP'"""
    return str(measure or "").rstrip("/").split("/")[-1].split("-rainfall")[0]


def parse_items(items):
    for it in items:
        try:
            ts = int(datetime.fromisoformat(str(it["dateTime"]).replace("Z", "+00:00")).timestamp())
            v = it.get("value")
            if isinstance(v, list):   # the API occasionally returns duplicates as a list
                v = v[0]
            v = float(v)
        except (KeyError, ValueError, TypeError):
            continue
        tag = station_of(it.get("measure"))
        if tag and v >= 0:
            yield tag, ts, v, GOOD


class Batch:
    def __init__(self, item, items):
        self.item, self.digest, self.problems, self.skipped = item, None, [], 0
        self._items = items
        self.rows_seen = len(items)

    @property
    def readings(self):
        return parse_items(self._items)


class EARain(Adapter):
    network = True

    def batches(self, conn, force=False):
        api = self.source.get("api", API).rstrip("/")
        stations = {r[0] for r in conn.execute(
            "SELECT source_tag FROM sensors WHERE source=? AND status!='retired' AND source_tag IS NOT NULL", (self.key,))}
        if not stations:
            return
        last = conn.execute("SELECT MAX(r.ts) FROM readings r JOIN sensors s ON s.id=r.sensor_id WHERE s.source=?",
                            (self.key,)).fetchone()[0]
        since = max(last or 0, int(time.time()) - 27 * 86400) - 3600
        q = {"parameter": "rainfall", "since": datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "_limit": 10000, "_sorted": ""}
        data = get_json(f"{api}/data/readings?" + urllib.parse.urlencode(q))
        items = [i for i in (data.get("items") or []) if station_of(i.get("measure")) in stations]
        yield Batch(f"readings since {datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d %H:%M}", items)

    @classmethod
    def trap_test(cls, trap):
        url = getattr(trap, "url", None)
        if not url:
            return
        trap.routes["/data/readings"] = {"items": [
            {"dateTime": "2026-11-02T07:15:00Z", "value": 0.4,
             "measure": url + "/id/measures/287283TP-rainfall-tipping_bucket_raingauge-t-15_min-mm"},
            {"dateTime": "2026-11-02T07:15:00Z", "value": 1.0,
             "measure": url + "/id/measures/OTHER-rainfall-tipping_bucket_raingauge-t-15_min-mm"}]}
        from ..db import connect
        import tempfile
        from pathlib import Path
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        conn.execute("INSERT INTO regions(key,name,created_ms) VALUES('r','R',0)")
        conn.execute("INSERT INTO systems(key,region_id,name,network_type,created_ms,updated_ms) VALUES('s',1,'S','storm_drain',0,0)")
        conn.execute("INSERT INTO sites(key,system_id,name,site_type,lat,lon,created_ms,updated_ms) VALUES('g',1,'G','rain_gauge',51,0,0,0)")
        conn.execute("INSERT INTO sensors(key,site_id,name,quantity,unit,source,source_tag,created_ms,updated_ms) "
                     "VALUES('g-r',1,'Rain','rain','mm','ea','287283TP',0,0)")
        conn.commit()
        ad = cls(None, {"key": "ea", "type": "ea_rain", "api": url})
        got = [r for b in ad.batches(conn, force=True) for r in b.readings]
        assert got == [("287283TP", 1793603700, 0.4, GOOD)], got
