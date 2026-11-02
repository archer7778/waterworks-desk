"""Read new readings from every configured source, convert them to canonical units, store them,
update the rollups and apply the retention policy.

Run by a systemd timer every few minutes. Safe to run by hand:
    python -m waterworks_desk.ingest                    # read every enabled source
    python -m waterworks_desk.ingest --source exports   # just one
    python -m waterworks_desk.ingest --force            # re-read files even if unchanged
    python -m waterworks_desk.ingest --setup site.json --actor "A. Name"   # import a setup file
    python -m waterworks_desk.ingest --export setup.json                    # export the setup
    python -m waterworks_desk.ingest --go-live SYSTEM_KEY --actor "A. Name"
    python -m waterworks_desk.ingest --verify-log       # check the signed audit trail
"""
import argparse
import fcntl
import json
import sys
import time
from datetime import datetime, timezone

from . import db as dbm
from . import alerts, logchain, model, notify, sources, units
from .config import find_config

CHUNK = 20_000
DAY = 86_400


def sensor_map(conn, source_key):
    """tag -> (sensor id, converter to canonical) for a source's sensors that aren't retired."""
    out = {}
    for r in conn.execute("SELECT id, source_tag, quantity, source_unit, unit FROM sensors "
                          "WHERE source=? AND source_tag IS NOT NULL AND status!='retired'", (source_key,)):
        out[r["source_tag"]] = (r["id"], units.converter(r["quantity"], r["source_unit"] or r["unit"]))
    return out


def store(conn, source_key, readings, smap=None):
    """Store (tag, ts, value, quality) tuples. Returns (stored, unmapped {tag: count}, spans
    {sensor_id: [first_ts, last_ts]}). Re-reading the same data overwrites, never duplicates."""
    smap = sensor_map(conn, source_key) if smap is None else smap
    unmapped, spans, buf, stored = {}, {}, [], 0
    sql = ("INSERT INTO readings(sensor_id,ts,value,quality) VALUES(?,?,?,?) "
           "ON CONFLICT(sensor_id,ts) DO UPDATE SET value=excluded.value, quality=excluded.quality")
    for tag, ts, v, q in readings:
        m = smap.get(tag)
        if m is None:
            unmapped[tag] = unmapped.get(tag, 0) + 1
            continue
        sid, conv = m
        buf.append((sid, ts, conv(v), q))
        sp = spans.get(sid)
        if sp is None:
            spans[sid] = [ts, ts]
        else:
            if ts < sp[0]:
                sp[0] = ts
            if ts > sp[1]:
                sp[1] = ts
        if len(buf) >= CHUNK:
            conn.executemany(sql, buf)
            stored += len(buf)
            buf.clear()
    if buf:
        conn.executemany(sql, buf)
        stored += len(buf)
    return stored, unmapped, spans


def rollup(conn, spans):
    """Recompute the 1-minute, 15-minute and hourly buckets touched by new readings. A bucket no
    longer than the sensor's own reporting interval would just copy the raw readings, so it's skipped
    (5-minute data gets 15-minute and hourly rollups, not 1-minute ones)."""
    for sid, (lo, hi) in spans.items():
        r = conn.execute("SELECT expected_interval_s FROM sensors WHERE id=?", (sid,)).fetchone()
        every = (r[0] if r else None) or 0
        for res in dbm.ROLLUP_RES:
            if res <= every:
                continue
            a, b = lo // res * res, hi // res * res + res
            conn.execute("INSERT OR REPLACE INTO rollups(res,sensor_id,ts,n,mean,lo,hi) "
                         "SELECT ?, sensor_id, (ts/?)*?, COUNT(*), AVG(value), MIN(value), MAX(value) FROM readings "
                         "WHERE sensor_id=? AND ts>=? AND ts<? AND quality!=? GROUP BY 3",
                         (res, res, res, sid, a, b, dbm.BAD))


def retention(conn, cfg, now=None):
    """Drop raw readings and 1-minute rollups past their keep-for times (rollups are made first,
    so history survives as 15-minute and hourly values). Deleting per sensor uses the key."""
    now = now or time.time()
    raw_days, minute_days = cfg["storage"]["raw_days"], cfg["storage"]["minute_days"]
    gone = 0
    ids = [r[0] for r in conn.execute("SELECT id FROM sensors")]
    for sid in ids:
        if raw_days > 0:
            gone += conn.execute("DELETE FROM readings WHERE sensor_id=? AND ts<?", (sid, int(now - raw_days * DAY))).rowcount
        if minute_days > 0:
            conn.execute("DELETE FROM rollups WHERE res=60 AND sensor_id=? AND ts<?", (sid, int(now - minute_days * DAY)))
    conn.commit()
    return gone


def note_unmapped(conn, source_key, unmapped):
    """Remember tags that arrive with no sensor set up, so the setup screen can offer to add them."""
    if not unmapped:
        return
    key = f"unmapped:{source_key}"
    seen = dbm.meta_get(conn, key, {}) or {}
    t = dbm.now_ms()
    for tag, n in unmapped.items():
        e = seen.get(tag) or {"first_ms": t, "count": 0}
        e["count"] += n
        e["last_ms"] = t
        seen[tag] = e
    if len(seen) > 5000:
        seen = dict(sorted(seen.items(), key=lambda kv: -kv[1]["last_ms"])[:5000])
    dbm.meta_set(conn, key, seen)


