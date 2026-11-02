# Waterworks Desk

A read-only operations console for water supply, sewer and drainage networks. It reads every
data source a utility has, groups readings the way the utility is organised (region, system,
site, component, sensor), puts every site on a map and tells an operator in plain words what
needs attention, and why.

It is built from the engine of [Downlink Desk](https://github.com/archer7778/downlink-desk) and
runs on the Python standard library on any ordinary computer or small server.

**Live demo: [archer7778.github.io/waterworks-desk](https://archer7778.github.io/waterworks-desk/)**:
Thames Water's 573 storm overflows and the Environment Agency's rain gauges, read from open
data and rebuilt automatically every three hours.

**Status: M1 (v0.4).** The console has a map of every site at its GPS position, a page per
site with a side view of wells and tanks and charts with limits and rain, setup screens for
everything, critical/warning/advisory alerts in plain language, storm overflow spill counts
with possible dry-day spill alerts, and the signed audit trail, in English and Vietnamese.
Next (M2): the learned normal, sensor-health checks and named accounts.

> **Not a safety system.** The console advises. The utility's SCADA alarms, interlocks and
> operators stay in charge. It never sends a command to a pump, valve, gate or PLC.

## What's here

| Part | What it does |
|---|---|
| `waterworks_desk/model.py` | Setting up by hand: regions, systems (drinking water, sanitary sewer, storm drain, combined sewer), zones, sites, components, sensors. Checks in plain words, drafts and go-live, heights typed the way people measure them. |
| `waterworks_desk/sources/` | Read-only adapters: CSV (wide or long, any delimiter, decimal commas, time zones), storm overflow monitors from Stream (any English water company), Environment Agency rainfall, and Open-Meteo weather. |
| `waterworks_desk/alerts.py` | Critical, warning and advisory alerts: limits, silent sensors and sites, overflow and tank levels, overflows spilling (and possibly spilling on a dry day), monitors offline, failing sources. Severity moved by criticality; one alert per problem; acknowledge and shelve are signed. |
| `waterworks_desk/spills.py` | Storm overflow spill periods, the 12/24 count used in England's returns, and the dry-day check against the nearest rain gauges. |
| `waterworks_desk/ingest.py` | Reads sources, converts units, stores readings, keeps 1-minute, 15-minute, hourly and daily rollups, applies retention, then judges alerts. |
| `waterworks_desk/units.py` | One canonical unit per quantity (pressure in metres of head, flow in m³/h, …). |
| `waterworks_desk/logchain.py` | The signed audit trail. Every change names the person who made it. |
| `waterworks_desk/server.py` | JSON API and the console (`web/index.html`, with Leaflet bundled so the map works offline; words in `web/i18n/`). |
| `waterworks_desk/export_static.py` | A read-only snapshot of the console as static files, for the public demo. |
| `tests/` | End-to-end tests, the read-only checks and write traps, tests on small samples of the real London feeds, and a browser test of setting up a site by hand. |
| `tools/` | `build_demo.py` (the whole public demo), `london_setup.py` (London from the overflow and rain feeds), `loadtest.py` (storage sizing). |

## Quick start

```bash
cp config.example.toml config.toml        # then edit it
python tools/london_setup.py > london-setup.json      # London; or set your own network up in the console
python -m waterworks_desk.ingest --setup london-setup.json --actor "Your name" --live
python -m waterworks_desk.ingest          # read the sources once
python -m waterworks_desk.server          # http://127.0.0.1:8080
```

On a Debian-based machine (a Raspberry Pi included), `./deploy/install.sh` installs it as services.
Automatic updates are **off** by default; update by hand after reviewing changes with
`sudo deploy/update.sh --now`.

## Using the console

| Page | For |
|---|---|
| **Overview** | A status strip (overflows spilling now, spilled in the last 24 hours, possible dry-day spills, sites offline, rain, data-feed freshness); each number filters the map. The map colours sites by state: problems drawn one by one (spilling ones pulse), healthy sites faded and gathered into numbered clusters when zoomed out, rain gauges as circles sized by the last day's rain. Clicking a site opens a preview beside the map. Alerts are grouped by kind with critical and new ones in full, and a 24-hour timeline shows rain, overflows spilling and sites offline against time. |
| **Alerts** | Every alert, filtered by state (active, not acknowledged, shelved, cleared this week), severity and kind, sorted by any column, and acknowledged or shelved from the list. |
| **Sites** | Every site in one table: status, latest reading and when it last reported. Sort by *Last data* to find sites that have gone quiet. |
| **Spills** | Storm overflow spills per overflow, filtered by receiving water or to possible dry-day spills. |
| **Setup** / **Audit trail** | Setting the network up, and the signed record of who changed what. |

Press `/` to search the page you're on, or to find any site by name or key from the box at the
top. Each list downloads as a CSV of what it shows, and remembers its filters, so the page you
come back to is the one you left.

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

## Storm overflow spills

The **Spills** page counts each overflow's spills over the last 7 days, 30 days, 12 months or
all the data, with the 12/24 method used for England's event duration monitoring returns (the
first 12 hours of discharge is one spill, each following 24-hour block with any discharge is
one more), the hours discharging, and the receiving water. Each overflow's page shows its own
totals.

A spill that starts on a dry day raises a **possible dry-day spill** alert (critical). Dry
follows the Environment Agency's definition: no more than 0.25 mm of rain that day and the 24
hours before, judged at the nearest three rain gauges within 15 km, all of which must be dry.
With no gauge close enough, or too few gauge readings, it says it can't tell rather than
guessing. The EA also uses rainfall radar and allows for drain-down in large catchments, so
the console flags what to look into; the company's annual return is the official count.

## The public demo

**London** is real: Thames Water's 573 storm overflow monitors at their real positions with
their live status (spilling, stopped, offline), and the Environment Agency's rain gauges, over
OpenStreetMap. Overflows alert when they discharge and when their monitor goes offline; the
last week is replayed so recent spills are in the history. Stream's feed gives each overflow's
latest spill only, so the workflow keeps the demo database between runs (`--keep`, with a
GitHub Actions cache) and the spill history grows from the first run onward.

`tools/build_demo.py` builds it with the same code a real installation runs: it sets London
up from the two feeds, reads them, replays the last week so recent spills are in the alert
history, and exports the read-only console. `.github/workflows/demo.yml` runs it and publishes
the result to GitHub Pages every three hours, on GitHub's own schedule: nothing needs to be
running anywhere else. GitHub switches off schedules in public repositories after 60 days
without a commit; the workflow re-enables itself on each run to stop that. If it ever does
stop, GitHub emails the owner, and *Actions → Demo → Enable workflow* turns it back on.
To fork it: Settings → Pages → Source: GitHub Actions, then run the Demo workflow once.

A region with no real location (a test network) can set `basemap = 0` to be drawn without a
street map; systems can carry map layers such as pipes and sewers (GeoJSON).

## Data sources

Every source is read-only, enforced twice: a code scan of `sources/` fails the build on any
write call, and network adapters are tested against trap servers (HTTP and Modbus TCP) that
fail on any write. Readings are stored as (sensor, time, value, quality) in the sensor's
canonical unit. Tags that arrive with no sensor set up are remembered so they can be added.

## Storage sizing

Measured with `tools/loadtest.py` on a 2-core cloud machine:
3,000 sensors at 1-minute resolution store at about 46,000 readings a second. Each 5-minute
run takes about 0.5 s. Raw data takes about 26 bytes a reading (111 MB a day). A year with
raw data kept 90 days needs about 16.5 GB. Use an SSD, not an SD card.

## Tests

```bash
python -m pytest tests -q
```

## Data credits

- Storm overflows: Thames Water, via [Stream](https://www.streamwaterdata.co.uk/), CC BY 4.0.
- Rainfall: this uses Environment Agency rainfall data from the
  [real-time data API (Beta)](https://environment.data.gov.uk/flood-monitoring/doc/reference),
  Open Government Licence v3.
- Map: © [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors.
- Weather: [Open-Meteo](https://open-meteo.com/), CC BY 4.0.

## Licence

MIT. See [LICENSE](LICENSE). Bundled: [Leaflet](https://leafletjs.com/) 1.9.4, BSD 2-Clause
(`web/vendor/leaflet/LICENSE`).
