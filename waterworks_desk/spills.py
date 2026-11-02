"""Storm overflow spills: periods, counts the regulator's way, and the dry-day check.

Spill periods come from an on/off monitor's readings (1 discharging, 0 not; a BAD reading is
the monitor offline, not a spill).

Counting follows the 12/24 method used for England's event duration monitoring returns
(Ofwat, Storm overflows PC definition, section 1.2): start counting at the first discharge;
any discharge in the first 12-hour block counts as one spill; any discharge in each following
24-hour block counts as one more; a 24-hour block with no discharge ends the sequence, and the
next discharge starts it again.

The dry-day check uses the Environment Agency's definition of a dry-day spill: no rainfall
above 0.25 mm on that day and the preceding 24 hours. The EA also uses rainfall radar and
investigates drain-down times; this check uses the nearest rain gauges only, so it reports a
*possible* dry-day spill, and says when there's no gauge close enough to tell.
"""
import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .db import BAD

DRY_MM = 0.25
GAUGE_KM = 15          # a gauge further away than this says little about rain at the overflow
GAUGES = 3             # judge by the nearest few, and call it dry only if all of them were dry


def periods(conn, sensor_id, t0, t1):
    """[(start, end), ...] the monitor was discharging between t0 and t1 (clipped), including a
    spill already under way at t0."""
    prev = conn.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? AND ts<? ORDER BY ts DESC LIMIT 1",
                        (sensor_id, t0)).fetchone()
    rows = conn.execute("SELECT ts, value, quality FROM readings WHERE sensor_id=? AND ts>=? AND ts<=? ORDER BY ts",
                        (sensor_id, t0, t1)).fetchall()
    out, start = [], None
    if prev is not None and prev[2] != BAD and prev[1] >= 0.5:
        start = t0
    for ts, v, q in rows:
        on = q != BAD and v >= 0.5
        if on and start is None:
            start = ts
        elif not on and start is not None:
            if ts > start:
                out.append((start, ts))
            start = None
    if start is not None and t1 > start:
        out.append((start, t1))
    return out


def count_12_24(spells):
    """Spill count by the 12/24 method for sorted, non-overlapping (start, end) discharge periods."""
    H, n, i = 3600, 0, 0
    while i < len(spells):
        block_end = spells[i][0] + 12 * H      # a new sequence: the first 12-hour block is one spill
        n += 1
        while True:                            # then each following 24-hour block with a discharge is one more
            nxt_start, nxt_end = block_end, block_end + 24 * H
            while i < len(spells) and spells[i][1] <= nxt_start:
                i += 1                         # discharges that finished before the next block
            if i < len(spells) and spells[i][0] < nxt_end:
                n += 1
                block_end = nxt_end
                continue
            break                              # a 24-hour block with no discharge ends the sequence
    return n


def _km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(a))


def gauges_near(conn, lat, lon, max_km=GAUGE_KM, k=GAUGES):
    """The nearest live rain gauges within max_km: [(sensor_id, name, km, interval_s)]."""
    out = []
    for r in conn.execute("SELECT x.id, s.name, s.lat, s.lon, x.expected_interval_s FROM sensors x JOIN sites s ON s.id=x.site_id "
                          "WHERE x.quantity='rain' AND x.status='live' AND s.status='live'"):
        d = _km(lat, lon, r[2], r[3])
        if d <= max_km:
            out.append((r[0], r[1], d, r[4] or 900))
    return sorted(out, key=lambda g: g[2])[:k]


def rain_total(conn, sensor_id, t0, t1, interval_s):
    """mm between t0 and t1, or None when the gauge reported less than 80 % of the time."""
    n, total = conn.execute("SELECT COUNT(*), COALESCE(SUM(value), 0) FROM readings WHERE sensor_id=? AND ts>? AND ts<=? AND quality!=?",
                            (sensor_id, t0, t1, BAD)).fetchone()
    expected = max(1, (t1 - t0) / interval_s)
    return round(total, 2) if n >= 0.8 * expected else None


