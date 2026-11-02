"""Setting up the network by hand: regions, systems, zones, sites, components and sensors.

Everything a person can create or change goes through `save`, `retire` and `go_live` here, which
the console's setup screens (M1) and the bundle import both call. Each change is checked first,
in plain words; errors stop the change, warnings are shown and allowed. Every change is written
to the signed audit trail with the person's name and what changed.

New things start as drafts: readings are stored for a draft sensor, but nothing alerts on it
until someone presses go live, so a half-finished site never raises alarms.

Heights and depths are typed the way people measure them and stored as metres above the
region's vertical datum (see `level_from`).
"""
import json
import math
import re
import sqlite3
from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import logchain, units
from .db import NETWORK_TYPES, now_ms

KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")

SITE_TYPES = {
    "pumping_station", "booster_station", "treatment_works", "reservoir", "tank", "wet_well", "well",
    "pressure_reducing_valve", "valve_chamber", "district_meter", "pressure_logger", "overflow_weir",
    "outfall", "flood_pumping_station", "storage_pond", "canal_gauge", "tide_gauge", "rain_gauge",
    "screen", "gate", "sampling_point", "other",
}
COMPONENT_TYPES = {
    "centrifugal_pump", "submersible_pump", "axial_flow_pump", "motor", "variable_speed_drive",
    "actuated_valve", "pressure_reducing_valve", "check_valve", "flap_gate", "sluice_gate",
    "overflow_regulator", "overflow_weir", "screen", "flow_meter", "level_sensor", "pressure_sensor",
    "analyser", "uv_lamp_bank", "blower", "generator", "tank", "other",
}
HEIGHT_REFS = {
    "above_datum": "height above the datum",
    "below_ground": "depth below ground",
    "above_ground": "height above ground",
    "above_floor": "height above the floor",
    "below_overflow": "depth below the overflow level",
}
LEVEL_QUANTITIES = {"level", "pressure"}

# kind -> table, parent field (key in data, column, parent table), editable columns
KINDS = {
    "region": ("regions", None, ("name", "timezone", "datum", "language", "lat", "lon", "radius_km", "basemap", "note")),
    "system": ("systems", ("region", "region_id", "regions"), ("name", "network_type", "departments")),
    "zone": ("zones", ("system", "system_id", "systems"), ("name", "kind", "boundary_json")),
    "site": ("sites", ("system", "system_id", "systems"),
             ("name", "site_type", "zone_id", "lat", "lon", "ground_level_m", "floor_level_m", "overflow_level_m",
              "area_curve_json", "criticality", "note")),
    "component_model": ("component_models", None, ("component_type", "maker", "model", "data_json")),
    "layer": ("map_layers", ("system", "system_id", "systems"), ("name", "kind", "geojson")),
    "component": ("components", ("site", "site_id", "sites"),
                  ("name", "component_type", "model_id", "serial", "install_date", "criticality", "note")),
    "sensor": ("sensors", ("site", "site_id", "sites"),
               ("name", "component_id", "quantity", "unit", "display_unit", "source", "source_tag", "source_unit",
                "mount_level_m", "mount_entry_json", "range_lo", "range_hi", "alarm_lo", "warn_lo", "warn_hi",
                "alarm_hi", "expected_interval_s", "note")),
}
HAS_STATUS = {"system", "zone", "site", "component", "sensor"}
CHILDREN = {"system": [("zone", "system_id"), ("site", "system_id")],
            "site": [("component", "site_id"), ("sensor", "site_id")]}
BUNDLE_ORDER = ("region", "system", "layer", "zone", "site", "component_model", "component", "sensor")


class SetupError(ValueError):
    def __init__(self, errors, warnings=()):
        super().__init__("; ".join(errors))
        self.errors, self.warnings = list(errors), list(warnings)


def _actor(actor):
    try:
        return logchain.clean_actor(actor)
    except ValueError as e:
        raise SetupError([str(e)])


def _num(v):
    if v is None or v == "":
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise SetupError([f"{v!r} is not a number"])
    if not math.isfinite(x):
        raise SetupError([f"{v!r} is not a finite number"])
    return x


