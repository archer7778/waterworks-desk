"""Build a setup bundle for the BattLeDIM L-Town demo from its EPANET model.

    python tools/ltown_setup.py path/to/L-TOWN.inp > examples/ltown-setup.json
    python -m waterworks_desk.ingest --setup examples/ltown-setup.json --actor "Your name" --live

L-Town's model gives positions in local metres and no real location, so the bundle places it
around a stand-in origin and says so in the region's name and note. Heights are the model's
elevations (metres above the model's own datum). Sensor tags match the BattLeDIM SCADA CSV
column names, so the files import without a tag map.

Data: BattLeDIM (Vrachimis et al., 2020), Zenodo DOI 10.5281/zenodo.4017659, CC BY 4.0.
"""
import argparse
import json
import math
import sys

PRESSURE_SENSORS = ("n1 n4 n31 n54 n105 n114 n163 n188 n215 n229 n288 n296 n332 n342 n410 n415 n429 n458 n469 "
                    "n495 n506 n516 n519 n549 n613 n636 n644 n679 n722 n726 n740 n752 n769").split()
FLOW_SENSORS = {"p227": "District inlet meter (p227)", "p235": "District inlet meter (p235)"}


def parse_inp(path):
    secs, cur = {}, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.split(";")[0].strip()
            if not s:
                continue
            if s.startswith("["):
                cur = s.strip("[]").upper()
                secs.setdefault(cur, [])
                continue
            if cur:
                secs[cur].append(s.split())
    return secs


