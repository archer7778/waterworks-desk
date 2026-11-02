"""SQLite storage. One file; WAL mode lets the web server read while the ingest job writes.

Readings are the bulk of the data (millions of rows a day for a mid-sized town), so they are
kept narrow: an integer sensor id, a time in whole seconds, the value and a quality code, in a
WITHOUT ROWID table keyed by (sensor_id, ts). Everything about a sensor (name, unit, limits,
where it is) lives once in `sensors`, never repeated per reading.

Everything a person sets up (systems, sites, components, sensors) lives here too, not in config
files, so a utility can change its network from the console without a release.
"""
import json
import sqlite3
import time

NETWORK_TYPES = ("drinking_water", "sanitary_sewer", "storm_drain", "combined_sewer")
STATUSES = ("draft", "live", "retired")
SEVERITIES = ("critical", "warning", "advisory")
ROLLUP_RES = (60, 900, 3600)   # 1 minute, 15 minutes, 1 hour

# Quality codes stored with every reading.
GOOD, SUSPECT, BAD, MANUAL = 0, 1, 2, 3

SCHEMA = """
CREATE TABLE IF NOT EXISTS regions (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  datum TEXT NOT NULL DEFAULT '',          -- vertical datum every height is measured from
  language TEXT NOT NULL DEFAULT 'en',
  lat REAL, lon REAL,                       -- centre, for the map and the weather source
  radius_km REAL,                           -- sites must fall within this distance of the centre
  note TEXT NOT NULL DEFAULT '',
  created_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS systems (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  region_id INTEGER NOT NULL REFERENCES regions(id),
  name TEXT NOT NULL,
  network_type TEXT NOT NULL CHECK (network_type IN ('drinking_water','sanitary_sewer','storm_drain','combined_sewer')),
  departments TEXT NOT NULL DEFAULT '',     -- comma-separated tags: who acts on it
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','retired')),
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS zones (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  system_id INTEGER NOT NULL REFERENCES systems(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT '',            -- pressure zone, metered district, catchment, drainage basin
  boundary_json TEXT,                       -- GeoJSON polygon, optional
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','retired')),
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  system_id INTEGER NOT NULL REFERENCES systems(id),
  zone_id INTEGER REFERENCES zones(id),
  name TEXT NOT NULL,
  site_type TEXT NOT NULL,
  lat REAL NOT NULL, lon REAL NOT NULL,     -- WGS84 decimal degrees
  ground_level_m REAL,                      -- metres above the region's datum
  floor_level_m REAL,                       -- wet wells and tanks: floor, above datum
  overflow_level_m REAL,                    -- wet wells, tanks, weirs: overflow or crest, above datum
  area_curve_json TEXT,                     -- [[depth_m, area_m2], ...] for wells, tanks, ponds
  criticality INTEGER NOT NULL DEFAULT 2 CHECK (criticality BETWEEN 1 AND 3),   -- 1 highest
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','retired')),
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS sites_system ON sites (system_id);
CREATE TABLE IF NOT EXISTS component_models (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  component_type TEXT NOT NULL,
  maker TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  data_json TEXT NOT NULL DEFAULT '{}'      -- datasheet values, each with its source
);
CREATE TABLE IF NOT EXISTS components (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL REFERENCES sites(id),
  name TEXT NOT NULL,
  component_type TEXT NOT NULL,
  model_id INTEGER REFERENCES component_models(id),
  serial TEXT NOT NULL DEFAULT '',
  install_date TEXT,                        -- YYYY-MM-DD
  criticality INTEGER NOT NULL DEFAULT 2 CHECK (criticality BETWEEN 1 AND 3),
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','retired')),
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS components_site ON components (site_id);
CREATE TABLE IF NOT EXISTS sensors (
  id INTEGER PRIMARY KEY,
  key TEXT NOT NULL UNIQUE,
  site_id INTEGER NOT NULL REFERENCES sites(id),
  component_id INTEGER REFERENCES components(id),
  name TEXT NOT NULL,                       -- plain words: "Pump 2 discharge pressure"
  quantity TEXT NOT NULL,                   -- see units.QUANTITIES
  unit TEXT NOT NULL,                       -- canonical unit for the quantity; readings are stored in it
  display_unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',          -- source key from config.toml, or 'manual'
  source_tag TEXT,                          -- the tag, column or register this sensor's readings carry
  source_unit TEXT NOT NULL DEFAULT '',     -- unit the source sends; converted on the way in
  mount_level_m REAL,                       -- level sensors and gauges: zero point, above datum
  mount_entry_json TEXT,                    -- the height as it was typed: {"ref": ..., "value": ...}
  range_lo REAL, range_hi REAL,
  alarm_lo REAL, warn_lo REAL, warn_hi REAL, alarm_hi REAL,
  expected_interval_s INTEGER,              -- how often it normally reports
  note TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','live','retired')),
  created_ms INTEGER NOT NULL,
  updated_ms INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS sensors_source_tag ON sensors (source, source_tag) WHERE source_tag IS NOT NULL;
CREATE INDEX IF NOT EXISTS sensors_site ON sensors (site_id);
CREATE TABLE IF NOT EXISTS readings (
  sensor_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                      -- UTC, whole seconds
  value REAL NOT NULL,
  quality INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (sensor_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS rollups (
  res INTEGER NOT NULL,                     -- seconds per bucket
  sensor_id INTEGER NOT NULL,
  ts INTEGER NOT NULL,                      -- bucket start, UTC seconds
  n INTEGER NOT NULL,
  mean REAL NOT NULL, lo REAL NOT NULL, hi REAL NOT NULL,
  PRIMARY KEY (res, sensor_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY,
  severity TEXT NOT NULL CHECK (severity IN ('critical','warning','advisory')),
  rule TEXT NOT NULL,
  sensor_id INTEGER, site_id INTEGER, component_id INTEGER,
  message TEXT NOT NULL,                    -- one plain sentence: what, where, since when, why, first check
  start_ms INTEGER NOT NULL, end_ms INTEGER,
  ack_ms INTEGER, ack_by TEXT,
  shelved_until_ms INTEGER, shelved_by TEXT, shelve_reason TEXT,
  data_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS alerts_open ON alerts (end_ms, severity);
CREATE TABLE IF NOT EXISTS work_orders (
  id INTEGER PRIMARY KEY,
  component_id INTEGER NOT NULL REFERENCES components(id),
  kind TEXT NOT NULL,                       -- maintenance_done, overhaul, replacement, inspection
  opened_ms INTEGER NOT NULL, closed_ms INTEGER,
  by TEXT NOT NULL, note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS imports (
  id INTEGER PRIMARY KEY,
  t_ms INTEGER NOT NULL,
  source TEXT NOT NULL,
  item TEXT NOT NULL,                       -- file name, or the request made
  digest TEXT,                              -- SHA-256 of a file, so an unchanged file is not read twice
  ok INTEGER NOT NULL,
  rows_seen INTEGER NOT NULL DEFAULT 0,
  readings_stored INTEGER NOT NULL DEFAULT 0,
  skipped INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS imports_item ON imports (source, item);
CREATE TABLE IF NOT EXISTS log (
  seq INTEGER PRIMARY KEY,
  t_ms INTEGER NOT NULL,
  kind TEXT NOT NULL,
  data_json TEXT NOT NULL,
  msg TEXT NOT NULL,
  actor TEXT NOT NULL,                      -- the person (or 'system') who made the entry
  prev TEXT NOT NULL,
  hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SCHEMA_VERSION = 1


def now_ms():
    return int(time.time() * 1000)


def connect(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_size_limit=67108864")
    conn.executescript(SCHEMA)
    if meta_get(conn, "schema_version") is None:
        meta_set(conn, "schema_version", SCHEMA_VERSION)
    conn.commit()
    return conn


def connect_ro(path):
    """A light read-only connection for the web server (no schema work, no writes)."""
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def open_db(cfg):
    return connect(cfg.db_path)


def meta_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
    conn.commit()


def record_import(conn, source, item, ok, rows_seen=0, stored=0, skipped=0, message="", digest=None):
    conn.execute("INSERT INTO imports(t_ms,source,item,digest,ok,rows_seen,readings_stored,skipped,message)"
                 " VALUES(?,?,?,?,?,?,?,?,?)",
                 (now_ms(), source, item[:300], digest, 1 if ok else 0, rows_seen, stored, skipped, message[:500]))
    conn.execute("DELETE FROM imports WHERE id NOT IN (SELECT id FROM imports ORDER BY id DESC LIMIT 2000)")
    conn.commit()


def storage(conn, db_path):
    """Disk size and use, the database's size, and how fast it's growing."""
    import shutil
    out = {}
    try:
        du = shutil.disk_usage(db_path.parent)
        out.update(disk_total=du.total, disk_used=du.used, disk_free=du.free)
    except OSError:
        pass
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += db_path.with_name(db_path.name + suffix).stat().st_size
        except OSError:
            pass
    out["db_bytes"] = size
    week_s = 7 * 86_400
    newest = conn.execute("SELECT MAX(t_ms) FROM imports WHERE ok=1").fetchone()[0]
    stored_week = conn.execute("SELECT SUM(readings_stored) FROM imports WHERE t_ms>?",
                               (now_ms() - week_s * 1000,)).fetchone()[0] or 0
    total = conn.execute("SELECT SUM(readings_stored) FROM imports").fetchone()[0] or 0
    if total and size and newest:
        per_day = size / total * stored_week / 7
        out["db_bytes_per_day"] = round(per_day)
        if per_day > 0 and "disk_free" in out:
            out["days_to_full"] = round(out["disk_free"] / per_day)
    if "disk_total" in out:
        frac = out["disk_free"] / out["disk_total"]
        out["level"] = ("crit" if frac < 0.03 or out["disk_free"] < 300e6
                        else "warn" if frac < 0.10 or out["disk_free"] < 1e9 else "ok")
    return out
