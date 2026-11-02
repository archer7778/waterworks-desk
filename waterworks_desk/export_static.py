"""Write a read-only snapshot of the console as static files, for the public demo on GitHub Pages.

    python -m waterworks_desk.export_static --config demo/config.toml --out site

Every API response the console needs is saved as JSON next to a copy of the console page, which
reads them instead of a live server. Charts get raw readings for the last 8 days, hourly values
for the last 95 days and daily values for everything. Changes are switched off.
"""
import argparse
import json
import shutil
import time
from pathlib import Path

from . import __version__, alerts, logchain, model
from . import db as dbm
from .config import find_config
from .server import WEB, App, _finite


def _w(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_finite(obj), separators=(",", ":"), allow_nan=False, ensure_ascii=False), encoding="utf-8")


def export(cfg, out, log=print):
    out = Path(out)
    api = out / "api"
    if api.exists():
        shutil.rmtree(api)
    t0 = time.time()
    app = App(cfg)
    clock = app.clock_ms() // 1000
    conf = app.config()
    conf.update(static=True, write_protected=False)
    _w(api / "config.json", conf)
    _w(api / "tree.json", app.tree())
    m = app.map()
    _w(api / "map.json", m)
    clocks = app.clocks()                                   # each system judged at its own time (demo mode)
    with app.ro() as c:
        everything = alerts.active(c, app.clock_ms(), None, True, 24 * 3650, limit=20000)
    _w(api / "alerts.json", [a for a in everything if a["end_ms"] is None or a["end_ms"] > clocks.get(a["system"], clock * 1000) - 30 * 86400_000])
    for s in m["sites"]:
        _w(api / "sites" / f"{s['key']}.json", app.site(s["key"], everything, clocks.get(s["system"], clock * 1000)))
    with app.ro() as c:
        sensors = [(r[0], r[1]) for r in c.execute("SELECT x.key, y.key FROM sensors x JOIN sites s ON s.id=x.site_id "
                                                  "JOIN systems y ON y.id=s.system_id WHERE x.status!='retired'")]
        _w(api / "log.json", logchain.entries(c))
        _w(api / "log" / "verify.json", logchain.verify(c))
        _w(api / "setup" / "export.json", model.export_bundle(c))
    _w(api / "setup" / "pending.json", [])
    _w(api / "setup" / "unmapped.json", {})
    for days in (7, 30, 365, 0):
        _w(api / f"spills-{days}.json", app.spills(None, days))
    with app.ro() as c:
        states = {r[0] for r in c.execute("SELECT key FROM sensors WHERE quantity='state'")}
    for key, system in sensors:
        d, end = api / "sensors" / key, clocks.get(system, clock * 1000) // 1000
        if key in states:   # on/off readings are few: every file holds all of them
            r = app.readings(key, 0, end, "raw")
            for f in ("raw", "3600", "86400"):
                _w(d / f"{f}.json", r)
            continue
        _w(d / "raw.json", app.readings(key, end - 8 * 86400, end, "raw"))
        _w(d / "3600.json", app.readings(key, end - 95 * 86400, end, 3600))
        _w(d / "86400.json", app.readings(key, 0, end, 86400))
    page = (WEB / "index.html").read_text(encoding="utf-8")
    inject = f'<script>window.WWD_STATIC={{"base":"api/","snapshot":{clock}}};</script>\n'
    page = page.replace('<script src="vendor/leaflet/leaflet.js"></script>', inject + '<script src="vendor/leaflet/leaflet.js"></script>', 1)
    (out / "index.html").write_text(page, encoding="utf-8")
    for sub in ("vendor", "i18n"):
        if (out / sub).exists():
            shutil.rmtree(out / sub)
        shutil.copytree(WEB / sub, out / sub)
    (out / ".nojekyll").write_text("")
    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    log(f"Exported a read-only snapshot to {out} ({size / 1e6:.1f} MB, {len(m['sites'])} sites, {len(sensors)} sensors) "
        f"in {time.time() - t0:.1f}s, Waterworks Desk {__version__}.")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Export a read-only static demo of the console")
    ap.add_argument("--config")
    ap.add_argument("--out", default="site")
    a = ap.parse_args(argv)
    export(find_config(a.config), a.out)


if __name__ == "__main__":
    main()
