"""The API end to end over HTTP: reads, the setup endpoint (passcode, name, CSRF), readings at
raw and rolled-up resolution, manual readings, and the audit trail."""
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from helpers import ACTOR, CSV_SOURCE, basic_network, make_env

from waterworks_desk import ingest, server


def start(passcode="letmein"):
    tmp, cfg, conn = make_env(CSV_SOURCE)
    cfg.data["server"]["write_passcode"] = passcode
    basic_network(conn)
    rows = ["Timestamp,PS1_P1_PT,PS1_FT,PS1_LT"]
    t0 = int(time.time()) // 3600 * 3600 - 6 * 3600
    for i in range(6 * 60):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t0 + 60 * i + 19800))   # source is UTC+05:30
        rows.append(f"{ts},{4 + (i % 10) / 10},{100 + i % 7},{1.2}")
    f = tmp / "data/import/s.csv"
    f.write_text("\n".join(rows) + "\n")
    os.utime(f, (time.time() - 60, time.time() - 60))
    ingest.run(cfg, conn, log=lambda m: None)
    app = server.App(cfg)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}", t0


def get(base, path):
    req = urllib.request.Request(base + path, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req) as r:
        body = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return json.loads(body)


def post(base, path, body, passcode="letmein", ctype="application/json", origin=None):
    h = {"Content-Type": ctype}
    if passcode is not None:
        h["X-Passcode"] = passcode
    if origin:
        h["Origin"] = origin
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_reads():
    httpd, base, t0 = start()
    try:
        assert get(base, "/api/health")["ok"]
        tree = get(base, "/api/tree")
        sysd = tree["regions"][0]["systems"][0]
        assert sysd["name"] == "Central drinking water" and sysd["sites"] == 1 and sysd["sensors"] == 3
        site = get(base, "/api/sites/ps1")
        assert [c["name"] for c in site["components"]] == ["Pump 1", "Pump 2"]
        wl = next(s for s in site["sensors"] if s["key"] == "ps1-wl")
        assert wl["latest"]["value"] == 1.2
        assert abs(wl["latest"]["level_above_datum_m"] - (-1.3 + 1.2)) < 1e-9     # zero point + reading
        raw = get(base, f"/api/sensors/ps1-flow/readings?from={t0}&to={t0 + 3600}")
        assert raw["res"] == "raw" and len(raw["t"]) == 61 and raw["unit"] == "m3/h" and raw["display"]["unit"] == "l/s"
        hourly = get(base, f"/api/sensors/ps1-flow/readings?from={t0}&to={t0 + 6 * 3600}&res=3600")
        assert hourly["res"] == 3600 and hourly["n"][:5] == [60] * 5
        assert get(base, "/api/sensors/ps1-flow/readings?from=0&to=99999999999")["res"] == "raw"   # few points: raw, whatever the span
        assert get(base, "/api/imports")[0]["readings_stored"] == 6 * 60 * 3
        assert get(base, "/api/log/verify")["ok"]
        exp = get(base, "/api/setup/export")
        assert len(exp["sensors"]) == 3
        try:
            get(base, "/api/sites/nope")
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
        try:
            get(base, "/api/sensors/ps1-flow/readings?from=nan")
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        httpd.shutdown()


def test_changes_need_passcode_name_and_same_origin():
    httpd, base, _ = start()
    try:
        body = {"action": "save", "kind": "site", "actor": "Minh Tran",
                "data": {"key": "t9", "system": "dw", "name": "North tank", "site_type": "tank", "lat": 45.85, "lon": 7.75}}
        assert post(base, "/api/setup", body, passcode="wrong")[0] == 401
        assert post(base, "/api/setup", body, passcode=None)[0] == 401
        assert post(base, "/api/setup", body, ctype="text/plain")[0] == 403          # a cross-site form can't send JSON
        assert post(base, "/api/setup", body, origin="http://evil.example")[0] == 403
        code, res = post(base, "/api/setup", {**body, "actor": ""})
        assert code == 422 and "a name is required" in res["problems"][0]
        code, res = post(base, "/api/setup", body)
        assert code == 200 and res["created"]
        code, res = post(base, "/api/setup", {**body, "data": {**body["data"], "key": "t10", "lat": 91}})
        assert code == 422 and res["error"] == "Not saved." and "latitude" in res["problems"][0]
        log = get(base, "/api/log")
        assert log[-1]["actor"] == "Minh Tran" and "North tank" in log[-1]["msg"]
        code, res = post(base, "/api/setup", {"action": "go_live", "kind": "system", "key": "dw", "actor": "Minh Tran"})
        assert code == 200 and res["live"] >= 8
        code, res = post(base, "/api/readings/manual", {"sensor": "ps1-wl", "value": 85, "unit": "cm", "actor": "Field Tech"})
        assert code == 200 and abs(res["value"] - 0.85) < 1e-9
        code, res = post(base, "/api/setup/import", {"actor": "Minh Tran", "bundle": {"zones": [
            {"key": "z1", "system": "dw", "name": "North pressure zone", "kind": "pressure zone"}]}})
        assert code == 200 and res["counts"] == {"zone": 1}
        assert get(base, "/api/log/verify")["ok"]
    finally:
        httpd.shutdown()


def test_no_passcode_still_needs_a_name():
    httpd, base, _ = start(passcode="")
    try:
        code, res = post(base, "/api/setup", {"action": "retire", "kind": "component", "key": "ps1-p2"}, passcode=None)
        assert code == 422 and "a name is required" in res["problems"][0]
        code, res = post(base, "/api/setup", {"action": "retire", "kind": "component", "key": "ps1-p2", "actor": ACTOR,
                                              "reason": "replaced"}, passcode=None)
        assert code == 200
    finally:
        httpd.shutdown()