def parse_coord(v, axis):
    """Decimal degrees, or degrees-minutes(-seconds) like 51°30'26"N / 0 7.6 W."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
    else:
        s = str(v or "").strip().upper()
        if not s:
            raise SetupError([f"{axis} is required"])
        hemi = re.search(r"[NSEW]", s)
        neg = s.startswith("-") or (hemi and hemi.group() in "SW")
        nums = [float(n) for n in re.findall(r"\d+(?:[.,]\d+)?", s.replace(",", "."))]
        if not nums or len(nums) > 3:
            raise SetupError([f"{axis} {v!r} isn't a coordinate; use decimal degrees like 51.5072"])
        x = nums[0] + (nums[1] / 60 if len(nums) > 1 else 0) + (nums[2] / 3600 if len(nums) > 2 else 0)
        x = -x if neg else x
        if hemi and ((axis == "latitude") != (hemi.group() in "NS")):
            raise SetupError([f"{v!r} looks like a {'longitude' if axis == 'latitude' else 'latitude'}"])
    lim = 90 if axis == "latitude" else 180
    if not math.isfinite(x) or abs(x) > lim:
        raise SetupError([f"{axis} must be between -{lim} and {lim} degrees"])
    return round(x, 7)


def distance_m(lat1, lon1, lat2, lon2):
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def level_from(entry, site=None, what="height"):
    """Turn a height as typed into metres above datum.

    entry: a number (already above datum) or {"ref": one of HEIGHT_REFS, "value": metres}.
    Returns (level_m, entry_as_stored). Depths are positive numbers measured downwards.
    """
    if entry is None or entry == "":
        return None, None
    if not isinstance(entry, dict):
        entry = {"ref": "above_datum", "value": entry}
    ref = entry.get("ref", "above_datum")
    if ref not in HEIGHT_REFS:
        raise SetupError([f"{what}: say what it's measured from ({', '.join(HEIGHT_REFS.values())})"])
    v = _num(entry.get("value"))
    if v is None:
        raise SetupError([f"{what}: a value in metres is required"])
    stored = {"ref": ref, "value": v}
    if ref == "above_datum":
        return v, stored
    if ref.startswith("below") and v < 0:
        raise SetupError([f"{what}: a {HEIGHT_REFS[ref]} can't be negative. Did you mean "
                          f"{'above ground' if ref == 'below_ground' else 'above the overflow'}?"])
    base_col = {"below_ground": "ground_level_m", "above_ground": "ground_level_m",
                "above_floor": "floor_level_m", "below_overflow": "overflow_level_m"}[ref]
    base = site[base_col] if site is not None and base_col in site.keys() else None
    if base is None:
        raise SetupError([f"{what}: measured from the {base_col.replace('_level_m', '')}, but the site has no "
                          f"{base_col.replace('_m', '').replace('_', ' ')} yet. Add it to the site first."])
    return (base - v if ref.startswith("below") else base + v), stored


def _row(conn, table, key):
    return conn.execute(f"SELECT * FROM {table} WHERE key=?", (key,)).fetchone()


def _id(conn, table, key, label):
    if key in (None, ""):
        return None
    r = _row(conn, table, key)
    if r is None:
        raise SetupError([f"there is no {label} {key!r}"])
    return r["id"]


def _clean(conn, kind, data, existing, known_sources):
    """Validate and normalise one object. Returns (columns, errors, warnings)."""
    errors, warnings = [], []
    table, parent, editable = KINDS[kind]
    cols = {}

    def take(name, fn=lambda x: x):
        if name in data:
            try:
                cols[name] = fn(data[name])
            except SetupError as e:
                errors.extend(e.errors)

    def text(x, n=200):
        return " ".join(str(x or "").split())[:n]

    take("name", text)
    if existing is None and not cols.get("name") and kind != "component_model":
        errors.append(f"a {kind.replace('_', ' ')} needs a name")
    take("note", lambda x: str(x or "")[:2000])

    def cur(col):
        return cols[col] if col in cols else (existing[col] if existing is not None else None)

    if kind == "region":
        take("timezone", text)
        if cur("timezone"):
            try:
                ZoneInfo(cur("timezone"))
            except (ZoneInfoNotFoundError, ValueError):
                errors.append(f"{cur('timezone')!r} isn't a time zone; use a name like Europe/London or Asia/Singapore")
        take("datum", text)
        take("language", lambda x: text(x, 10))
        take("lat", lambda x: parse_coord(x, "latitude"))
        take("lon", lambda x: parse_coord(x, "longitude"))
        take("radius_km", _num)
        take("basemap", lambda x: 0 if str(x).strip().lower() in ("0", "false", "no", "off", "none") else 1)
        if cur("radius_km") is not None and cur("radius_km") <= 0:
            errors.append("the region's radius must be more than 0 km")
        if not cur("datum"):
            warnings.append("no vertical datum named: heights will be stored, but nobody will know what they're above")
    elif kind == "system":
        take("network_type", text)
        if cur("network_type") not in NETWORK_TYPES:
            errors.append(f"network type must be one of: {', '.join(t.replace('_', ' ') for t in NETWORK_TYPES)}")
        take("departments", lambda x: ",".join(text(d, 40) for d in (x if isinstance(x, list) else str(x).split(",")) if text(d, 40)))
    elif kind == "zone":
        take("kind", text)
        take("boundary_json", lambda x: json.dumps(x) if isinstance(x, (dict, list)) else (x or None))
    elif kind == "site":
        take("site_type", text)
        if existing is None and not cur("site_type"):
            errors.append("say what kind of site it is (pumping station, tank, overflow weir, rain gauge…)")
        elif cur("site_type") and cur("site_type") not in SITE_TYPES:
            warnings.append(f"{cur('site_type')!r} isn't a site type the console knows; it will be shown as entered")
        take("lat", lambda x: parse_coord(x, "latitude"))
        take("lon", lambda x: parse_coord(x, "longitude"))
        if cur("lat") is None or cur("lon") is None:
            errors.append("a site needs its GPS position (latitude and longitude)")
        take("criticality", lambda x: int(_num(x)))
        if cur("criticality") is not None and cur("criticality") not in (1, 2, 3):
            errors.append("criticality is 1 (highest), 2 or 3")
        # heights: ground first (others may be measured from it)
        if "ground_level_m" in data:
            try:
                cols["ground_level_m"], _ = level_from(data["ground_level_m"], None, "ground level")
            except SetupError as e:
                errors.extend(e.errors)
        probe = dict(existing) if existing is not None else {}
        probe.update({k: v for k, v in cols.items()})
        probe.setdefault("ground_level_m", None)
        probe.setdefault("floor_level_m", None)
        probe.setdefault("overflow_level_m", None)

        class P(dict):
            def keys(self):
                return super().keys()
        for col, label in (("floor_level_m", "floor level"), ("overflow_level_m", "overflow level")):
            if col in data:
                try:
                    cols[col], _ = level_from(data[col], P(probe), label)
                    probe[col] = cols[col]
                except SetupError as e:
                    errors.extend(e.errors)
        g, f, o = cur("ground_level_m"), cur("floor_level_m"), cur("overflow_level_m")
        if f is not None and o is not None and o <= f:
            errors.append(f"the overflow level ({o:.2f} m) must be above the floor ({f:.2f} m)")
        if g is not None and f is not None and f > g + 0.01 and cur("site_type") in ("wet_well", "well"):
            warnings.append(f"the floor ({f:.2f} m) is above ground level ({g:.2f} m) for a {cur('site_type').replace('_', ' ')}; check the depth")
        if "area_curve_json" in data:
            ac = data["area_curve_json"]
            try:
                ac = json.loads(ac) if isinstance(ac, str) and ac.strip() else ac
                if ac in (None, "", []):
                    cols["area_curve_json"] = None
                else:
                    pts = [(float(d), float(a)) for d, a in ac]
                    if any(a <= 0 for _, a in pts) or any(d < 0 for d, _ in pts):
                        errors.append("the area curve needs depths of 0 or more and areas above 0")
                    elif any(pts[i + 1][0] <= pts[i][0] for i in range(len(pts) - 1)):
                        errors.append("the area curve's depths must go up in order")
                    cols["area_curve_json"] = json.dumps(pts)
            except (TypeError, ValueError):
                errors.append("the area curve should be pairs of [depth in m, area in m²]")
    elif kind == "layer":
        take("kind", text)
        if "geojson" in data:
            g = data["geojson"]
            try:
                g = json.loads(g) if isinstance(g, str) else g
                if not isinstance(g, dict) or g.get("type") not in ("FeatureCollection", "Feature"):
                    raise ValueError
                feats = g.get("features") if g.get("type") == "FeatureCollection" else [g]
                if not isinstance(feats, list) or not feats:
                    raise ValueError
                for f in feats:
                    if not isinstance(f, dict):
                        raise ValueError
                    for x, y, *_ in _coords(f.get("geometry")):
                        if not (-180 <= x <= 180 and -90 <= y <= 90):
                            raise SetupError(["the map layer has a point outside the world (is it in lat/lon, WGS84?)"])
                cols["geojson"] = json.dumps(g, separators=(",", ":"))
            except SetupError as e:
                errors.extend(e.errors)
            except (TypeError, ValueError):
                errors.append("a map layer must be GeoJSON (a FeatureCollection)")
        elif existing is None:
            errors.append("a map layer needs its GeoJSON")
    elif kind == "component_model":
        take("component_type", text)
        take("maker", text)
        take("model", text)
        take("data_json", lambda x: json.dumps(x) if isinstance(x, (dict, list)) else (x or "{}"))
    elif kind == "component":
        take("component_type", text)
        if existing is None and not cur("component_type"):
            errors.append("say what kind of component it is (pump, valve, meter…)")
        elif cur("component_type") and cur("component_type") not in COMPONENT_TYPES:
            warnings.append(f"{cur('component_type')!r} isn't a component type the catalogue knows yet")
        take("serial", text)
        take("criticality", lambda x: None if _num(x) is None else int(_num(x)))
        if cur("criticality") is not None and cur("criticality") not in (1, 2, 3):
            errors.append("criticality is 1 (highest), 2 or 3")
        if data.get("install_date"):
            try:
                cols["install_date"] = date.fromisoformat(str(data["install_date"])).isoformat()
                if cols["install_date"] > date.today().isoformat():
                    errors.append("the install date is in the future")
            except ValueError:
                errors.append("install date should look like 2019-06-30")
        if "model" in data:
            try:
                cols["model_id"] = _id(conn, "component_models", data["model"], "component model")
            except SetupError as e:
                errors.extend(e.errors)
    elif kind == "sensor":
        take("quantity", text)
        q = cur("quantity")
        if q not in units.QUANTITIES:
            errors.append(f"say what the sensor measures; one of: {', '.join(sorted(units.QUANTITIES))}")
            q = None
        if q:
            cols["unit"] = units.canonical(q)
        for col, label in (("source_unit", "the unit the source sends"), ("display_unit", "the display unit")):
            take(col, text)
            if q and cur(col) and not units.known(q, cur(col)):
                errors.append(f"{label} {cur(col)!r} isn't a unit of {q}")
        take("source", text)
        take("source_tag", lambda x: text(x, 200) or None)
        src, tag = cur("source"), cur("source_tag")
        if src and src != "manual" and known_sources is not None and src not in known_sources:
            errors.append(f"there is no data source {src!r} in config.toml (known: {', '.join(sorted(known_sources)) or 'none'})")
        if src and src != "manual" and not tag:
            errors.append("say which tag, column or register carries this sensor's readings")
        if src == "manual" and tag:
            cols["source_tag"] = None
        take("expected_interval_s", lambda x: int(_num(x)) if _num(x) is not None else None)
        # limits and range: typed in the display unit (or canonical), stored canonical
        lim_unit = data.get("limits_unit") or cur("display_unit") or (q and units.canonical(q))
        for col in ("range_lo", "range_hi", "alarm_lo", "warn_lo", "warn_hi", "alarm_hi"):
            if col in data:
                try:
                    v = _num(data[col])
                    cols[col] = None if v is None or not q else units.to_canonical(q, lim_unit, v)
                except (SetupError, ValueError) as e:
                    errors.extend(getattr(e, "errors", [str(e)]))
        seq = [(c, cur(c)) for c in ("alarm_lo", "warn_lo", "warn_hi", "alarm_hi") if cur(c) is not None]
        for (c1, v1), (c2, v2) in zip(seq, seq[1:]):
            if v1 > v2 or (v1 == v2 and (c1, c2) == ("warn_lo", "warn_hi")):
                errors.append(f"the {c1.replace('_', ' ')} limit must be below the {c2.replace('_', ' ')} limit")
        lo, hi = cur("range_lo"), cur("range_hi")
        if lo is not None and hi is not None and lo >= hi:
            errors.append("the sensor's range must have its low end below its high end")
        for c, v in seq:
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                errors.append(f"the {c.replace('_', ' ')} limit is outside what the sensor can measure")
        if "component" in data:
            try:
                cols["component_id"] = _id(conn, "components", data["component"], "component")
            except SetupError as e:
                errors.extend(e.errors)
    return cols, errors, warnings


def _coords(geom):
    """Every [lon, lat] in a GeoJSON geometry; raises ValueError on anything that isn't one."""
    if not isinstance(geom, dict) or geom.get("type") not in (
            "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"):
        raise ValueError("unknown geometry")
    def walk(v):
        if isinstance(v, (list, tuple)) and v and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            if len(v) < 2:
                raise ValueError("a position needs longitude and latitude")
            yield v
        elif isinstance(v, (list, tuple)) and v:
            for x in v:
                yield from walk(x)
        else:
            raise ValueError("not a coordinate")
    return list(walk(geom.get("coordinates")))


