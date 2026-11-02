"""Alerts: critical, warning and advisory, each one plain sentence with what, where, since when,
why it matters and what to check first. They sit beside the utility's SCADA alarms and never
replace them, so they are always called alerts.

Rules in M1
  limit_high / limit_low   a reading past its warning (warning) or alarm (critical) limit for
                           at least two readings in a row; clears after two readings back inside.
                           If the same problem comes back within 24 hours its alert reopens and
                           counts the return, so a daily dip is one alert, not one a day
  silent                   a live sensor that has stopped reporting (3x its usual interval)
  site_silent              every live sensor at a site silent: one alert instead of many
  near_overflow / over_overflow   a level at a well, tank or weir close to or over its overflow
  tank_low                 a tank or reservoir nearly empty
  source_failing           a data source failing three runs in a row

Severity starts from the rule and is then moved by criticality (set during setup): criticality
1 raises an advisory to a warning; criticality 3 lowers a critical to a warning and a warning to
an advisory. The alert records why.

Only live sensors and sites alert. Every acknowledgement and shelve is written to the signed
audit trail with the person's name.
"""
import json
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import db as dbm
from . import logchain, units

PERSIST = 2                     # readings in a row needed to open or close a limit alert
REOPEN_S = 24 * 3600            # the same problem back within this long reopens its alert (one alert per problem)
SEV_ORDER = {"advisory": 0, "warning": 1, "critical": 2}
SEV_BY_ORDER = {v: k for k, v in SEV_ORDER.items()}

# Templates: English here (stored as the message and sent to phones); the console renders the
# same template keys in the viewer's language from web/i18n/*.json.
T = {
    "limit_high": "{sensor} at {site} is {value}, above its {level} limit of {limit}, since {since}. {why} First check: {check}",
    "limit_low": "{sensor} at {site} is {value}, below its {level} limit of {limit}, since {since}. {why} First check: {check}",
    "silent": "No readings from {sensor} at {site} since {since} (it normally reports every {every}). Its alerts can't work while it's silent. First check: the logger's power and communications, and the data export.",
    "site_silent": "Nothing has been heard from {site} since {since}: all {n} of its sensors are silent. It may have lost power or communications. First check: the site's power and telemetry link.",
    "over_overflow": "{sensor} at {site} is at {value}, at or over the overflow level, since {since}. It may be spilling. First check: pump status and inflow, and look for a spill.",
    "near_overflow": "{sensor} at {site} is {value}, within {margin} of the overflow level, since {since}. It could spill if it keeps rising. First check: whether the pumps are running and keeping up.",
    "tank_low": "{site} is down to {value} ({pct}% full) since {since}. Supply from it could fail if it keeps falling. First check: the inlet and the pumps that fill it.",
    "source_failing": "The data source {source} has failed {n} times in a row, last at {since}: {message} Readings from it are missing until it works. First check: the export, file share or connection.",
}
WHY = {  # (quantity, direction) -> (why it matters, first check)
    ("pressure", "low"): ("Low pressure can mean a burst main, a pump trip or a closed valve.", "pump status and nearby pressures."),
    ("pressure", "high"): ("High pressure stresses mains and can cause bursts.", "pressure-reducing valves and pump speed."),
    ("flow", "high"): ("Unusually high flow can mean a burst or a meter fault.", "night flow and nearby pressures."),
    ("flow", "low"): ("Low flow can mean a pump trip, a closed valve or a blockage.", "pump status and valve positions."),
    ("level", "high"): ("A high level risks an overflow or spill.", "pumps running and inflow."),
    ("level", "low"): ("A low level risks running dry or losing supply.", "inflow and the pumps that fill it."),
    ("current", "high"): ("High current can mean wear, ragging or a blockage.", "the pump's trip log and the wet-well level."),
    ("temperature", "high"): ("Overheating shortens equipment life and can trip it.", "cooling, ventilation and load."),
    ("chlorine", "low"): ("Low chlorine can let bacteria survive in the network.", "dosing pumps and the chlorine stock."),
    ("chlorine", "high"): ("High chlorine causes complaints and may break the limit.", "the dosing rate."),
    ("turbidity", "high"): ("High turbidity can shield bacteria from disinfection.", "filters and the raw water."),
}
DEFAULT_WHY = ("It is outside the range set for it.", "the sensor and the equipment it measures.")


