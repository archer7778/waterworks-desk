"""Weather from Open-Meteo (open-meteo.com): hourly rainfall and temperature for a point, to lay
over every chart. Rain explains sewer inflow; heat explains demand. Read-only: GET requests only.

Two modes:
  forecast  the last two days and today, refreshed at most every 30 minutes (live use)
  archive   a fixed historical range, fetched once a year at a time (demos on historical data)

config.toml:
    [[sources]]
    key = "weather"
    type = "open_meteo"
    lat = 51.5072
    lon = -0.1276
    mode = "forecast"                 # or "archive", with start = "2018-01-01" and end = "2019-12-31"
    variables = ["precipitation", "temperature_2m"]

Set up a site (site_type "rain_gauge") with one sensor per variable: source "weather", source_tag
"precipitation" (quantity "rain", unit "mm") and "temperature_2m" (quantity "temperature",
unit "degC"). Open-Meteo data is CC BY 4.0: credit "Weather data by Open-Meteo.com".
"""
import json
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

from .. import __version__
from ..db import GOOD, meta_get, meta_set
from .base import Adapter

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_API = "https://archive-api.open-meteo.com/v1/archive"


def get_json(url, tries=3, timeout=30):
    """A polite GET with a few retries; never anything but GET."""
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"WaterworksDesk/{__version__}",
                                                       "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:   # network trouble, rate limit, bad gateway: wait and try again
            last = e
            time.sleep(min(2 ** i, 10) if i < tries - 1 else 0)
    raise last


class Batch:
    def __init__(self, item, payload, variables):
        self.item, self.digest, self.problems, self.skipped = item, None, [], 0
        self._payload, self._vars = payload, variables
        self.rows_seen = 0

    @property
    def readings(self):
        h = self._payload.get("hourly") or {}
        times = h.get("time") or []
        horizon = time.time()
        for i, t in enumerate(times):
            ts = int(datetime.fromisoformat(t).replace(tzinfo=timezone.utc).timestamp())
            if ts > horizon:
                continue   # forecasts aren't readings
            self.rows_seen += 1
            for v in self._vars:
                vals = h.get(v) or []
                if i < len(vals) and vals[i] is not None:
                    yield v, ts, float(vals[i]), GOOD


class OpenMeteo(Adapter):
    network = True

    def _url(self, base, **extra):
        s = self.source
        q = {"latitude": s["lat"], "longitude": s["lon"], "hourly": ",".join(self.variables), "timezone": "GMT", **extra}
        return base + "?" + urllib.parse.urlencode(q)

    @property
    def variables(self):
        return list(self.source.get("variables", ["precipitation", "temperature_2m"]))

    def batches(self, conn, force=False):
        s = self.source
        if "lat" not in s or "lon" not in s:
            raise ValueError("the weather source needs lat and lon in config.toml")
        mode = s.get("mode", "forecast")
        if mode == "forecast":
            key = f"openmeteo:{self.key}:last"
            if not force and time.time() - (meta_get(conn, key, 0) or 0) < 1800:
                return
            data = get_json(self._url(s.get("api", FORECAST_API), past_days=2, forecast_days=1))
            meta_set(conn, key, time.time())
            yield Batch(f"forecast {datetime.now(timezone.utc):%Y-%m-%d %H:00}", data, self.variables)
        elif mode == "archive":
            start, end = date.fromisoformat(str(s["start"])), date.fromisoformat(str(s["end"]))
            done_key = f"openmeteo:{self.key}:years"
            done = set(meta_get(conn, done_key, []) or [])
            for y in range(start.year, end.year + 1):
                if str(y) in done and not force:
                    continue
                a, b = max(start, date(y, 1, 1)), min(end, date(y, 12, 31))
                data = get_json(self._url(s.get("api", ARCHIVE_API), start_date=a.isoformat(), end_date=b.isoformat()))
                batch = Batch(f"archive {a}..{b}", data, self.variables)
                batch.year = str(y)
                yield batch
        else:
            raise ValueError(f"weather mode {mode!r}: use forecast or archive")

    def remember(self, conn, batch):
        y = getattr(batch, "year", None)
        if y and int(y) < date.today().year:   # a finished year never changes; this year keeps filling in
            k = f"openmeteo:{self.key}:years"
            meta_set(conn, k, sorted(set(meta_get(conn, k, []) or []) | {y}))

    @classmethod
    def trap_test(cls, trap):
        """Run against a write trap (tests/test_readonly.py): it must read and never write."""
        url = getattr(trap, "url", None)
        if not url:
            return   # not an HTTP trap: this adapter only speaks HTTP
        trap.routes["/v1/forecast"] = {"hourly": {"time": ["2020-01-01T00:00", "2020-01-01T01:00"],
                                                  "precipitation": [0.0, 1.2], "temperature_2m": [10.5, None]}}
        from ..db import connect
        import tempfile
        from pathlib import Path
        conn = connect(Path(tempfile.mkdtemp()) / "t.db")
        ad = cls(None, {"key": "weather", "type": "open_meteo", "lat": 1, "lon": 2, "api": url + "/v1/forecast"})
        got = [r for b in ad.batches(conn, force=True) for r in b.readings]
        assert got == [("precipitation", 1577836800, 0.0, GOOD), ("temperature_2m", 1577836800, 10.5, GOOD),
                       ("precipitation", 1577840400, 1.2, GOOD)], got
