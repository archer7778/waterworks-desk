"""The console's web server: serves the page and a small JSON API. Standard library only.
    python -m waterworks_desk.server [--config config.toml]

M0 skeleton: the network tree, sites, sensors and their readings (raw or rolled up), the audit
trail, imports and storage, plus the setup endpoint that everything set up by hand goes
through. Changes need the operator passcode (when one is set) AND a name, so the signed audit
trail always says who made them. Named accounts replace the passcode in M2.

Read endpoints
  GET /api/health                       for the updater and monitoring
  GET /api/tree                         regions > systems > zones and sites, with counts
  GET /api/systems/<key>                one system: its zones and sites
  GET /api/sites/<key>                  one site: components, sensors and their latest readings
  GET /api/sensors/<key>/readings?from=&to=&res=auto|raw|60|900|3600
  GET /api/setup/export                 everything set up by hand, as a setup bundle
  GET /api/setup/unmapped               tags arriving from sources with no sensor yet
  GET /api/log?after=  /api/log/verify  the signed audit trail
  GET /api/imports  /api/storage
Change endpoints (JSON body; X-Passcode header when a passcode is set)
  POST /api/setup    {"action": "save"|"retire"|"go_live", "kind": "site", "data": {...}, "actor": "Name"}
  POST /api/setup/import   {"bundle": {...}, "actor": "Name", "live": false}
  POST /api/readings/manual  {"sensor": "key", "value": 1.23, "unit": "bar", "actor": "Name"}
"""
import argparse
import contextlib
import gzip
import hmac
import json
import math
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, ingest, logchain, model
from . import db as dbm
from .config import find_config