def clock(cfg, conn):
    """The time alerts are judged at: now, or for a historical demo the end of the data."""
    mode = str((cfg.data.get("alerts") or {}).get("clock", "now")).strip()
    if mode == "now":
        return int(time.time())
    if mode == "data_end":
        t = conn.execute("SELECT MAX(ts) FROM readings").fetchone()[0]
        return int(t) if t else int(time.time())
    return int(datetime.fromisoformat(mode.replace("Z", "+00:00")).timestamp())


def _fmt(q, unit, v, display_unit):
    u = display_unit or unit
    try:
        x = units.from_canonical(q, u, v)
    except (KeyError, ZeroDivisionError):
        x, u = v, unit
    dp = 0 if abs(x) >= 1000 else 1 if abs(x) >= 10 else 2
    return f"{x:.{dp}f} {u}".replace("m3/h", "m³/h").strip()


def _when(ts, now, tz):
    d = datetime.fromtimestamp(ts, tz)
    n = datetime.fromtimestamp(now, tz)
    return d.strftime("%H:%M") if d.date() == n.date() else d.strftime("%-d %b %H:%M")


def _adjust(sev, crit):
    o = SEV_ORDER[sev]
    if crit == 1 and o == 0:
        return "warning", "raised: criticality 1"
    if crit == 3 and o > 0:
        return SEV_BY_ORDER[o - 1], "lowered: criticality 3"
    return sev, None


def _beyond(v, lo, hi):
    return (lo is not None and v < lo) or (hi is not None and v > hi)


def _since_breach(conn, sid, now, lo, hi):
    """Start of the current run of readings beyond (lo, hi): the first one after the last reading inside."""
    conds, args = [], []
    if lo is not None:
        conds.append("value>=?")
        args.append(lo)
    if hi is not None:
        conds.append("value<=?")
        args.append(hi)
    inside = conn.execute(f"SELECT MAX(ts) FROM readings WHERE sensor_id=? AND ts<=? AND quality!=? AND {' AND '.join(conds)}",
                          (sid, now, dbm.BAD, *args)).fetchone()[0]
    r = conn.execute("SELECT MIN(ts) FROM readings WHERE sensor_id=? AND ts>? AND ts<=? AND quality!=?",
                     (sid, inside if inside is not None else -1, now, dbm.BAD)).fetchone()[0]
    return r or now