def save(conn, kind, data, actor, known_sources=None, commit=True, audit=True):
    """Create or update one object by its key. Returns {"key", "created", "warnings"}; raises
    SetupError (with every problem found) when it can't be saved."""
    if kind not in KINDS:
        raise SetupError([f"unknown kind {kind!r}"])
    actor = _actor(actor)
    table, parent, editable = KINDS[kind]
    key = str(data.get("key") or "").strip()
    if not KEY_RE.match(key):
        raise SetupError(["every item needs a short key: letters, digits and - _ . : (e.g. thanh-da-ps)"])
    existing = _row(conn, table, key)
    if existing is not None and kind in HAS_STATUS and existing["status"] == "retired":
        raise SetupError([f"{key!r} was retired; bring it back first or use a new key"])
    cols, errors, warnings = _clean(conn, kind, data, existing, known_sources)
    # parent
    if parent:
        pkey, pcol, ptable = parent
        if pkey in data or existing is None:
            try:
                pid = _id(conn, ptable, data.get(pkey), pkey)
                if pid is None:
                    errors.append(f"say which {pkey} it belongs to")
                cols[pcol] = pid
            except SetupError as e:
                errors.extend(e.errors)
    site_row = None
    if kind in ("site", "component", "sensor"):
        sid = cols.get("site_id", existing["site_id"] if existing is not None and kind != "site" else None)
        if kind == "site":
            sysid = cols.get("system_id", existing["system_id"] if existing is not None else None)
            if "zone" in data:
                try:
                    cols["zone_id"] = _id(conn, "zones", data["zone"], "zone")
                except SetupError as e:
                    errors.extend(e.errors)
            zid = cols.get("zone_id", existing["zone_id"] if existing is not None else None)
            if zid and sysid and conn.execute("SELECT system_id FROM zones WHERE id=?", (zid,)).fetchone()[0] != sysid:
                errors.append("the zone belongs to a different system")
            lat = cols.get("lat", existing["lat"] if existing is not None else None)
            lon = cols.get("lon", existing["lon"] if existing is not None else None)
            if sysid and lat is not None and lon is not None:
                reg = conn.execute("SELECT r.* FROM regions r JOIN systems s ON s.region_id=r.id WHERE s.id=?",
                                   (sysid,)).fetchone()
                if reg and reg["radius_km"] and reg["lat"] is not None:
                    d = distance_m(lat, lon, reg["lat"], reg["lon"]) / 1000
                    if d > reg["radius_km"]:
                        errors.append(f"that position is {d:.0f} km from the centre of {reg['name']}, outside its "
                                      f"{reg['radius_km']:.0f} km area. Check the coordinates (latitude and longitude swapped?)")
                for o in conn.execute("SELECT key, name, lat, lon FROM sites WHERE system_id=? AND status!='retired' AND key!=?",
                                      (sysid, key)):
                    if distance_m(lat, lon, o["lat"], o["lon"]) < 2:
                        warnings.append(f"this is within 2 m of {o['name']} ({o['key']}); is it the same place?")
                        break
        elif sid:
            site_row = conn.execute("SELECT * FROM sites WHERE id=?", (sid,)).fetchone()
            cid = cols.get("component_id", existing["component_id"] if existing is not None and kind == "sensor" else None)
            if kind == "sensor" and cid and conn.execute("SELECT site_id FROM components WHERE id=?", (cid,)).fetchone()[0] != sid:
                errors.append("that component is at a different site")
    if kind == "sensor" and "mount_level_m" in data and not errors:
        try:
            lvl, stored = level_from(data["mount_level_m"], site_row, "mounting height")
            cols["mount_level_m"], cols["mount_entry_json"] = lvl, json.dumps(stored) if stored else None
            q = cols.get("quantity", existing["quantity"] if existing is not None else None)
            if (lvl is not None and q == "level" and site_row is not None and site_row["overflow_level_m"] is not None
                    and lvl > site_row["overflow_level_m"]):
                warnings.append("the sensor's zero point is above the site's overflow level; check the mounting height")
        except SetupError as e:
            errors.extend(e.errors)
    if errors:
        raise SetupError(errors, warnings)
    t = now_ms()
    before = {k: existing[k] for k in cols if existing is not None and k in existing.keys()}
    changed = {k: v for k, v in cols.items() if existing is None or existing[k] != v}
    try:
        if existing is None:
            extra = {"created_ms": t}
            if kind in HAS_STATUS:
                extra.update(updated_ms=t, status="draft")
            if kind == "region":
                cols.setdefault("timezone", "UTC")
            allc = {"key": key, **cols, **extra}
            conn.execute(f"INSERT INTO {table} ({','.join(allc)}) VALUES ({','.join('?' * len(allc))})", list(allc.values()))
        elif changed:
            if kind in HAS_STATUS:
                changed["updated_ms"] = t
            conn.execute(f"UPDATE {table} SET {','.join(c + '=?' for c in changed)} WHERE key=?", [*changed.values(), key])
    except sqlite3.IntegrityError as e:
        conn.rollback()
        msg = str(e)
        if "source" in msg and "tag" in msg or "sensors.source" in msg:
            raise SetupError([f"another sensor already reads tag {cols.get('source_tag')!r} from {cols.get('source')!r}"], warnings)
        raise SetupError([f"couldn't save: {msg}"], warnings)
    if audit and (existing is None or changed):
        name = cols.get("name") or (existing["name"] if existing is not None and "name" in existing.keys() else key)
        verb = "Created" if existing is None else "Changed"
        logchain.append(conn, "setup", {"kind": kind, "key": key, "action": "create" if existing is None else "update",
                                        "before": before if existing is not None else None,
                                        "after": {k: v for k, v in changed.items() if k != "updated_ms"}},
                        f"{verb} {kind.replace('_', ' ')} {name} ({key}).", actor, commit=False)
    if commit:
        conn.commit()
    return {"key": key, "created": existing is None, "warnings": warnings}