WEB = Path(__file__).resolve().parent.parent / "web"
STARTED = dbm.now_ms()
MAX_POINTS = 2000


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.conn = dbm.open_db(cfg)
        self.lock = threading.Lock()
        self._pool = queue.LifoQueue()
        self.known_sources = {s["key"] for s in cfg.sources}
        if not self.conn.execute("SELECT 1 FROM log LIMIT 1").fetchone():
            logchain.append(self.conn, "system", {"version": __version__},
                            f"Console commissioned (Waterworks Desk {__version__}).", "system")

    @contextlib.contextmanager
    def ro(self):
        try:
            c = self._pool.get_nowait()
        except queue.Empty:
            c = dbm.connect_ro(self.cfg.db_path)
        try:
            yield c
        finally:
            if self._pool.qsize() < 6:
                self._pool.put(c)
            else:
                c.close()

    # ---------- reads ----------
    def tree(self):
        with self.ro() as c:
            regions = []
            for r in c.execute("SELECT * FROM regions ORDER BY name"):
                systems = []
                for s in c.execute("SELECT * FROM systems WHERE region_id=? AND status!='retired' ORDER BY name", (r["id"],)):
                    sites = c.execute("SELECT COUNT(*) n, SUM(status='live') live FROM sites WHERE system_id=? AND status!='retired'",
                                      (s["id"],)).fetchone()
                    sensors = c.execute("SELECT COUNT(*) FROM sensors x JOIN sites y ON x.site_id=y.id "
                                        "WHERE y.system_id=? AND x.status!='retired'", (s["id"],)).fetchone()[0]
                    zones = [dict(key=z["key"], name=z["name"], kind=z["kind"], status=z["status"]) for z in
                             c.execute("SELECT * FROM zones WHERE system_id=? AND status!='retired' ORDER BY name", (s["id"],))]
                    systems.append({"key": s["key"], "name": s["name"], "network_type": s["network_type"],
                                    "status": s["status"], "departments": [d for d in s["departments"].split(",") if d],
                                    "zones": zones, "sites": sites["n"], "sites_live": sites["live"] or 0,
                                    "sensors": sensors})
                regions.append({"key": r["key"], "name": r["name"], "timezone": r["timezone"], "datum": r["datum"],
                                "language": r["language"], "lat": r["lat"], "lon": r["lon"], "note": r["note"],
                                "systems": systems})
            return {"regions": regions}

    def system(self, key):
        with self.ro() as c:
            s = c.execute("SELECT * FROM systems WHERE key=?", (key,)).fetchone()
            if s is None:
                return None
            sites = [self._site_brief(c, r) for r in
                     c.execute("SELECT * FROM sites WHERE system_id=? AND status!='retired' ORDER BY name", (s["id"],))]
            return {"key": s["key"], "name": s["name"], "network_type": s["network_type"], "status": s["status"],
                    "sites": sites}

    @staticmethod
    def _site_brief(c, r):
        zone = c.execute("SELECT key FROM zones WHERE id=?", (r["zone_id"],)).fetchone() if r["zone_id"] else None
        return {"key": r["key"], "name": r["name"], "site_type": r["site_type"], "lat": r["lat"], "lon": r["lon"],
                "zone": zone["key"] if zone else None, "status": r["status"], "criticality": r["criticality"]}

    def site(self, key):
        with self.ro() as c:
            r = c.execute("SELECT * FROM sites WHERE key=?", (key,)).fetchone()
            if r is None:
                return None
            out = self._site_brief(c, r)
            out.update(ground_level_m=r["ground_level_m"], floor_level_m=r["floor_level_m"],
                       overflow_level_m=r["overflow_level_m"],
                       area_curve=json.loads(r["area_curve_json"]) if r["area_curve_json"] else None, note=r["note"],
                       system=c.execute("SELECT key FROM systems WHERE id=?", (r["system_id"],)).fetchone()[0])
            comps = {}
            out["components"] = []
            for k in c.execute("SELECT * FROM components WHERE site_id=? AND status!='retired' ORDER BY name", (r["id"],)):
                comps[k["id"]] = k["key"]
                out["components"].append({"key": k["key"], "name": k["name"], "type": k["component_type"],
                                          "install_date": k["install_date"], "criticality": k["criticality"],
                                          "status": k["status"]})
            out["sensors"] = []
            for x in c.execute("SELECT * FROM sensors WHERE site_id=? AND status!='retired' ORDER BY name", (r["id"],)):
                last = c.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? ORDER BY ts DESC LIMIT 1",
                                 (x["id"],)).fetchone()
                if last is None:   # raw data may have aged out; fall back to the newest rollup
                    last = c.execute("SELECT ts+res-1 ts, mean value, 0 quality FROM rollups WHERE sensor_id=? "
                                     "ORDER BY ts DESC LIMIT 1", (x["id"],)).fetchone()
                out["sensors"].append(self._sensor_json(x, comps.get(x["component_id"]), last))
            return out

    @staticmethod
    def _sensor_json(x, comp_key, last):
        lvl = None
        if last is not None and x["mount_level_m"] is not None and x["quantity"] in ("level", "pressure"):
            lvl = round(x["mount_level_m"] + last["value"], 3)   # water level (or, for pressure, head) above datum
        return {"key": x["key"], "name": x["name"], "quantity": x["quantity"], "unit": x["unit"],
                "display_unit": x["display_unit"] or x["unit"], "component": comp_key,
                "source": x["source"], "source_tag": x["source_tag"], "status": x["status"],
                "limits": {k: x[k] for k in ("alarm_lo", "warn_lo", "warn_hi", "alarm_hi")},
                "range": [x["range_lo"], x["range_hi"]], "mount_level_m": x["mount_level_m"],
                "latest": None if last is None else {"ts": last["ts"], "value": last["value"],
                                                     "quality": last["quality"], "level_above_datum_m": lvl}}

    def readings(self, key, t_from, t_to, res):
        with self.ro() as c:
            x = c.execute("SELECT id, unit FROM sensors WHERE key=?", (key,)).fetchone()
            if x is None:
                return None
            if t_to is None:
                t_to = int(time.time())
            if t_from is None:
                t_from = t_to - 86_400
            span = max(1, t_to - t_from)
            if res == "auto":
                n_raw = c.execute("SELECT COUNT(*) FROM readings WHERE sensor_id=? AND ts BETWEEN ? AND ?",
                                  (x["id"], t_from, t_to)).fetchone()[0]
                every = c.execute("SELECT expected_interval_s FROM sensors WHERE id=?", (x["id"],)).fetchone()[0] or 0
                res = "raw" if 0 < n_raw <= MAX_POINTS else next(
                    (r for r in dbm.ROLLUP_RES if r > every and span / r <= MAX_POINTS), 3600)
            if res == "raw":
                rows = c.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? AND ts BETWEEN ? AND ? "
                                 "ORDER BY ts LIMIT 200000", (x["id"], t_from, t_to)).fetchall()
                return {"sensor": key, "unit": x["unit"], "res": "raw", "t": [r[0] for r in rows],
                        "v": [r[1] for r in rows], "q": [r[2] for r in rows]}
            res = int(res)
            if res not in dbm.ROLLUP_RES:
                raise ValueError(f"res must be auto, raw or one of {dbm.ROLLUP_RES}")
            rows = c.execute("SELECT ts, mean, lo, hi, n FROM rollups WHERE res=? AND sensor_id=? AND ts BETWEEN ? AND ? "
                             "ORDER BY ts LIMIT 200000", (res, x["id"], t_from - res + 1, t_to)).fetchall()
            return {"sensor": key, "unit": x["unit"], "res": res, "t": [r[0] for r in rows],
                    "mean": [r[1] for r in rows], "lo": [r[2] for r in rows], "hi": [r[3] for r in rows],
                    "n": [r[4] for r in rows]}

    def unmapped(self):
        with self.ro() as c:
            return {s: dbm.meta_get(c, f"unmapped:{s}", {}) for s in sorted(self.known_sources)}

    # ---------- changes (one writer at a time) ----------
    def setup(self, body):
        action, kind = body.get("action", "save"), body.get("kind", "")
        actor = body.get("actor", "")
        with self.lock:
            if action == "save":
                data = body.get("data")
                if not isinstance(data, dict):
                    raise model.SetupError(["data should be an object"])
                return model.save(self.conn, kind, data, actor, self.known_sources)
            if action == "retire":
                return model.retire(self.conn, kind, str(body.get("key", "")), actor, str(body.get("reason", ""))[:300])
            if action == "go_live":
                return model.go_live(self.conn, kind, str(body.get("key", "")), actor, self.known_sources)
        raise model.SetupError([f"unknown action {action!r}"])

    def import_setup(self, body):
        with self.lock:
            return model.import_bundle(self.conn, body.get("bundle"), body.get("actor", ""), self.known_sources,
                                       bool(body.get("live")), source_name="console upload")

    def manual(self, body):
        with self.lock:
            v = body.get("value")
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise model.SetupError(["the reading must be a number"])
            return ingest.manual_reading(self.conn, str(body.get("sensor", "")), v, body.get("actor", ""),
                                         body.get("unit"), body.get("ts"))


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        server_version = "WaterworksDesk/" + __version__
        timeout = 60

        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, status=200):
            try:
                body = json.dumps(obj, separators=(",", ":"), allow_nan=False).encode()
            except ValueError:
                body = json.dumps(_finite(obj), separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if len(body) > 1400 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
                body = gzip.compress(body, 4)
                self.send_header("Content-Encoding", "gzip")
                self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _same_origin(self):
            """Block other websites from making changes through a visitor's browser (CSRF)."""
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return False
            origin = self.headers.get("Origin")
            if origin:
                return urlparse(origin).netloc == (self.headers.get("Host") or "")
            return True

        def _authorised(self):
            code = app.cfg["server"]["write_passcode"]
            return not code or hmac.compare_digest(self.headers.get("X-Passcode", ""), str(code))

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n < 0 or n > 4_000_000:
                raise ValueError("request too large")
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            return body

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p == "/api/health":
                    return self._json({"ok": True, "version": __version__, "started_ms": STARTED})
                if p == "/api/tree":
                    return self._json(app.tree())
                m = re.fullmatch(r"/api/systems/([^/]+)", p)
                if m:
                    r = app.system(unquote(m.group(1)))
                    return self._json(r) if r else self._json({"error": "no such system"}, 404)
                m = re.fullmatch(r"/api/sites/([^/]+)", p)
                if m:
                    r = app.site(unquote(m.group(1)))
                    return self._json(r) if r else self._json({"error": "no such site"}, 404)
                m = re.fullmatch(r"/api/sensors/([^/]+)/readings", p)
                if m:
                    t_from = _int(q, "from")
                    t_to = _int(q, "to")
                    r = app.readings(unquote(m.group(1)), t_from, t_to, q.get("res", "auto"))
                    return self._json(r) if r else self._json({"error": "no such sensor"}, 404)
                if p == "/api/setup/export":
                    with app.ro() as c:
                        return self._json(model.export_bundle(c))
                if p == "/api/setup/unmapped":
                    return self._json(app.unmapped())
                if p == "/api/log":
                    with app.ro() as c:
                        return self._json(logchain.entries(c, _int(q, "after") or 0))
                if p == "/api/log/verify":
                    with app.ro() as c:
                        return self._json(logchain.verify(c))
                if p == "/api/imports":
                    with app.ro() as c:
                        return self._json([dict(r) for r in c.execute("SELECT * FROM imports ORDER BY id DESC LIMIT 200")])
                if p == "/api/storage":
                    with app.ro() as c:
                        return self._json(dbm.storage(c, app.cfg.db_path))
                if p in ("/", "/index.html"):
                    return self._static("index.html")
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        def _static(self, name):
            f = WEB / name
            body = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            p = urlparse(self.path).path
            if not self._same_origin():
                return self._json({"error": "Changes must come from the console itself."}, 403)
            if not self._authorised():
                return self._json({"error": "This console needs the operator passcode for changes."}, 401)
            try:
                body = self._body()
                if p == "/api/setup":
                    return self._json(app.setup(body))
                if p == "/api/setup/import":
                    return self._json(app.import_setup(body))
                if p == "/api/readings/manual":
                    return self._json(app.manual(body))
                return self._json({"error": "not found"}, 404)
            except model.SetupError as e:
                return self._json({"error": "Not saved.", "problems": e.errors, "warnings": e.warnings}, 422)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

    return H


def _int(q, name):
    if name not in q or q[name] == "":
        return None
    v = float(q[name])
    if not math.isfinite(v) or abs(v) > 1e11:
        raise ValueError(f"{name} must be a time in seconds")
    return int(v)


def _finite(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _finite(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_finite(v) for v in o]
    return o


def main(argv=None):
    ap = argparse.ArgumentParser(description="Waterworks Desk console server")
    ap.add_argument("--config")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    app = App(cfg)
    s = cfg["server"]
    httpd = ThreadingHTTPServer((s["host"], int(s["port"])), make_handler(app))
    print(f"Waterworks Desk {__version__} serving on http://{s['host']}:{s['port']}  (Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