def conditions(conn, now, held=None):
    """Everything that should be alerting at `now`: {ident: alert dict}. `held` is the open
    alerts {ident: row}: a limit alert stays open until two readings in a row are back inside."""
    held = held or {}
    out = {}
    regions = {}
    sensors = conn.execute(
        "SELECT x.*, s.name site_name, s.key site_key, s.id sid_site, s.criticality site_crit, s.site_type, "
        "s.floor_level_m, s.overflow_level_m, s.system_id, c.criticality comp_crit, r.timezone tz "
        "FROM sensors x JOIN sites s ON s.id=x.site_id JOIN systems y ON y.id=s.system_id "
        "JOIN regions r ON r.id=y.region_id LEFT JOIN components c ON c.id=x.component_id "
        "WHERE x.status='live' AND s.status='live' AND y.status='live'").fetchall()
    by_site = {}
    for x in sensors:
        tz = regions.setdefault(x["tz"], ZoneInfo(x["tz"] or "UTC"))
        crit = x["comp_crit"] or x["site_crit"] or 2
        base = {"system_id": x["system_id"], "sensor_id": x["id"], "site_id": x["sid_site"],
                "component_id": x["component_id"]}
        last = conn.execute("SELECT ts, value FROM readings WHERE sensor_id=? AND ts<=? AND quality!=? "
                            "ORDER BY ts DESC LIMIT ?", (x["id"], now, dbm.BAD, PERSIST)).fetchall()
        by_site.setdefault(x["sid_site"], []).append((x, last))
        every = x["expected_interval_s"] or 300
        if not last:
            continue
        p = {"sensor": x["name"], "site": x["site_name"]}
        # silent
        if now - last[0]["ts"] > max(3 * every, 900):
            sev, why = _adjust("warning", crit)
            out[f"silent:{x['id']}"] = {**base, "rule": "silent", "severity": sev, "start": last[0]["ts"], "adj": why,
                                        "t": "silent", "p": {**p, "since": last[0]["ts"],
                                                             "every": f"{every // 60} min" if every >= 60 else f"{every} s",
                                                             "every_s": every}}
            continue
        fmt = lambda v: _fmt(x["quantity"], x["unit"], v, x["display_unit"])
        # limits: alarm first, then warning. An open alert stays open (at its current level) until
        # two readings in a row are back inside every limit.
        bands = (("alarm", x["alarm_lo"], x["alarm_hi"], "critical"), ("warning", x["warn_lo"], x["warn_hi"], "warning"))
        lo_all = max([v for v in (x["alarm_lo"], x["warn_lo"]) if v is not None], default=None)
        hi_all = min([v for v in (x["alarm_hi"], x["warn_hi"]) if v is not None], default=None)
        opened = None
        for rule in ("limit_high", "limit_low"):
            if f"{rule}:{x['id']}" in held:
                opened = rule
        still = opened and not (len(last) >= PERSIST and not any(_beyond(r["value"], lo_all, hi_all) for r in last))
        for level, lo, hi, sev0 in bands:
            if lo is None and hi is None:
                continue
            need = 1 if still else PERSIST
            if len(last) >= need and all(_beyond(r["value"], lo, hi) for r in last[:need]):
                high = hi is not None and last[0]["value"] > hi
                wk = (x["quantity"], "high" if high else "low")
                why, check = WHY.get(wk, DEFAULT_WHY)
                wk = "_".join(wk) if wk in WHY else "default"
                sev, adj = _adjust(sev0, crit)
                start = _since_breach(conn, x["id"], now, lo, hi)
                rule = "limit_high" if high else "limit_low"
                out[f"{rule}:{x['id']}"] = {**base, "rule": rule, "severity": sev, "start": start, "adj": adj, "t": rule,
                                            "p": {**p, "value": fmt(last[0]["value"]), "level": level,
                                                  "limit": fmt(hi if high else lo), "since": start, "why": why, "check": check, "wk": wk}}
                break
        else:
            if still:   # one reading back inside, not two yet: keep it open as it was, with the new value
                h = held[f"{opened}:{x['id']}"]
                d = json.loads(h["data_json"])
                d["p"]["value"] = fmt(last[0]["value"])
                out[f"{opened}:{x['id']}"] = {**base, "rule": opened, "severity": h["severity"], "start": h["start_ms"] // 1000,
                                               "adj": d.get("adj"), "t": d["t"], "p": d["p"]}
        # overflow and tank levels (level sensors with a zero point, at sites with levels set)
        if x["quantity"] == "level" and x["mount_level_m"] is not None and x["overflow_level_m"] is not None:
            lvl = x["mount_level_m"] + last[0]["value"]
            depth = (x["overflow_level_m"] - x["floor_level_m"]) if x["floor_level_m"] is not None else None
            margin = max(0.2, 0.1 * depth) if depth else 0.3
            show = lambda v: f"{v:.2f} m"
            if lvl >= x["overflow_level_m"]:
                sev, adj = _adjust("critical", crit)
                start = _since_breach(conn, x["id"], now, None, x["overflow_level_m"] - x["mount_level_m"])
                out[f"over_overflow:{x['id']}"] = {**base, "rule": "over_overflow", "severity": sev, "start": start, "adj": adj,
                                                   "t": "over_overflow", "p": {**p, "value": show(last[0]["value"]), "since": start}}
            elif lvl >= x["overflow_level_m"] - margin and x["site_type"] not in ("tank", "reservoir"):
                sev, adj = _adjust("warning", crit)
                start = _since_breach(conn, x["id"], now, None, x["overflow_level_m"] - margin - x["mount_level_m"])
                out[f"near_overflow:{x['id']}"] = {**base, "rule": "near_overflow", "severity": sev, "start": start, "adj": adj,
                                                   "t": "near_overflow", "p": {**p, "value": show(last[0]["value"]),
                                                                               "margin": show(margin), "since": start}}
            if x["site_type"] in ("tank", "reservoir") and depth:
                pct = (lvl - x["floor_level_m"]) / depth * 100
                if pct <= 10 and len(last) >= PERSIST:
                    sev, adj = _adjust("warning", crit)
                    lim = x["floor_level_m"] + 0.1 * depth - x["mount_level_m"]
                    start = _since_breach(conn, x["id"], now, lim, None)
                    out[f"tank_low:{x['id']}"] = {**base, "rule": "tank_low", "severity": sev, "start": start, "adj": adj,
                                                  "t": "tank_low", "p": {**p, "value": show(last[0]["value"]),
                                                                         "pct": max(0, round(pct)), "since": start}}
    # a whole site silent: one alert instead of one per sensor
    for site_id, rows in by_site.items():
        ids = [f"silent:{x['id']}" for x, last in rows if last]
        if len(rows) >= 2 and ids and len(ids) == len(rows) and all(i in out for i in ids):
            first = out[ids[0]]
            for i in ids:
                out.pop(i)
            x = rows[0][0]
            sev, adj = _adjust("critical" if (x["site_crit"] or 2) == 1 else "warning", 2)
            out[f"site_silent:{site_id}"] = {**{k: first[k] for k in ("system_id", "site_id")}, "sensor_id": None,
                                             "component_id": None, "rule": "site_silent", "severity": sev,
                                             "start": max(o_last[0]["ts"] for _, o_last in rows if o_last), "adj": adj,
                                             "t": "site_silent", "p": {"site": x["site_name"], "n": len(rows),
                                                                       "since": max(o_last[0]["ts"] for _, o_last in rows if o_last)}}
    return out