def retire(conn, kind, key, actor, reason=""):
    """Take something out of service. It and its history stay; it stops alerting and leaves the map."""
    actor = _actor(actor)
    if kind not in HAS_STATUS:
        raise SetupError([f"a {kind} can't be retired"])
    table = KINDS[kind][0]
    r = _row(conn, table, key)
    if r is None:
        raise SetupError([f"there is no {kind} {key!r}"])
    ids = _descendants(conn, kind, r["id"])
    for k, i in ids:
        conn.execute(f"UPDATE {KINDS[k][0]} SET status='retired', updated_ms=? WHERE id=?", (now_ms(), i))
    logchain.append(conn, "setup", {"kind": kind, "key": key, "action": "retire", "reason": reason,
                                    "with": len(ids) - 1},
                    f"Retired {kind} {r['name']} ({key})" + (f": {reason}" if reason else "") + ".", actor, commit=False)
    conn.commit()
    return {"key": key, "retired": len(ids)}


def _descendants(conn, kind, oid):
    out = [(kind, oid)]
    for ck, col in CHILDREN.get(kind, []):
        for (cid,) in conn.execute(f"SELECT id FROM {KINDS[ck][0]} WHERE {col}=? AND status!='retired'", (oid,)):
            out.extend(_descendants(conn, ck, cid))
    return out


