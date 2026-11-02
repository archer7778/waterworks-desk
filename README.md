# Waterworks Desk

A read-only operations console for water supply, sewer and drainage networks. It reads every
data source a utility has, groups readings the way the utility is organised (region, system,
site, component, sensor) and, from M1 on, tells an operator in plain words what is normal,
what is drifting and what needs attention.

It is built from the engine of [Downlink Desk](https://github.com/archer7778/downlink-desk) and
runs on the Python standard library, on anything from a Raspberry Pi 5 to a small server.

**Status: M0, the foundation.** Storage, setup by hand, the CSV adapter, the read-only
guards and the API are in place and tested. The map, setup screens and alerts arrive in M1.

> **Not a safety system.** The console advises. The utility's SCADA alarms, interlocks and
> operators stay in charge. It never sends a command to a pump, valve, gate or PLC.

## What's here

| Part | What it does |
|---|---|
| `waterworks_desk/model.py` | Setting up by hand: regions, systems (drinking water, sanitary sewer, storm drain, combined sewer), zones, sites, components, sensors. Checks in plain words, drafts and go-live, heights typed the way people measure them. |
| `waterworks_desk/sources/` | Read-only adapters. M0: CSV (wide or long, any delimiter, decimal commas, time zones). |
| `waterworks_desk/ingest.py` | Reads sources, converts units, stores readings, keeps 1-minute, 15-minute and hourly rollups, applies retention. |
| `waterworks_desk/units.py` | One canonical unit per quantity (pressure in metres of head, flow in m³/h, …). |
| `waterworks_desk/logchain.py` | The signed audit trail. Every change names the person who made it. |
| `waterworks_desk/server.py` | JSON API and the console page. |
| `tests/` | End-to-end tests, the read-only checks and write traps, and a real-data test on L-Town. |
| `tools/` | `ltown_setup.py` (demo setup from the L-Town model), `loadtest.py` (storage sizing). |

## Quick start

```bash
cp config.example.toml config.toml        # then edit it
python -m waterworks_desk.ingest --setup examples/ltown-setup.json --actor "Your name" --live
python -m waterworks_desk.ingest          # read the sources once
python -m waterworks_desk.server          # http://127.0.0.1:8080
```

On a Raspberry Pi or other Debian machine, `./deploy/install.sh` installs it as services.
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

- L-Town demo: BattLeDIM dataset, Vrachimis et al. (2020), Zenodo
  [10.5281/zenodo.4017659](https://doi.org/10.5281/zenodo.4017659), CC BY 4.0. The data
  isn't in this repository; download it from Zenodo. The demo places L-Town at a labelled
  stand-in location.

## Licence

MIT. See [LICENSE](LICENSE).
