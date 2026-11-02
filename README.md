# Waterworks Desk

A read-only operations console for water supply, sewer and drainage networks. It reads every
data source a utility has, groups readings the way the utility is organised (region, system,
site, component, sensor), puts every site on a map and tells an operator in plain words what
needs attention, and why.

It is built from the engine of [Downlink Desk](https://github.com/archer7778/downlink-desk) and
runs on the Python standard library on any ordinary computer or small server.

**Status: M1.** The console has a map of every site at its GPS position, a page per site
with a side view of wells and tanks and charts with limits and rain, setup screens for
everything, critical/warning/advisory alerts in plain language, and the signed audit trail,
in English and Vietnamese. A read-only public demo builds itself on GitHub Pages with two
systems: London's real sewer overflows and rain gauges on a real street map, from live open
feeds, and the BattLeDIM L-Town test network with its known leaks. Next (M2): the learned normal, sensor-health checks and named accounts.

> **Not a safety system.** The console advises. The utility's SCADA alarms, interlocks and
> operators stay in charge. It never sends a command to a pump, valve, gate or PLC.

## What's here

| Part | What it does |
|---|---|
| `waterworks_desk/model.py` | Setting up by hand: regions, systems (drinking water, sanitary sewer, storm drain, combined sewer), zones, sites, components, sensors. Checks in plain words, drafts and go-live, heights typed the way people measure them. |
| `waterworks_desk/sources/` | Read-only adapters: CSV (wide or long, any delimiter, decimal commas, time zones), storm overflow monitors from Stream (any English water company), Environment Agency rainfall, and Open-Meteo weather. |
| `waterworks_desk/alerts.py` | Critical, warning and advisory alerts: limits, silent sensors and sites, overflow and tank levels, failing sources. Severity moved by criticality; one alert per problem; acknowledge and shelve are signed. |
| `waterworks_desk/ingest.py` | Reads sources, converts units, stores readings, keeps 1-minute, 15-minute, hourly and daily rollups, applies retention, then judges alerts. |
| `waterworks_desk/units.py` | One canonical unit per quantity (pressure in metres of head, flow in m³/h, …). |
| `waterworks_desk/logchain.py` | The signed audit trail. Every change names the person who made it. |
| `waterworks_desk/server.py` | JSON API and the console (`web/index.html`, with Leaflet bundled so the map works offline; words in `web/i18n/`). |
| `waterworks_desk/export_static.py` | A read-only snapshot of the console as static files, for the public demo. |
| `tests/` | End-to-end tests, the read-only checks and write traps, a real-data test on L-Town, and a browser test of setting up a site by hand. |
| `tools/` | `build_demo.py` (the whole public demo), `london_setup.py` (London from the overflow and rain feeds), `ltown_setup.py` (setup from the L-Town model), `loadtest.py` (storage sizing). |

## Quick start

```bash
cp config.example.toml config.toml        # then edit it
python -m waterworks_desk.ingest --setup examples/ltown-setup.json --actor "Your name" --live
python -m waterworks_desk.ingest          # read the sources once
python -m waterworks_desk.server          # http://127.0.0.1:8080
```

On a Debian-based machine (a Raspberry Pi included), `./deploy/install.sh` installs it as services.
Automatic updates are **off** by default; update by hand after reviewing changes with
`sudo deploy/update.sh --now`.

## Setting up the network

Everything can be set up one item at a time through `POST /api/setup` (the M1 setup screens
use this), or in bulk from a setup file:

```json
{
  "regions": [{"key": "riverton", "name": "Riverton", "timezone": "Asia/Singapore",
               "datum": "National datum", "lat": 1.3521, "lon": 103.8198, "radius_km": 40}],
  "systems": [{"key": "d1-cs", "region": "riverton", "name": "District 1 combined sewer",
               "network_type": "combined_sewer"}],
  "sites": [{"key": "d1-ps3", "system": "d1-cs", "name": "Quay Street pumping station",
             "site_type": "pumping_station", "lat": "1°17'02\"N", "lon": "103°51'03\"E",
             "ground_level_m": 2.1, "floor_level_m": {"ref": "below_ground", "value": 6.5},
             "overflow_level_m": {"ref": "above_floor", "value": 5.0}}],
  "sensors": [{"key": "d1-ps3-wl", "site": "d1-ps3", "name": "Wet well level", "quantity": "level",
               "source": "exports", "source_tag": "PS3_LT01", "source_unit": "cm",
               "mount_level_m": {"ref": "above_floor", "value": 0.3}, "warn_hi": 4.0, "alarm_hi": 4.6}]
}
```

(Riverton and its sites are invented; the coordinates are only an example.)

- **Heights** are typed as measured: above datum, depth below ground, height above ground,
  height above the floor, depth below the overflow. They are stored as metres above the
  region's datum, and the value as typed is kept.
- **Coordinates** take decimal degrees or degrees, minutes and seconds.
- **New items start as drafts.** Readings are stored, but nothing alerts until someone puts
  the item live, and nothing goes live while a check fails.
- **Every change needs a name** (and the passcode, when one is set) and is written to the
  signed audit trail with what changed.
- `--export FILE` writes the whole setup back out in the same format.

## Alerts

Every alert is one sentence: what, where, since when, why it matters and what to check first.
*Critical* means act now, *warning* means this shift, *advisory* means plan for it. Severity
starts from the rule and moves with criticality (1 raises an advisory to a warning; 3 lowers a
step). A limit alert opens after two readings in a row past the limit and clears after two
back inside; if the same problem returns within 24 hours its alert reopens and counts the
return, so a daily dip is one alert, not one a day. Drafts never alert. Acknowledging and
shelving (always with a reason, for up to 7 days) are signed into the audit trail. Phone
alerts go through ntfy: critical loud, warning quiet.

They are called alerts, never alarms: the utility's SCADA alarms stay in charge.

## The public demo

**London** is real: Thames Water's 573 storm overflow monitors at their real positions with
their live status (spilling, stopped, offline), and the Environment Agency's rain gauges, over
OpenStreetMap. Overflows alert when they discharge and when their monitor goes offline; the
last week is replayed so recent spills are in the history.

**L-Town** is BattLeDIM's test network. It has no real location, so it's drawn without a
street map (its region sets `basemap = 0`), with its 905 mains. Each system is judged at the end
of its own data, so the 2019 test network and the live London feeds sit in one console.

`tools/build_demo.py` builds it with the same code a real installation runs: it sets up L-Town
from its EPANET model, reads 2018, gives each sensor demo limits just outside anything it read
in 2018, puts everything live, reads 2019 and replays the year hour by hour so the alerts it
would have raised are in the history, then exports the read-only console.
`.github/workflows/demo.yml` downloads the data from Zenodo (checking every file's checksum),
fetches London's feeds, builds and publishes it to GitHub Pages every three hours.

## Data sources

Every source is read-only, enforced twice: a code scan of `sources/` fails the build on any
write call, and network adapters are tested against trap servers (HTTP and Modbus TCP) that
fail on any write. Readings are stored as (sensor, time, value, quality) in the sensor's
canonical unit. Tags that arrive with no sensor set up are remembered so they can be added.

## Storage sizing

Measured with `tools/loadtest.py` on a 2-core cloud machine:
3,000 sensors at 1-minute resolution store at about 46,000 readings a second. Each 5-minute
run takes about 0.5 s. Raw data takes about 26 bytes a reading (111 MB a day). A year with
raw data kept 90 days needs about 16.5 GB. Use an SSD, not an SD card. The real L-Town data
(2 years, 7.8 million readings) imports in about 40 seconds into 336 MB.

## Tests

```bash
python -m pytest tests -q
WATERWORKS_LTOWN_DIR=/path/to/battledim python -m pytest tests/test_ltown.py -q   # with the real data
```

## Data credits

- Storm overflows: Thames Water, via [Stream](https://www.streamwaterdata.co.uk/), CC BY 4.0.
- Rainfall: this uses Environment Agency rainfall data from the
  [real-time data API (Beta)](https://environment.data.gov.uk/flood-monitoring/doc/reference),
  Open Government Licence v3.
- Map: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- Weather: [Open-Meteo](https://open-meteo.com/), CC BY 4.0.
- L-Town demo: BattLeDIM dataset, Vrachimis et al. (2020), Zenodo
  [10.5281/zenodo.4017659](https://doi.org/10.5281/zenodo.4017659), CC BY 4.0. The data
  isn't in this repository; download it from Zenodo. The demo places L-Town at a labelled
  stand-in location.

## Licence

MIT. See [LICENSE](LICENSE). Bundled: [Leaflet](https://leafletjs.com/) 1.9.4, BSD 2-Clause
(`web/vendor/leaflet/LICENSE`).
