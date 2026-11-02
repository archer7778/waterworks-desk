"""Storm overflow status from Stream (streamwaterdata.co.uk), the English water companies' open
data hub. Every company publishes its event duration monitors (EDMs) in the same layout on
ArcGIS: Id, Status (1 discharging, 0 stopped, -1 offline), StatusStart, LatestEventStart,
LatestEventEnd, LastUpdated, Latitude, Longitude, ReceivingWaterCourse. Read-only: GET queries
only. Thames Water's layer is CC BY 4.0.

Each run turns the snapshot into readings for each monitor (tag = its Id, quantity "state"):
  1 at LatestEventStart and 0 at LatestEventEnd (the most recent spill),
  the current status at StatusStart, and again at LastUpdated (so a quiet monitor still reports).
Offline monitors give a reading flagged BAD, which the console shows as "monitor offline".
Re-reading the same snapshot overwrites, never duplicates.

config.toml:
    [[sources]]
    key = "thames"
    type = "stream_edm"
    url = "https://services2.arcgis.com/g6o32ZDQ33GpCIu3/arcgis/rest/services/Thames_Water_Storm_Overflow_Activity_(Production)_view/FeatureServer/0"
    # or path = "data/thames-overflows.json"   (a saved query result, for demos and tests)

tools/london_setup.py makes the matching sites and sensors from the same feed.
"""
import json
import time
import urllib.parse
from datetime import datetime, timezone

from ..db import BAD, GOOD, meta_get, meta_set
from .base import Adapter
from .openmeteo import get_json

FIELDS = "Id,Status,StatusStart,LatestEventStart,LatestEventEnd,LastUpdated,Latitude,Longitude,ReceivingWaterCourse"


def query_url(layer_url, offset=0):
    q = {"where": "1=1", "outFields": FIELDS, "outSR": "4326", "f": "json", "resultOffset": offset,
         "resultRecordCount": 1000, "orderByFields": "OBJECTID"}
    return layer_url.rstrip("/") + "/query?" + urllib.parse.urlencode(q)


def fetch(layer_url):
    """Every feature, a page of 1,000 at a time."""
    feats, offset = [], 0
    while True:
        d = get_json(query_url(layer_url, offset))
        if "error" in d:
            raise ValueError(f"ArcGIS said: {d['error'].get('message', d['error'])}")
        page = d.get("features") or []
        feats += page
        if not d.get("exceededTransferLimit") or not page:
            return feats
        offset += len(page)


def readings_from(features):
    s = lambda ms: int(ms // 1000) if ms else None
    for f in features:
        a = f.get("attributes") or {}
        tag = str(a.get("Id") or "").strip()
        if not tag:
            continue
        st = a.get("Status")
        if a.get("LatestEventStart"):
            yield tag, s(a["LatestEventStart"]), 1.0, GOOD
        if a.get("LatestEventEnd"):
            yield tag, s(a["LatestEventEnd"]), 0.0, GOOD
        for t in (a.get("StatusStart"), a.get("LastUpdated")):
            if t:
                if st == -1:
                    yield tag, s(t), 0.0, BAD
                elif st in (0, 1):
                    yield tag, s(t), float(st), GOOD


class Batch:
    def __init__(self, item, features):
        self.item, self.digest, self.problems, self.skipped = item, None, [], 0
        self._f = features
        self.rows_seen = len(features)

    @property
    def readings(self):
        return readings_from(self._f)


class StreamEDM(Adapter):
    network = True

    def batches(self, conn, force=False):
        s = self.source
        if s.get("path"):
            with open(self.cfg.resolve(s["path"]), encoding="utf-8") as f:
                yield Batch(str(s["path"]), json.load(f).get("features") or [])
            return
        if not s.get("url"):
            raise ValueError("the overflow source needs a url (the ArcGIS layer) or a path (a saved query)")
        key = f"stream_edm:{self.key}:last"
        every = float(s.get("min_minutes", 10)) * 60
        if not force and time.time() - (meta_get(conn, key, 0) or 0) < every:
            return
        feats = fetch(s["url"])
        meta_set(conn, key, time.time())
        yield Batch(f"snapshot {datetime.now(timezone.utc):%Y-%m-%d %H:%M}", feats)

    @classmethod
    def trap_test(cls, trap):
        url = getattr(trap, "url", None)
        if not url:
            return
        trap.routes["/layer/0/query"] = {"features": [{"attributes": {
            "Id": "TWL1", "Status": 1, "StatusStart": 1700000000000, "LatestEventStart": 1700000000000,
            "LatestEventEnd": None, "LastUpdated": 1700000600000}}]}
        from ..db import connect
        import tempfile
        from pathlib import Path
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        ad = cls(None, {"key": "thames", "type": "stream_edm", "url": url + "/layer/0"})
        got = [r for b in ad.batches(conn, force=True) for r in b.readings]
        assert got == [("TWL1", 1700000000, 1.0, GOOD), ("TWL1", 1700000000, 1.0, GOOD), ("TWL1", 1700000600, 1.0, GOOD)], got
