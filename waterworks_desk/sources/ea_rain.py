"""Rainfall from the Environment Agency's real-time flood-monitoring API (England): tipping-bucket
gauges reporting millimetres per 15 minutes. No sign-up; Open Government Licence v3 (credit:
"This uses Environment Agency rainfall data from the real-time data API (Beta)"). Read-only.

Each run asks each station set up as a sensor (tag = the station reference, e.g. 287283TP) for
its readings since the last one stored: a week back on the first run, at most four weeks (what
the API keeps). One small request per station: asking for every gauge in England at once is
more than the API will answer.

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
        first = int(self.source.get("first_days", 7))
        items, problems = [], []
        for ref in sorted(stations):
            last = conn.execute("SELECT MAX(r.ts) FROM readings r JOIN sensors s ON s.id=r.sensor_id "
                                "WHERE s.source=? AND s.source_tag=?", (self.key, ref)).fetchone()[0]
            since = max(last - 3600 if last else int(time.time()) - first * 86400, int(time.time()) - 27 * 86400)
            iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = f"{api}/id/stations/{urllib.parse.quote(ref)}/readings?_sorted&_limit=5000&since={iso}"
            try:
                items += get_json(url, tries=2).get("items") or []
            except Exception as e:   # one gauge failing never stops the others
                problems.append(f"{ref}: {type(e).__name__}: {e}"[:160])
        b = Batch(f"{len(stations)} stations", items)
        b.problems, b.skipped = problems[:8], len(problems)
        yield b

    @classmethod
    def trap_test(cls, trap):
        url = getattr(trap, "url", None)
        if not url:
            return
        trap.routes["/id/stations/287283TP/readings"] = {"items": [
            {"dateTime": "2026-11-02T07:15:00Z", "value": 0.4,
             "measure": url + "/id/measures/287283TP-rainfall-tipping_bucket_raingauge-t-15_min-mm"},
            {"dateTime": "bad", "value": 1.0,
             "measure": url + "/id/measures/287283TP-rainfall-tipping_bucket_raingauge-t-15_min-mm"}]}
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
