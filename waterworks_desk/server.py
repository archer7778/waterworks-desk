"""The console's web server: serves the page and a small JSON API. Standard library only.
    python -m waterworks_desk.server [--config config.toml]

Changes need the operator passcode (when one is set) AND a name, so the signed audit trail
always says who made them. Named accounts replace the passcode in M2. Changes to anything
already live wait as pending changes until someone applies them.

Read endpoints
  GET /api/health                       for the updater and monitoring
  GET /api/config                       what the console needs to draw itself (types, units, map tiles)
  GET /api/tree                         regions > systems > zones and sites, with counts
  GET /api/map?system=                  sites with positions, worst open alert and key readings
  GET /api/systems/<key>                one system: its zones and sites
  GET /api/sites/<key>                  one site: components, sensors, latest readings, alerts
  GET /api/sensors/<key>/readings?from=&to=&res=auto|raw|60|900|3600|86400
  GET /api/alerts?system=&shelved=1&ended_hours=24
  GET /api/spills?system=&days=30          per overflow: spills (12/24 method), hours, possible dry-day spills (days=0: all data)
  GET /api/setup/item?kind=&key=        one item as an editable form
  GET /api/setup/pending                changes to live items waiting to be applied
  GET /api/setup/export                 everything set up by hand, as a setup bundle
  GET /api/setup/unmapped               tags arriving from sources with no sensor yet
  GET /api/log?after=  /api/log/verify  the signed audit trail
  GET /api/imports  /api/storage
Change endpoints (JSON body; X-Passcode header when a passcode is set)
  POST /api/setup   {"action": "save"|"apply"|"discard"|"retire"|"go_live", "kind": "site", "data"|"key": ..., "actor": "Name"}
  POST /api/setup/import     {"bundle": {...}, "actor": "Name", "live": false}
  POST /api/readings/manual  {"sensor": "key", "value": 1.23, "unit": "bar", "actor": "Name"}
  POST /api/alerts/ack       {"id": 1, "actor": "Name", "note": ""}
  POST /api/alerts/shelve    {"id": 1, "actor": "Name", "hours": 4, "reason": "planned works"}
  POST /api/alerts/unshelve  {"id": 1, "actor": "Name"}
"""
import argparse
import contextlib
import gzip
import hmac
import json
import math
import mimetypes
import queue
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, alerts, ingest, logchain, model, spills, units
from . import db as dbm
from .config import find_config

WEB = Path(__file__).resolve().parent.parent / "web"
STARTED = dbm.now_ms()
MAX_POINTS = 2000
SEV_RANK = {"critical": 3, "warning": 2, "advisory": 1}


