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

from . import __version__, logchain, model
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
    _w(api / "alerts.json", app.alerts(include_shelved=True, include_ended_hours=24 * 30))
    for s in m["sites"]:
        _w(api / "sites" / f"{s['key']}.json", app.site(s["key"]))
    with app.ro() as c:
        sensors = [r[0] for r in c.execute("SELECT key FROM sensors WHERE status!='retired'")]
        _w(api / "log.json", logchain.entries(c))
        _w(api / "log" / "verify.json", logchain.verify(c))
        _w(api / "setup" / "export.json", model.export_bundle(c))
    _w(api / "setup" / "pending.json", [])
    _w(api / "setup" / "unmapped.json", {})
    for key in sensors:
        d = api / "sensors" / key
        _w(d / "raw.json", app.readings(key, clock - 8 * 86400, clock, "raw"))
        _w(d / "3600.json", app.readings(key, clock - 95 * 86400, clock, 3600))
        _w(d / "86400.json", app.readings(key, 0, clock, 86400))
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