def problems(conn, kind, oid, known_sources=None):
    """Checks that must pass before something goes live (beyond the checks on every save)."""
    table = KINDS[kind][0]
    r = conn.execute(f"SELECT * FROM {table} WHERE id=?", (oid,)).fetchone()
    errs, warns = [], []
    label = f"{r['name']} ({r['key']})"
    if kind == "sensor":
        if not r["source"]:
            errs.append(f"{label}: say where its readings come from (a data source, or manual entry)")
        elif r["source"] != "manual" and known_sources is not None and r["source"] not in known_sources:
            errs.append(f"{label}: data source {r['source']!r} isn't in config.toml")
        if r["quantity"] in LEVEL_QUANTITIES and r["mount_level_m"] is None:
            warns.append(f"{label}: no mounting height, so its readings can't be turned into water levels")
        if all(r[c] is None for c in ("alarm_lo", "warn_lo", "warn_hi", "alarm_hi")):
            warns.append(f"{label}: no limits set; it will only alert on its learned normal (from M2)")
    if kind == "site":
        if r["ground_level_m"] is None:
            warns.append(f"{label}: no ground level, so depths can't be measured from it")
        if r["site_type"] in ("wet_well", "tank", "reservoir", "storage_pond") and (r["floor_level_m"] is None or r["overflow_level_m"] is None):
            warns.append(f"{label}: add floor and overflow levels so levels can be shown against them")
    return errs, warns