def source_conditions(conn, cfg, now):
    out = {}
    for s in cfg.sources:
        rows = conn.execute("SELECT ok, message, t_ms FROM imports WHERE source=? ORDER BY id DESC LIMIT 3", (s["key"],)).fetchall()
        if len(rows) == 3 and not any(r["ok"] for r in rows):
            out[f"source_failing:{s['key']}"] = {"system_id": None, "sensor_id": None, "site_id": None, "component_id": None,
                                                 "rule": "source_failing", "severity": "warning", "start": rows[0]["t_ms"] // 1000,
                                                 "adj": None, "t": "source_failing",
                                                 "p": {"source": s["key"], "n": 3, "since": rows[0]["t_ms"] // 1000,
                                                       "message": (rows[0]["message"] or "").rstrip(".") + "."}}
    return out


def render(t, p, tz, now):
    q = dict(p)
    if "since" in q:
        q["since"] = _when(q["since"], now, tz)
    return T[t].format(**q)


def why_texts():
    """The 'why it matters' and 'first check' sentences by key, for the console's translations."""
    out = {"_".join(k): {"why": w, "check": c} for k, (w, c) in WHY.items()}
    out["default"] = {"why": DEFAULT_WHY[0], "check": DEFAULT_WHY[1]}
    return out


def evaluate(conn, cfg=None, now=None, log=None):
    """Open, update and close alerts to match the network at `now`. Returns what changed."""
    now = int(now if now is not None else (clock(cfg, conn) if cfg else time.time()))
    have = {r["ident"]: r for r in conn.execute("SELECT * FROM alerts WHERE end_ms IS NULL")}
    want = conditions(conn, now, have)
    if cfg is not None:
        want.update(source_conditions(conn, cfg, now))
    tzs = {r["system_id"]: ZoneInfo(r["tz"] or "UTC") for r in conn.execute(
        "SELECT y.id system_id, r.timezone tz FROM systems y JOIN regions r ON r.id=y.region_id")}
    opened, escalated, closed = [], [], []
    for ident, a in want.items():
        tz = tzs.get(a["system_id"], timezone.utc)
        msg = render(a["t"], a["p"], tz, now)
        data = json.dumps({"t": a["t"], "p": a["p"], "adj": a["adj"]}, default=str)
        cur = have.get(ident)
        if cur is None:
            back = conn.execute("SELECT * FROM alerts WHERE ident=? AND end_ms>=? ORDER BY end_ms DESC LIMIT 1",
                                (ident, (now - REOPEN_S) * 1000)).fetchone()
            if back is not None:   # the same problem back again: reopen it rather than start another
                d = json.loads(back["data_json"] or "{}")
                returns = int(d.get("returns", 0)) + 1
                a["p"] = {**a["p"], "since": back["start_ms"] // 1000}
                msg = render(a["t"], a["p"], tz, now)
                data = json.dumps({"t": a["t"], "p": a["p"], "adj": a["adj"], "returns": returns,
                                   "last_clear_ms": back["end_ms"]}, default=str)
                worse = SEV_ORDER[a["severity"]] > SEV_ORDER[back["severity"]]
                sev = a["severity"] if worse else back["severity"]
                conn.execute("UPDATE alerts SET end_ms=NULL, severity=?, message=?, data_json=?" + (", ack_ms=NULL, ack_by=NULL" if worse else "")
                             + " WHERE id=?", (sev, msg, data, back["id"]))
                (escalated if worse else opened).append({"id": back["id"], "severity": sev, "message": msg, "returned": returns})
                continue
            cid = conn.execute("INSERT INTO alerts(severity,rule,ident,system_id,sensor_id,site_id,component_id,message,start_ms,data_json)"
                               " VALUES(?,?,?,?,?,?,?,?,?,?)",
                               (a["severity"], a["rule"], ident, a["system_id"], a["sensor_id"], a["site_id"],
                                a["component_id"], msg, a["start"] * 1000, data)).lastrowid
            opened.append({"id": cid, "severity": a["severity"], "message": msg})
        else:
            old = json.loads(cur["data_json"] or "{}")
            if old.get("returns"):
                a["p"] = {**a["p"], "since": cur["start_ms"] // 1000}
                msg = render(a["t"], a["p"], tz, now)
                data = json.dumps({"t": a["t"], "p": a["p"], "adj": a["adj"], "returns": old["returns"],
                                   "last_clear_ms": old.get("last_clear_ms")}, default=str)
            worse = SEV_ORDER[a["severity"]] > SEV_ORDER[cur["severity"]]
            conn.execute("UPDATE alerts SET severity=?, message=?, data_json=?" + (", ack_ms=NULL, ack_by=NULL" if worse else "")
                         + " WHERE id=?", (a["severity"], msg, data, cur["id"]))
            if worse:
                escalated.append({"id": cur["id"], "severity": a["severity"], "message": msg})
    for ident, cur in have.items():
        if ident not in want:
            end = now
            if cur["sensor_id"] is not None and cur["rule"].startswith(("limit", "near", "over", "tank")):
                r = conn.execute("SELECT MIN(ts) FROM (SELECT ts FROM readings WHERE sensor_id=? AND ts<=? ORDER BY ts DESC LIMIT ?)",
                                 (cur["sensor_id"], now, PERSIST)).fetchone()[0]
                end = r or now
            conn.execute("UPDATE alerts SET end_ms=? WHERE id=?", (end * 1000, cur["id"]))
            closed.append({"id": cur["id"], "severity": cur["severity"], "message": cur["message"]})
    conn.commit()
    if log and (opened or escalated or closed):
        log(f"Alerts: {len(opened)} new, {len(escalated)} worse, {len(closed)} cleared.")
    return {"opened": opened, "escalated": escalated, "closed": closed, "now": now}


def replay(conn, start, end, step=3600, log=None):
    """Judge the network at every step from start to end, to build up the alert history a
    historical dataset would have produced (used by the demo build)."""
    n = 0
    for t in range(int(start), int(end) + 1, int(step)):
        evaluate(conn, None, t)
        n += 1
    if log:
        tot = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        log(f"Replayed {n} steps: {tot} alerts in the history.")


def active(conn, now_ms=None, system=None, include_shelved=False, include_ended_hours=0):
    now_ms = now_ms or dbm.now_ms()
    q = ("SELECT a.*, s.key site_key, s.name site_name, s.lat, s.lon, x.key sensor_key, x.name sensor_name, "
         "y.key system_key, y.name system_name FROM alerts a LEFT JOIN sites s ON s.id=a.site_id "
         "LEFT JOIN sensors x ON x.id=a.sensor_id LEFT JOIN systems y ON y.id=a.system_id WHERE ")
    conds, args = ["(a.end_ms IS NULL" + (" OR a.end_ms>?)" if include_ended_hours else ")")], []
    if include_ended_hours:
        args.append(now_ms - include_ended_hours * 3600_000)
    if not include_shelved:
        conds.append("(a.shelved_until_ms IS NULL OR a.shelved_until_ms<? OR a.end_ms IS NOT NULL)")
        args.append(now_ms)
    if system:
        conds.append("(y.key=? OR a.system_id IS NULL)")
        args.append(system)
    rows = conn.execute(q + " AND ".join(conds) + " ORDER BY a.end_ms IS NOT NULL, "
                        "CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, a.start_ms DESC LIMIT 500",
                        args).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data_json"] or "{}")
        out.append({"id": r["id"], "severity": r["severity"], "rule": r["rule"], "message": r["message"],
                    "t": d.get("t"), "p": d.get("p"), "adjusted": d.get("adj"), "returns": d.get("returns", 0), "last_clear_ms": d.get("last_clear_ms"),
                    "start_ms": r["start_ms"], "end_ms": r["end_ms"], "ack_ms": r["ack_ms"], "ack_by": r["ack_by"],
                    "shelved_until_ms": r["shelved_until_ms"], "shelved_by": r["shelved_by"],
                    "shelve_reason": r["shelve_reason"],
                    "site": r["site_key"], "site_name": r["site_name"], "lat": r["lat"], "lon": r["lon"],
                    "sensor": r["sensor_key"], "sensor_name": r["sensor_name"],
                    "system": r["system_key"], "system_name": r["system_name"]})
    return out


def acknowledge(conn, alert_id, actor, note=""):
    r = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if r is None:
        raise ValueError("there is no such alert")
    actor = logchain.clean_actor(actor)
    conn.execute("UPDATE alerts SET ack_ms=?, ack_by=? WHERE id=?", (dbm.now_ms(), actor, alert_id))
    logchain.append(conn, "ack", {"alert": alert_id, "rule": r["rule"], "severity": r["severity"], "note": note[:500]},
                    f"Acknowledged: {r['message']}" + (f" Note: {note[:300]}" if note else ""), actor, commit=False)
    conn.commit()
    return {"id": alert_id, "ack_by": actor}


def shelve(conn, alert_id, actor, hours, reason):
    """Hide an alert for a while, never forever, and always with a reason."""
    r = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if r is None:
        raise ValueError("there is no such alert")
    actor = logchain.clean_actor(actor)
    reason = " ".join(str(reason or "").split())[:300]
    if not reason:
        raise ValueError("say why it's being shelved, so the next shift knows")
    try:
        hours = float(hours)
    except (TypeError, ValueError):
        raise ValueError("say how long to shelve it for, in hours")
    if not 0 < hours <= 168:
        raise ValueError("an alert can be shelved for up to 7 days (168 hours)")
    until = dbm.now_ms() + int(hours * 3600_000)
    conn.execute("UPDATE alerts SET shelved_until_ms=?, shelved_by=?, shelve_reason=? WHERE id=?", (until, actor, reason, alert_id))
    logchain.append(conn, "shelve", {"alert": alert_id, "hours": hours, "reason": reason},
                    f"Shelved for {hours:g} h ({reason}): {r['message']}", actor, commit=False)
    conn.commit()
    return {"id": alert_id, "shelved_until_ms": until}


def unshelve(conn, alert_id, actor):
    r = conn.execute("SELECT * FROM alerts WHERE id=?", (alert_id,)).fetchone()
    if r is None:
        raise ValueError("there is no such alert")
    actor = logchain.clean_actor(actor)
    conn.execute("UPDATE alerts SET shelved_until_ms=NULL, shelved_by=NULL, shelve_reason=NULL WHERE id=?", (alert_id,))
    logchain.append(conn, "unshelve", {"alert": alert_id}, f"Unshelved: {r['message']}", actor, commit=False)
    conn.commit()
    return {"id": alert_id}
