"""Configuration loading. Paths in the config file are relative to the file's folder.

config.toml holds how this installation runs: where its data lives, the server, alerts, update
policy and the data sources it reads. The network itself (regions, systems, sites, components,
sensors) is set up by hand in the console and lives in the database.
"""
import os
import re
import tomllib
from pathlib import Path

DEFAULTS = {
    "server": {"host": "127.0.0.1", "port": 8080, "write_passcode": ""},
    "storage": {"db": "data/waterworks-desk.db",
                "raw_days": 90,        # raw readings kept this long; 0 keeps them forever
                "minute_days": 400,    # 1-minute rollups kept this long; 0 keeps them forever
                },                     # 15-minute and hourly rollups are always kept
    "ingest": {"poll_minutes": 5},
    "notify": {"ntfy_topic": "", "ntfy_server": "https://ntfy.sh", "console_url": "", "summary_hour": 7},
    "updates": {"auto_update": False},  # never self-update near a real utility network
    "alerts": {"clock": "now"},         # "now"; "data_end" for a historical demo; or an ISO time
    "console": {"title": "Waterworks Desk", "language": "en"},
    "map": {"tile_url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
            "attribution": "© OpenStreetMap contributors", "max_zoom": 19},
}
SOURCE_TYPES = {"csv", "open_meteo"}
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,62}$")


def _coerce(section, key, value, default):
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in ("true", "yes", "on", "1"):
            return True
        if str(value).strip().lower() in ("false", "no", "off", "0"):
            return False
    elif isinstance(default, (int, float)):
        try:
            x = float(value)
        except (TypeError, ValueError):
            x = float("nan")
        if x == x and abs(x) != float("inf"):
            return int(x) if isinstance(default, int) and x == int(x) else x
    else:
        return value if value is not None else default
    raise SystemExit(f"config.toml: {key} under [{section}] should be "
                     f"{'true or false' if isinstance(default, bool) else 'a number'}, not {value!r}.")


class Config:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.root = self.path.parent
        with open(self.path, "rb") as f:
            raw = tomllib.load(f)
        self.data = {}
        for k, v in DEFAULTS.items():
            got = raw.get(k, {})
            if not isinstance(got, dict):
                raise SystemExit(f"config.toml: [{k}] should be a section, not a single value.")
            self.data[k] = {**v, **got}
            for key, default in v.items():
                self.data[k][key] = _coerce(k, key, self.data[k][key], default)
        self.sources = self._sources(raw.get("sources", []))
        for k, v in raw.items():
            if k not in self.data and k != "sources":
                self.data[k] = v

    @staticmethod
    def _sources(raw):
        if not isinstance(raw, list):
            raise SystemExit("config.toml: sources should be written as [[sources]] tables.")
        out, seen = [], set()
        for i, s in enumerate(raw, 1):
            if not isinstance(s, dict):
                raise SystemExit(f"config.toml: source {i} should be a [[sources]] table.")
            key, typ = str(s.get("key", "")).strip(), str(s.get("type", "")).strip()
            if not KEY_RE.match(key):
                raise SystemExit(f"config.toml: source {i} needs a key of lower-case letters, digits, '-', '_' or '.'.")
            if key in seen:
                raise SystemExit(f"config.toml: two sources share the key {key!r}.")
            if key == "manual":
                raise SystemExit("config.toml: 'manual' is reserved for readings typed into the console.")
            if typ not in SOURCE_TYPES:
                raise SystemExit(f"config.toml: source {key!r} has type {typ!r}; known types: {', '.join(sorted(SOURCE_TYPES))}.")
            seen.add(key)
            out.append({**s, "key": key, "type": typ, "enabled": s.get("enabled", True) is not False})
        return out

    def __getitem__(self, key):
        return self.data[key]

    def resolve(self, rel):
        p = Path(rel).expanduser()
        return p if p.is_absolute() else self.root / p

    @property
    def db_path(self):
        return self.resolve(self["storage"]["db"])

    def secret(self, name):
        """A secret from secrets/<name> (outside the repo) or the environment."""
        env = os.environ.get("WATERWORKS_" + re.sub(r"\W", "_", name).upper(), "").strip()
        if env:
            return env
        try:
            return self.resolve("secrets/" + name).read_text().strip()
        except OSError:
            return ""


def find_config(arg=None):
    for cand in [arg, os.environ.get("WATERWORKS_DESK_CONFIG"), "config.toml",
                 Path(__file__).resolve().parent.parent / "config.toml"]:
        if cand and Path(cand).is_file():
            return Config(cand)
    raise SystemExit("No config.toml found. Copy config.example.toml to config.toml and edit it.")