def go_live(conn, kind, key, actor, known_sources=None):
    """Make a draft (and its draft children) live. Nothing changes if any of them has a problem."""
    actor = _actor(actor)
    if kind not in HAS_STATUS:
        raise SetupError([f"a {kind} doesn't go live"])
    table = KINDS[kind][0]
    r = _row(conn, table, key)
    if r is None:
        raise SetupError([f"there is no {kind} {key!r}"])
    if r["status"] == "retired":
        raise SetupError([f"{key!r} is retired"])
    items = _descendants(conn, kind, r["id"])
    # the parents must be live (or going live) too
    if kind in ("site", "zone"):
        if conn.execute("SELECT status FROM systems WHERE id=?", (r["system_id"],)).fetchone()[0] != "live":
            raise SetupError(["its system is still a draft; put the system live first"])
    if kind in ("component", "sensor"):
        if conn.execute("SELECT status FROM sites WHERE id=?", (r["site_id"],)).fetchone()[0] != "live":
            raise SetupError(["its site is still a draft; put the site live first"])
    errs, warns = [], []
    for k, i in items:
        e, w = problems(conn, k, i, known_sources)
        errs += e
        warns += w
    if errs:
        raise SetupError(errs, warns)
    n = 0
    for k, i in items:
        n += conn.execute(f"UPDATE {KINDS[k][0]} SET status='live', updated_ms=? WHERE id=? AND status='draft'",
                          (now_ms(), i)).rowcount
    keys = {(k, conn.execute(f"SELECT key FROM {KINDS[k][0]} WHERE id=?", (i,)).fetchone()[0]) for k, i in items}
    for p in conn.execute("SELECT kind, key, data_json FROM pending_changes").fetchall():
        if (p["kind"], p["key"]) in keys:
            save(conn, p["kind"], json.loads(p["data_json"]), actor, known_sources, commit=False)
            conn.execute("DELETE FROM pending_changes WHERE kind=? AND key=?", (p["kind"], p["key"]))
            n += 1
    logchain.append(conn, "go_live", {"kind": kind, "key": key, "items": n},
                    f"Put {kind} {r['name']} ({key}) live" + (f" with {n - 1} item(s) under it" if n > 1 else "") + ".",
                    actor, commit=False)
    conn.commit()
    return {"key": key, "live": n, "warnings": warns}


