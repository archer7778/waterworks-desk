"""Build a setup bundle for London from two open feeds:

  the storm overflows Thames Water publishes on Stream (a saved query result, or the live layer)
  the Environment Agency's rainfall stations (GET /id/stations?parameter=rainfall&lat=..&long=..&dist=..)

    python tools/london_setup.py --overflows thames-overflows.json --rain ea-rain-stations.json > london-setup.json

Every position is the real one, so London is shown over a real street map. Overflows that share
a point (several at one treatment works) become separate sites at the same place.

Data: Thames Water storm overflow activity via Stream, CC BY 4.0; Environment Agency
flood-monitoring API, Open Government Licence v3.
"""
import argparse
import json
import sys


def build(overflows, rain, overflow_source="thames", rain_source="ea"):
    feats = overflows.get("features") or []
    region = {"key": "london", "name": "London and the Thames Valley", "timezone": "Europe/London",
              "datum": "Ordnance Datum Newlyn", "lat": 51.5072, "lon": -0.1276, "radius_km": 150, "basemap": 1,
              "note": "Real positions. Overflows: Thames Water via Stream (CC BY 4.0). Rain: Environment Agency (OGL v3)."}
    system = {"key": "thames-overflows", "region": "london", "name": "Thames Water sewer overflows",
              "network_type": "combined_sewer", "departments": ["operations", "environment"]}
    sites, sensors = [], []
    for f in feats:
        a = f.get("attributes") or {}
        oid = str(a.get("Id") or "").strip()
        if not oid or a.get("Latitude") is None:
            continue
        water = (a.get("ReceivingWaterCourse") or "").strip() or "a watercourse"
        key = f"twl-{oid.lower()}"
        sites.append({"key": key, "system": "thames-overflows", "name": f"Overflow {oid}", "site_type": "overflow_weir",
                      "lat": round(a["Latitude"], 6), "lon": round(a["Longitude"], 6), "criticality": 2,
                      "note": f"Storm overflow discharging to {water}."})
        sensors.append({"key": f"{key}-edm", "site": key, "name": "Spill monitor", "quantity": "state",
                        "source": overflow_source, "source_tag": oid, "note": f"Discharges to {water}. Event duration monitor."})
    rain_system = {"key": "london-rain", "region": "london", "name": "London rain gauges", "network_type": "storm_drain",
                   "departments": ["operations"]}
    seen = set()
    for st in rain.get("items") or []:
        ref = st.get("stationReference") or st.get("notation")
        if not ref or st.get("lat") is None:
            continue
        pos = (round(st["lat"], 5), round(st["long"], 5))
        if pos in seen:      # two station records for one gauge (it happens): keep the first
            continue
        seen.add(pos)
        key = f"ea-{ref.lower()}"
        sites.append({"key": key, "system": "london-rain", "name": f"Rain gauge {ref}", "site_type": "rain_gauge",
                      "lat": st["lat"], "lon": st["long"], "criticality": 3,
                      "note": f"Environment Agency station {ref}, grid reference {st.get('gridReference', '')}."})
        sensors.append({"key": f"{key}-rain", "site": key, "name": "Rainfall (15 min)", "quantity": "rain",
                        "source": rain_source, "source_tag": ref, "source_unit": "mm", "expected_interval_s": 900})
    return {"regions": [region], "systems": [system, rain_system], "sites": sites, "sensors": sensors}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--overflows", required=True, help="saved Stream query result (JSON)")
    ap.add_argument("--rain", required=True, help="saved EA station list (JSON)")
    a = ap.parse_args(argv)
    with open(a.overflows, encoding="utf-8") as f, open(a.rain, encoding="utf-8") as g:
        json.dump(build(json.load(f), json.load(g)), sys.stdout, indent=1)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