def dry_check(conn, lat, lon, spill_start, now, tz="Europe/London"):
    """Was it a dry day when this spill started? Returns {"dry": True/False/None, "mm": max total,
    "gauges": [{"name", "km", "mm"}], "window": [t0, t1]}; dry is None when no gauge nearby has
    enough readings to tell."""
    z = ZoneInfo(tz)
    day = datetime.fromtimestamp(spill_start, z).replace(hour=0, minute=0, second=0, microsecond=0)
    t0 = int((day - timedelta(hours=24)).timestamp())
    t1 = min(int((day + timedelta(days=1)).timestamp()), int(now))
    found = []
    for sid, name, km, every in gauges_near(conn, lat, lon):
        mm = rain_total(conn, sid, t0, t1, every)
        if mm is not None:
            found.append({"name": name, "km": round(km, 1), "mm": mm})
    if not found:
        return {"dry": None, "mm": None, "gauges": [], "window": [t0, t1]}
    worst = max(g["mm"] for g in found)
    return {"dry": worst <= DRY_MM, "mm": worst, "gauges": found, "window": [t0, t1]}


def report(conn, t0, t1, now, system=None, tz="Europe/London", snapshot_sources=()):
    """Per overflow between t0 and t1: spills (12/24), hours discharging, possible dry-day spills,
    last spill. Sorted worst first."""
    q = ("SELECT x.id, x.key sensor, s.key site, s.name, s.lat, s.lon, s.note, y.key system, g.key region FROM sensors x "
         "JOIN sites s ON s.id=x.site_id JOIN systems y ON y.id=s.system_id JOIN regions g ON g.id=y.region_id "
         "WHERE x.quantity='state' AND s.site_type IN ('overflow_weir','outfall') AND x.status!='retired'")
    args = []
    if system and system.startswith("region:"):
        q += " AND y.region_id=(SELECT id FROM regions WHERE key=?)"
        args.append(system[7:])
    elif system:
        q += " AND y.key=?"
        args.append(system)
    rows, first = [], conn.execute("SELECT MIN(r.ts) FROM readings r JOIN sensors x ON x.id=r.sensor_id WHERE x.quantity='state'").fetchone()[0]
    for r in conn.execute(q, args).fetchall():
        sp = periods(conn, r["id"], t0, t1)
        if not sp:
            continue
        dry = unknown = 0
        for a, _ in sp:
            d = dry_check(conn, r["lat"], r["lon"], a, now, tz)
            dry += d["dry"] is True
            unknown += d["dry"] is None
        note = r["note"] or ""
        water = note.split("discharging to ", 1)[1].rstrip(".") if "discharging to " in note else ""
        rows.append({"site": r["site"], "name": r["name"], "water": water, "system": r["system"], "region": r["region"],
                     "spills": count_12_24(sp), "events": len(sp), "hours": round(sum(b - a for a, b in sp) / 3600, 1),
                     "dry": dry, "rain_unknown": unknown, "last_start": sp[-1][0], "last_end": sp[-1][1]})
    rows.sort(key=lambda x: (-x["spills"], -x["hours"]))
    # A snapshot feed (Stream) only tells each overflow's latest spill, so its history is complete
    # from the first time it was read; continuous sources (CSV exports, a historian) from data_from.
    complete = None
    if snapshot_sources:
        ms = conn.execute(f"SELECT MIN(t_ms) FROM imports WHERE ok=1 AND source IN ({','.join('?' * len(snapshot_sources))})",
                          list(snapshot_sources)).fetchone()[0]
        complete = ms // 1000 if ms else None
    return {"from": t0, "to": t1, "data_from": first, "complete_from": complete, "overflows": rows,
            "totals": {"overflows": len(rows), "spills": sum(x["spills"] for x in rows),
                       "hours": round(sum(x["hours"] for x in rows), 1), "dry": sum(x["dry"] for x in rows)}}