# ---------- bundles: bulk setup files (import, export, copy between installs) ----------

def import_bundle(conn, bundle, actor, known_sources=None, make_live=False, source_name="bundle"):
    """Create or update everything in a bundle in one transaction: either all of it is saved or
    none of it is. Problems are reported per item. One audit entry records the import."""
    actor = _actor(actor)
    if not isinstance(bundle, dict):
        raise SetupError(["a setup file should be a JSON object with lists like \"sites\": [...]"])
    plural = {k: k + "s" for k in BUNDLE_ORDER}
    errors, warnings, counts = [], [], {}
    for kind in BUNDLE_ORDER:
        items = bundle.get(plural[kind], []) or []
        if not isinstance(items, list):
            errors.append(f"\"{plural[kind]}\" should be a list of items")
            continue
        for item in items:
            if not isinstance(item, dict):
                errors.append(f"every item under \"{plural[kind]}\" should be an object with a key, not {json.dumps(item)[:40]}")
                continue
            try:
                res = save(conn, kind, item, actor, known_sources, commit=False, audit=False)
                counts[kind] = counts.get(kind, 0) + 1
                warnings += [f"{kind} {item.get('key')}: {w}" for w in res["warnings"]]
            except SetupError as e:
                errors += [f"{kind} {item.get('key')}: {m}" for m in e.errors]
    if errors:
        conn.rollback()
        raise SetupError(errors, warnings)
    import hashlib
    digest = hashlib.sha256(json.dumps(bundle, sort_keys=True, default=str).encode()).hexdigest()
    logchain.append(conn, "setup", {"action": "import", "source": source_name, "counts": counts, "sha256": digest},
                    f"Imported setup from {source_name}: " + ", ".join(f"{n} {k.replace('_', ' ')}(s)" for k, n in counts.items()) + ".",
                    actor, commit=False)
    conn.commit()
    live = []
    if make_live:
        for s in bundle.get("systems", []) or []:
            live.append(go_live(conn, "system", s["key"], actor, known_sources))
    return {"counts": counts, "warnings": warnings, "live": live}


def export_bundle(conn, include_retired=False, only=None):
    """Everything set up by hand, as a bundle that import_bundle can read back."""
    out = {}
    refs = {"system": ("region", "regions", "region_id"), "zone": ("system", "systems", "system_id"),
            "layer": ("system", "systems", "system_id"),
            "site": ("system", "systems", "system_id"), "component": ("site", "sites", "site_id"),
            "sensor": ("site", "sites", "site_id")}
    for kind in BUNDLE_ORDER:
        table, _, editable = KINDS[kind]
        if only and only[0] != kind:
            continue
        where = "" if include_retired or kind not in HAS_STATUS else " WHERE status!='retired'"
        args = ()
        if only:
            where, args = " WHERE id=?", (only[1],)
        items = []
        for r in conn.execute(f"SELECT * FROM {table}{where} ORDER BY id", args):
            d = {"key": r["key"]}
            if kind in refs:
                pk, pt, pc = refs[kind]
                d[pk] = conn.execute(f"SELECT key FROM {pt} WHERE id=?", (r[pc],)).fetchone()[0]
            for c in editable:
                v = r[c]
                if v is None or c in ("unit", "mount_entry_json"):
                    continue
                if c == "zone_id":
                    d["zone"] = conn.execute("SELECT key FROM zones WHERE id=?", (v,)).fetchone()[0]
                elif c == "component_id":
                    d["component"] = conn.execute("SELECT key FROM components WHERE id=?", (v,)).fetchone()[0]
                elif c == "model_id":
                    d["model"] = conn.execute("SELECT key FROM component_models WHERE id=?", (v,)).fetchone()[0]
                elif c in ("area_curve_json", "boundary_json", "data_json", "geojson"):
                    d[c] = json.loads(v)
                else:
                    d[c] = v
            if kind == "sensor":
                d["limits_unit"] = r["unit"]
                if r["mount_entry_json"]:
                    d["mount_level_m"] = json.loads(r["mount_entry_json"])
            if kind in HAS_STATUS:
                d["_status"] = r["status"]
            items.append(d)
        if items:
            out[kind + "s"] = items
    return out