def build(inp, origin_lat, origin_lon, source="ltown"):
    secs = parse_inp(inp)
    xy = {r[0]: (float(r[1]), float(r[2])) for r in secs["COORDINATES"]}
    elev = {r[0]: float(r[1]) for r in secs["JUNCTIONS"]}
    links = {r[0]: (r[1], r[2]) for r in secs["PIPES"]}
    links.update({r[0]: (r[1], r[2]) for r in secs.get("PUMPS", [])})
    links.update({r[0]: (r[1], r[2]) for r in secs.get("VALVES", [])})
    x0 = min(x for x, _ in xy.values())
    y0 = min(y for _, y in xy.values())
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(origin_lat))

    def ll(node_or_xy):
        x, y = xy[node_or_xy] if isinstance(node_or_xy, str) else node_or_xy
        return round(origin_lat + (y - y0) / m_per_deg_lat, 7), round(origin_lon + (x - x0) / m_per_deg_lon, 7)

    def mid(link):
        a, b = links[link]
        return ll(((xy[a][0] + xy[b][0]) / 2, (xy[a][1] + xy[b][1]) / 2))

    region = {"key": "ltown", "name": "L-Town (demo, stand-in location)", "timezone": "UTC",
              "datum": "L-Town model datum (EPANET elevations)", "lat": None, "lon": None, "radius_km": 25,
              "note": "BattLeDIM's L-Town placed at a stand-in location; it is not where the network is. "
                      "Data: BattLeDIM, Zenodo 10.5281/zenodo.4017659, CC BY 4.0."}
    c = ll(((min(x for x, _ in xy.values()) + max(x for x, _ in xy.values())) / 2,
            (min(y for _, y in xy.values()) + max(y for _, y in xy.values())) / 2))
    region["lat"], region["lon"] = c
    system = {"key": "ltown-dw", "region": "ltown", "name": "L-Town drinking water", "network_type": "drinking_water",
              "departments": ["operations", "maintenance", "instruments"]}
    sites, comps, sensors = [], [], []
    for r in secs["RESERVOIRS"]:
        lat, lon = ll(r[0])
        sites.append({"key": f"ltown-{r[0].lower()}", "system": "ltown-dw", "name": f"Reservoir {r[0]}",
                      "site_type": "reservoir", "lat": lat, "lon": lon, "ground_level_m": float(r[1]), "criticality": 1,
                      "note": f"Fixed head {float(r[1]):.2f} m in the model."})
    for r in secs["TANKS"]:
        tid, e, init, lo, hi, diam = r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])
        area = round(math.pi * diam ** 2 / 4, 2)
        lat, lon = ll(tid)
        sites.append({"key": f"ltown-{tid.lower()}", "system": "ltown-dw", "name": f"Tank {tid}", "site_type": "tank",
                      "lat": lat, "lon": lon, "ground_level_m": e, "floor_level_m": e,
                      "overflow_level_m": {"ref": "above_floor", "value": hi},
                      "area_curve_json": [[0, area], [hi, area]], "criticality": 1})
        sensors.append({"key": f"ltown-{tid.lower()}-level", "site": f"ltown-{tid.lower()}", "name": "Water level",
                        "quantity": "level", "source": source, "source_tag": tid, "source_unit": "m",
                        "mount_level_m": {"ref": "above_floor", "value": 0}, "range_lo": 0, "range_hi": hi,
                        "warn_lo": max(lo, 0.5), "expected_interval_s": 300})
    for r in secs.get("PUMPS", []):
        pid = r[0]
        lat, lon = mid(pid)
        sk = f"ltown-{pid.lower().replace('_', '')}-station"
        sites.append({"key": sk, "system": "ltown-dw", "name": f"Pumping station ({pid})", "site_type": "pumping_station",
                      "lat": lat, "lon": lon, "ground_level_m": elev.get(r[1]), "criticality": 1})
        comps.append({"key": f"{sk}-pump", "site": sk, "name": "Pump 1", "component_type": "centrifugal_pump"})
        sensors.append({"key": f"{sk}-flow", "site": sk, "component": f"{sk}-pump", "name": "Pump 1 flow",
                        "quantity": "flow", "source": source, "source_tag": pid, "source_unit": "m3/h",
                        "expected_interval_s": 300})
    for r in secs.get("VALVES", []):
        vid = r[0]
        lat, lon = mid(vid)
        sk = f"ltown-{vid.lower()}"
        sites.append({"key": sk, "system": "ltown-dw", "name": f"Pressure reducing valve {vid}",
                      "site_type": "pressure_reducing_valve", "lat": lat, "lon": lon, "ground_level_m": elev.get(r[1]),
                      "note": f"Set to {float(r[5]):.0f} m in the model."})
        comps.append({"key": f"{sk}-valve", "site": sk, "name": vid, "component_type": "pressure_reducing_valve"})
    for link, name in FLOW_SENSORS.items():
        lat, lon = mid(link)
        sk = f"ltown-meter-{link}"
        sites.append({"key": sk, "system": "ltown-dw", "name": name, "site_type": "district_meter", "lat": lat, "lon": lon,
                      "ground_level_m": elev.get(links[link][0])})
        comps.append({"key": f"{sk}-fm", "site": sk, "name": "Flow meter", "component_type": "flow_meter"})
        sensors.append({"key": f"{sk}-flow", "site": sk, "component": f"{sk}-fm", "name": "Inflow",
                        "quantity": "flow", "source": source, "source_tag": link, "source_unit": "m3/h",
                        "expected_interval_s": 300})
    for n in PRESSURE_SENSORS:
        lat, lon = ll(n)
        sk = f"ltown-pl-{n}"
        sites.append({"key": sk, "system": "ltown-dw", "name": f"Pressure logger {n}", "site_type": "pressure_logger",
                      "lat": lat, "lon": lon, "ground_level_m": elev[n], "criticality": 3})
        sensors.append({"key": f"{sk}-p", "site": sk, "name": "Pressure", "quantity": "pressure",
                        "source": source, "source_tag": n, "source_unit": "m",
                        "mount_level_m": {"ref": "above_ground", "value": 0}, "range_lo": 0, "range_hi": 100,
                        "expected_interval_s": 300})
    return {"regions": [region], "systems": [system], "sites": sites, "components": comps, "sensors": sensors}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("inp")
    ap.add_argument("--origin", default="35.0,33.0",
                    help="stand-in lat,lon for the model's south-west corner (default 35.0,33.0)")
    ap.add_argument("--source", default="ltown", help="source key in config.toml")
    a = ap.parse_args(argv)
    lat, lon = (float(v) for v in a.origin.split(","))
    json.dump(build(a.inp, lat, lon, a.source), sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