def run(cfg, conn, only=None, force=False, log=print):
    total = 0
    for src in cfg.sources:
        if not src["enabled"] or (only and src["key"] != only):
            continue
        ad = sources.make(cfg, src)
        smap = sensor_map(conn, src["key"])
        try:
            batches = ad.batches(conn, force=force)
            for b in batches:
                try:
                    stored, unmapped, spans = store(conn, src["key"], b.readings, smap)
                    rollup(conn, spans)
                    conn.commit()
                except Exception as e:   # one bad file never stops the others
                    conn.rollback()
                    dbm.record_import(conn, src["key"], b.item, False, b.rows_seen, 0, b.skipped,
                                      f"{type(e).__name__}: {e}", b.digest)
                    log(f"[{src['key']}] {b.item}: failed: {type(e).__name__}: {e}")
                    continue
                note_unmapped(conn, src["key"], unmapped)
                msg = "; ".join(b.problems)
                if unmapped:
                    msg = (f"{len(unmapped)} tag(s) have no sensor set up yet: {', '.join(sorted(unmapped)[:6])}"
                           f"{'…' if len(unmapped) > 6 else ''}. " + msg)
                ok = b.skipped == 0 or stored > 0 or bool(unmapped)   # partly readable still counts; nothing readable doesn't
                dbm.record_import(conn, src["key"], b.item, ok, b.rows_seen, stored, b.skipped, msg.strip(), b.digest)
                ad.remember(conn, b)
                total += stored
                log(f"[{src['key']}] {b.item}: {b.rows_seen} rows, {stored} readings stored"
                    f"{f', {b.skipped} skipped' if b.skipped else ''}{f', {len(unmapped)} unmapped tags' if unmapped else ''}.")
        except Exception as e:
            dbm.record_import(conn, src["key"], "(source)", False, message=f"{type(e).__name__}: {e}")
            log(f"[{src['key']}] failed: {type(e).__name__}: {e}")
    gone = retention(conn, cfg)
    if gone:
        log(f"Retention: removed {gone} raw readings past {cfg['storage']['raw_days']} days (rollups kept).")
    changes = alerts.evaluate(conn, cfg, log=log)
    notify.alerts_changed(cfg, changes, log)
    return total


def manual_reading(conn, sensor_key, value, actor, unit=None, ts=None):
    """A reading typed in by a person (a gauge board, a run-hours meter, a chlorine test)."""
    actor = model._actor(actor)
    r = conn.execute("SELECT * FROM sensors WHERE key=?", (sensor_key,)).fetchone()
    if r is None or r["status"] == "retired":
        raise model.SetupError([f"there is no sensor {sensor_key!r}"])
    if not units.finite(value):
        raise model.SetupError(["the reading must be a number"])
    ts = int(ts if ts is not None else time.time())
    if ts > time.time() + 300:
        raise model.SetupError(["the reading's time is in the future"])
    v = units.to_canonical(r["quantity"], unit or r["display_unit"] or r["unit"], value)
    stored, _, spans = store(conn, "manual", [("x", ts, v, dbm.MANUAL)], {"x": (r["id"], lambda x: x)})
    rollup(conn, spans)
    conn.commit()
    dbm.record_import(conn, "manual", sensor_key, True, 1, stored, 0, f"entered by {actor}")
    return {"sensor": sensor_key, "ts": ts, "value": v, "unit": r["unit"]}


def manual_reading_and_alerts(conn, cfg, *a, **k):
    res = manual_reading(conn, *a, **k)
    notify.alerts_changed(cfg, alerts.evaluate(conn, cfg), None)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Waterworks Desk: read sources, import setup files")
    ap.add_argument("--config")
    ap.add_argument("--source")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--setup", metavar="FILE", help="import a setup file (JSON bundle)")
    ap.add_argument("--live", action="store_true", help="with --setup: put the imported systems live")
    ap.add_argument("--export", metavar="FILE")
    ap.add_argument("--go-live", metavar="SYSTEM_KEY")
    ap.add_argument("--actor", help="your name, for the audit trail")
    ap.add_argument("--verify-log", action="store_true")
    a = ap.parse_args(argv)
    cfg = find_config(a.config)
    conn = dbm.open_db(cfg)
    known = {s["key"] for s in cfg.sources}
    try:
        if a.verify_log:
            v = logchain.verify(conn)
            print(json.dumps(v, indent=1))
            return 0 if v["ok"] else 1
        if a.export:
            with open(a.export, "w", encoding="utf-8") as f:
                json.dump(model.export_bundle(conn), f, indent=1, ensure_ascii=False)
            print(f"Wrote {a.export}")
            return 0
        if a.setup or a.go_live:
            if not a.actor:
                raise SystemExit("Give your name with --actor, so the audit trail says who made the change.")
            if a.setup:
                with open(a.setup, encoding="utf-8") as f:
                    res = model.import_bundle(conn, json.load(f), a.actor, known, a.live, source_name=a.setup)
                print("Imported:", ", ".join(f"{n} {k}" for k, n in res["counts"].items()))
            else:
                res = model.go_live(conn, "system", a.go_live, a.actor, known)
                print(f"{res['live']} item(s) are now live.")
            notes = res.get("warnings", [])
            for w in notes[:5]:
                print("  note:", w)
            if len(notes) > 5:
                print(f"  … and {len(notes) - 5} more notes (the same checks show on each item in the console).")
            return 0
    except model.SetupError as e:
        print("Not saved. Problems:", *[f"  - {m}" for m in e.errors], sep="\n", file=sys.stderr)
        for w in e.warnings:
            print("  note:", w, file=sys.stderr)
        return 2
    lock_path = cfg.db_path.parent / "ingest.lock"
    with open(lock_path, "a") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another ingest run is in progress; leaving it to finish.")
            return 0
        stamp = lambda m: print(f"{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%SZ} {m}", flush=True)
        n = run(cfg, conn, a.source, a.force, stamp)
        stamp(f"Done: {n} readings stored.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