# ---------- changes to live items wait until someone applies them ----------

def is_live(conn, kind, key):
    if kind not in HAS_STATUS:
        return False
    r = _row(conn, KINDS[kind][0], key)
    return r is not None and r["status"] == "live"


def propose(conn, kind, data, actor, known_sources=None):
    """Check a change to a live item and park it until it's applied. Nothing about the live item
    changes yet. A second proposal for the same item replaces the first."""
    actor = _actor(actor)
    key = str(data.get("key") or "").strip()
    if not is_live(conn, kind, key):
        raise SetupError([f"{key!r} isn't live; save it directly"])
    conn.commit()
    conn.execute("SAVEPOINT dry_run")
    try:
        res = save(conn, kind, data, actor, known_sources, commit=False, audit=False)
    finally:
        conn.execute("ROLLBACK TO dry_run")
        conn.execute("RELEASE dry_run")
    conn.execute("INSERT INTO pending_changes(kind,key,data_json,actor,t_ms) VALUES(?,?,?,?,?) "
                 "ON CONFLICT(kind,key) DO UPDATE SET data_json=excluded.data_json, actor=excluded.actor, t_ms=excluded.t_ms",
                 (kind, key, json.dumps(data, default=str), actor, now_ms()))
    logchain.append(conn, "setup", {"kind": kind, "key": key, "action": "propose", "change": data},
                    f"Proposed a change to live {kind} {key}; it waits until someone applies it.", actor, commit=False)
    conn.commit()
    return {"key": key, "pending": True, "warnings": res["warnings"]}


def pending(conn):
    out = []
    for r in conn.execute("SELECT * FROM pending_changes ORDER BY t_ms"):
        table = KINDS[r["kind"]][0]
        cur = _row(conn, table, r["key"])
        data = json.loads(r["data_json"])
        out.append({"kind": r["kind"], "key": r["key"], "by": r["actor"], "t_ms": r["t_ms"], "change": data,
                    "name": cur["name"] if cur is not None else r["key"]})
    return out


def apply_pending(conn, kind, key, actor, known_sources=None):
    actor = _actor(actor)
    r = conn.execute("SELECT * FROM pending_changes WHERE kind=? AND key=?", (kind, key)).fetchone()
    if r is None:
        raise SetupError([f"there is no pending change to {kind} {key}"])
    res = save(conn, kind, json.loads(r["data_json"]), actor, known_sources, commit=False)
    conn.execute("DELETE FROM pending_changes WHERE kind=? AND key=?", (kind, key))
    logchain.append(conn, "go_live", {"kind": kind, "key": key, "proposed_by": r["actor"]},
                    f"Applied the change to {kind} {key} proposed by {r['actor']}.", actor, commit=False)
    conn.commit()
    return {**res, "applied": True}


def discard_pending(conn, kind, key, actor):
    actor = _actor(actor)
    n = conn.execute("DELETE FROM pending_changes WHERE kind=? AND key=?", (kind, key)).rowcount
    if not n:
        raise SetupError([f"there is no pending change to {kind} {key}"])
    logchain.append(conn, "setup", {"kind": kind, "key": key, "action": "discard"},
                    f"Discarded the pending change to {kind} {key}.", actor, commit=False)
    conn.commit()
    return {"key": key, "discarded": True}


def item(conn, kind, key):
    """One item in setup-file form (limits in its display unit), for the edit forms."""
    table = KINDS[kind][0]
    r = _row(conn, table, key)
    if r is None:
        return None
    b = export_bundle(conn, include_retired=True, only=(kind, r["id"]))
    d = b[kind + "s"][0]
    if kind == "sensor" and r["display_unit"]:
        for c in ("range_lo", "range_hi", "alarm_lo", "warn_lo", "warn_hi", "alarm_hi"):
            if d.get(c) is not None:
                d[c] = round(units.from_canonical(r["quantity"], r["display_unit"], d[c]), 6)
        d["limits_unit"] = r["display_unit"]
    p = conn.execute("SELECT data_json, actor FROM pending_changes WHERE kind=? AND key=?", (kind, key)).fetchone()
    if p:
        d["_pending"] = {"change": json.loads(p["data_json"]), "by": p["actor"]}
    return d