def display(quantity, unit, display_unit):
    """How to turn canonical values into the display unit: shown = (value - offset) / factor."""
    u = display_unit or unit
    try:
        f, o = units.QUANTITIES[quantity][1][units._norm(u)]
    except KeyError:
        u, f, o = unit, 1, 0
    return {"unit": u.replace("m3", "m³"), "factor": f, "offset": o}


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

    def clock_ms(self):
        with self.ro() as c:
            return alerts.clock(self.cfg, c) * 1000

    def clocks(self):
        """{system key: ms} the time each system is judged at (differs only in a historical demo)."""
        with self.ro() as c:
            keys = {r[0]: r[1] for r in c.execute("SELECT id, key FROM systems")}
            return {keys[i]: t * 1000 for i, t in alerts.system_clocks(self.cfg, c).items() if i in keys}

    # ---------- reads ----------
    def config(self):
        m, con = self.cfg["map"], self.cfg["console"]
        return {"version": __version__, "title": con["title"], "language": con["language"],
                "default_system": con.get("default_system", ""),
                "write_protected": bool(self.cfg["server"]["write_passcode"]), "static": False,
                "clock_ms": self.clock_ms(), "clock_mode": str(self.cfg["alerts"]["clock"]), "clocks": self.clocks(),
                "map": {"tile_url": m["tile_url"], "attribution": m["attribution"], "max_zoom": m["max_zoom"]},
                "network_types": list(dbm.NETWORK_TYPES), "site_types": sorted(model.SITE_TYPES),
                "component_types": sorted(model.COMPONENT_TYPES), "height_refs": model.HEIGHT_REFS,
                "quantities": {q: {"unit": u, "units": sorted({a for a in al if a})} for q, (u, al) in units.QUANTITIES.items()},
                "sources": sorted(self.known_sources) + ["manual"],
                "credits": self.cfg.data.get("credits", [])}

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
                                "language": r["language"], "lat": r["lat"], "lon": r["lon"], "radius_km": r["radius_km"], "basemap": bool(r["basemap"]),
                                "note": r["note"], "systems": systems})
            return {"regions": regions}

    def map(self, system=None):
        with self.ro() as c:
            q = ("SELECT s.*, y.key system_key, y.network_type, g.key region_key, g.basemap FROM sites s "
                 "JOIN systems y ON y.id=s.system_id JOIN regions g ON g.id=y.region_id "
                 "WHERE s.status!='retired' AND y.status!='retired'")
            args = []
            if system and system.startswith("region:"):
                q += " AND g.key=?"
                args.append(system[7:])
            elif system:
                q += " AND y.key=?"
                args.append(system)
            worst = {}
            for a in c.execute("SELECT site_id, severity FROM alerts WHERE end_ms IS NULL AND "
                               "(shelved_until_ms IS NULL OR shelved_until_ms<?)", (dbm.now_ms(),)):
                if SEV_RANK[a["severity"]] > SEV_RANK.get(worst.get(a["site_id"]), 0):
                    worst[a["site_id"]] = a["severity"]
            out = []
            for s in c.execute(q + " ORDER BY s.name", args):
                key_vals = []
                for x in c.execute("SELECT * FROM sensors WHERE site_id=? AND status!='retired' ORDER BY id LIMIT 3", (s["id"],)):
                    last = self._latest(c, x["id"])
                    key_vals.append({"key": x["key"], "name": x["name"], "quantity": x["quantity"],
                                     "display": display(x["quantity"], x["unit"], x["display_unit"]),
                                     "value": last["value"] if last else None, "ts": last["ts"] if last else None,
                                     "quality": last["quality"] if last else None})
                rain = c.execute("SELECT key FROM sensors WHERE site_id=? AND quantity='rain' AND status!='retired' LIMIT 1",
                                 (s["id"],)).fetchone()
                out.append({"key": s["key"], "name": s["name"], "site_type": s["site_type"], "lat": s["lat"], "lon": s["lon"],
                            "rain_sensor": rain[0] if rain else None,
                            "system": s["system_key"], "network_type": s["network_type"], "status": s["status"],
                            "region": s["region_key"], "basemap": bool(s["basemap"]),
                            "criticality": s["criticality"], "worst": worst.get(s["id"]), "readings": key_vals})
            lq = ("SELECT l.key, l.name, l.kind, l.geojson, y.key system, g.key region FROM map_layers l "
                  "JOIN systems y ON y.id=l.system_id JOIN regions g ON g.id=y.region_id WHERE y.status!='retired'")
            if system and system.startswith("region:"):
                lq, la = lq + " AND g.key=?", [system[7:]]
            elif system:
                lq, la = lq + " AND y.key=?", [system]
            else:
                la = []
            layers = [{"key": r["key"], "name": r["name"], "kind": r["kind"], "system": r["system"], "region": r["region"],
                       "geojson": json.loads(r["geojson"])} for r in c.execute(lq, la)]
            return {"sites": out, "layers": layers}

    @staticmethod
    def _latest(c, sid):
        last = c.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
        if last is None:   # raw data may have aged out; fall back to the newest rollup
            last = c.execute("SELECT ts+res-1 ts, mean value, 0 quality FROM rollups WHERE sensor_id=? "
                             "ORDER BY ts DESC LIMIT 1", (sid,)).fetchone()
        return last

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

    def site(self, key, all_alerts=None, clock_ms=None):
        with self.ro() as c:
            r = c.execute("SELECT * FROM sites WHERE key=?", (key,)).fetchone()
            if r is None:
                return None
            out = self._site_brief(c, r)
            sysrow = c.execute("SELECT y.key, y.name, y.network_type, g.timezone, g.datum FROM systems y "
                               "JOIN regions g ON g.id=y.region_id WHERE y.id=?", (r["system_id"],)).fetchone()
            out.update(ground_level_m=r["ground_level_m"], floor_level_m=r["floor_level_m"],
                       overflow_level_m=r["overflow_level_m"],
                       area_curve=json.loads(r["area_curve_json"]) if r["area_curve_json"] else None, note=r["note"],
                       system=sysrow["key"], system_name=sysrow["name"], network_type=sysrow["network_type"],
                       timezone=sysrow["timezone"], datum=sysrow["datum"])
            comps = {}
            out["components"] = []
            for k in c.execute("SELECT * FROM components WHERE site_id=? AND status!='retired' ORDER BY name", (r["id"],)):
                comps[k["id"]] = k["key"]
                out["components"].append({"key": k["key"], "name": k["name"], "type": k["component_type"],
                                          "install_date": k["install_date"], "criticality": k["criticality"] or r["criticality"],
                                          "status": k["status"]})
            out["sensors"] = []
            for x in c.execute("SELECT * FROM sensors WHERE site_id=? AND status!='retired' ORDER BY name", (r["id"],)):
                out["sensors"].append(self._sensor_json(x, comps.get(x["component_id"]), self._latest(c, x["id"])))
            out["pending"] = [dict(kind=p["kind"], key=p["key"]) for p in c.execute(
                "SELECT kind, key FROM pending_changes")]
        if all_alerts is None:
            out["alerts"] = [a for a in self.alerts(include_ended_hours=24 * 7) if a["site"] == key]
        else:   # the static export passes every alert once, with each system's own clock
            out["alerts"] = [a for a in all_alerts if a["site"] == key and (a["end_ms"] is None or a["end_ms"] > clock_ms - 7 * 86400_000)]
        return out

    @staticmethod
    def _sensor_json(x, comp_key, last):
        lvl = None
        if last is not None and x["mount_level_m"] is not None and x["quantity"] in ("level", "pressure"):
            lvl = round(x["mount_level_m"] + last["value"], 3)   # water level (or, for pressure, head) above datum
        return {"key": x["key"], "name": x["name"], "quantity": x["quantity"], "unit": x["unit"],
                "display": display(x["quantity"], x["unit"], x["display_unit"]), "component": comp_key,
                "source": x["source"], "source_tag": x["source_tag"], "status": x["status"],
                "limits": {k: x[k] for k in ("alarm_lo", "warn_lo", "warn_hi", "alarm_hi")},
                "range": [x["range_lo"], x["range_hi"]], "mount_level_m": x["mount_level_m"],
                "expected_interval_s": x["expected_interval_s"],
                "latest": None if last is None else {"ts": last["ts"], "value": last["value"],
                                                     "quality": last["quality"], "level_above_datum_m": lvl}}

    def readings(self, key, t_from, t_to, res):
        with self.ro() as c:
            x = c.execute("SELECT id, unit, quantity, display_unit, expected_interval_s FROM sensors WHERE key=?", (key,)).fetchone()
            if x is None:
                return None
            if t_to is None:
                t_to = self.clock_ms() // 1000
            if t_from is None:
                t_from = t_to - 86_400
            span = max(1, t_to - t_from)
            every = x["expected_interval_s"] or 0
            if res == "auto":
                n_raw = c.execute("SELECT COUNT(*) FROM readings WHERE sensor_id=? AND ts BETWEEN ? AND ?",
                                  (x["id"], t_from, t_to)).fetchone()[0]
                res = "raw" if 0 < n_raw <= MAX_POINTS else next(
                    (r for r in dbm.ROLLUP_RES if r > every and span / r <= MAX_POINTS), 86400)
            base = {"sensor": key, "unit": x["unit"], "quantity": x["quantity"],
                    "display": display(x["quantity"], x["unit"], x["display_unit"]), "from": t_from, "to": t_to}
            if x["quantity"] == "state":
                res = "raw"   # on/off: averages mean nothing; the readings are few (one per change)
            if res == "raw":
                rows = c.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? AND ts BETWEEN ? AND ? "
                                 "ORDER BY ts LIMIT 200000", (x["id"], t_from, t_to)).fetchall()
                out = {**base, "res": "raw", "t": [r[0] for r in rows], "v": [r[1] for r in rows], "q": [r[2] for r in rows]}
                prev = c.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                                 (x["id"], t_from)).fetchone()
                out["prev"] = list(prev) if prev else None   # the state it was already in when the window opened
                return out
            res = int(res)
            if res not in dbm.ROLLUP_RES:
                raise ValueError(f"res must be auto, raw or one of {dbm.ROLLUP_RES}")
            rows = c.execute("SELECT ts, mean, lo, hi, n FROM rollups WHERE res=? AND sensor_id=? AND ts BETWEEN ? AND ? "
                             "ORDER BY ts LIMIT 200000", (res, x["id"], t_from - res + 1, t_to)).fetchall()
            return {**base, "res": res, "t": [r[0] for r in rows], "v": [r[1] for r in rows],
                    "lo": [r[2] for r in rows], "hi": [r[3] for r in rows], "n": [r[4] for r in rows]}

    def alerts(self, system=None, include_shelved=False, include_ended_hours=0):
        with self.ro() as c:
            return alerts.active(c, self.clock_ms(), system, include_shelved, include_ended_hours)

    def spills(self, system=None, days=30):
        """Spill report per overflow: 12/24 counts, hours, possible dry-day spills."""
        with self.ro() as c:
            end = max(self.clocks().values() or [self.clock_ms()]) // 1000
            if system and not system.startswith("region:"):
                end = self.clocks().get(system, end * 1000) // 1000
            tz = "UTC"
            r = c.execute("SELECT g.timezone FROM regions g " + (
                "WHERE g.key=?" if system and system.startswith("region:") else
                "JOIN systems y ON y.region_id=g.id WHERE y.key=?" if system else "LIMIT 1"),
                ([system[7:]] if system and system.startswith("region:") else [system] if system else [])).fetchone()
            if r:
                tz = r[0] or "UTC"
            first = c.execute("SELECT MIN(r.ts) FROM readings r JOIN sensors x ON x.id=r.sensor_id WHERE x.quantity='state'").fetchone()[0] or end
            start = first if not days else end - int(days) * 86400
            snap = [s["key"] for s in self.cfg.sources if s.get("type") == "stream_edm"]
            out = spills.report(c, start, end, end, system, tz, snap)
            if out["complete_from"]:
                out["complete_from"] = min(out["complete_from"], end)
            out["days"] = int(days or 0)
            return out

    def unmapped(self):
        with self.ro() as c:
            return {s: dbm.meta_get(c, f"unmapped:{s}", {}) for s in sorted(self.known_sources)}

    # ---------- changes (one writer at a time) ----------
    def setup(self, body):
        action, kind = body.get("action", "save"), body.get("kind", "")
        actor = body.get("actor", "")
        key = str(body.get("key", "") or (body.get("data") or {}).get("key", ""))
        with self.lock:
            if action == "save":
                data = body.get("data")
                if not isinstance(data, dict):
                    raise model.SetupError(["data should be an object"])
                if model.is_live(self.conn, kind, data.get("key")):
                    return model.propose(self.conn, kind, data, actor, self.known_sources)
                return model.save(self.conn, kind, data, actor, self.known_sources)
            if action == "apply":
                return model.apply_pending(self.conn, kind, key, actor, self.known_sources)
            if action == "discard":
                return model.discard_pending(self.conn, kind, key, actor)
            if action == "retire":
                return model.retire(self.conn, kind, key, actor, str(body.get("reason", ""))[:300])
            if action == "go_live":
                res = model.go_live(self.conn, kind, key, actor, self.known_sources)
                alerts.evaluate(self.conn, self.cfg)
                return res
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
            return ingest.manual_reading_and_alerts(self.conn, self.cfg, str(body.get("sensor", "")), v, body.get("actor", ""),
                                                    body.get("unit"), body.get("ts"))

    def alert_action(self, what, body):
        try:
            aid = int(body.get("id"))
        except (TypeError, ValueError):
            raise ValueError("which alert? (id)")
        with self.lock:
            if what == "ack":
                return alerts.acknowledge(self.conn, aid, body.get("actor", ""), str(body.get("note", "")))
            if what == "shelve":
                return alerts.shelve(self.conn, aid, body.get("actor", ""), body.get("hours"), body.get("reason", ""))
            return alerts.unshelve(self.conn, aid, body.get("actor", ""))


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

        def handle_one_request(self):
            """Never drop a connection on an unexpected error: answer 500 and log it."""
            try:
                super().handle_one_request()
            except (ConnectionError, TimeoutError):
                raise
            except Exception:
                import traceback
                traceback.print_exc()
                try:
                    self._json({"error": "Something went wrong on the server; it has been logged."}, 500)
                except Exception:
                    pass

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            p = u.path
            try:
                if p == "/api/health":
                    return self._json({"ok": True, "version": __version__, "started_ms": STARTED})
                if p == "/api/config":
                    return self._json(app.config())
                if p == "/api/tree":
                    return self._json(app.tree())
                if p == "/api/map":
                    return self._json(app.map(q.get("system") or None))
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
                    r = app.readings(unquote(m.group(1)), _int(q, "from"), _int(q, "to"), q.get("res", "auto"))
                    return self._json(r) if r else self._json({"error": "no such sensor"}, 404)
                if p == "/api/alerts":
                    return self._json(app.alerts(q.get("system") or None, q.get("shelved") == "1",
                                                 min(_int(q, "ended_hours") or 0, 24 * 90)))
                if p == "/api/spills":
                    d = _int(q, "days")
                    return self._json(app.spills(q.get("system") or None, 30 if d is None else max(0, min(d, 3650))))
                if p == "/api/setup/item":
                    if q.get("kind") not in model.KINDS:
                        raise ValueError("kind?")
                    with app.ro() as c:
                        r = model.item(c, q["kind"], q.get("key", ""))
                    return self._json(r) if r else self._json({"error": "not found"}, 404)
                if p == "/api/setup/pending":
                    with app.ro() as c:
                        return self._json(model.pending(c))
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
                if not p.startswith("/api/"):
                    return self._static(p.lstrip("/"))
                return self._json({"error": "not found"}, 404)
            except ValueError as e:
                return self._json({"error": str(e)}, 400)

        def _static(self, name):
            f = (WEB / name).resolve()
            if WEB.resolve() not in f.parents or not f.is_file():
                return self._json({"error": "not found"}, 404)
            body = f.read_bytes()
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/javascript", "application/json")) else ""))
            self.send_header("Cache-Control", "no-cache")
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
                m = re.fullmatch(r"/api/alerts/(ack|shelve|unshelve)", p)
                if m:
                    return self._json(app.alert_action(m.group(1), body))
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
