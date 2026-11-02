"""CSV adapter: a watched folder (or one file) of exports from SCADA, a historian or a logger.

Reads two layouts:
  wide  one timestamp column, then one column per tag (what most SCADA exports look like):
            Timestamp;n1;n4;PUMP_1
            2018-01-01 00:00:00;28,92;33,87;44,04
  long  one row per reading: timestamp, tag, value (and optionally quality):
            time,tag,value,quality
            2026-11-02T07:00:00Z,PS03_P2_PT101,2.41,good

It works out the delimiter and the decimal mark (28,92 vs 28.92) by itself unless told, reads
times in the source's time zone, and remembers each file's SHA-256 so an unchanged file is
never read twice. A file still being written (changed in the last few seconds) waits for the
next run. It only ever opens files for reading.

config.toml:
    [[sources]]
    key = "ltown"
    type = "csv"
    path = "data/import/ltown"        # a folder or a single file
    pattern = "*_SCADA_*.csv"          # optional, default "*.csv"
    layout = "wide"                    # or "long"
    timezone = "UTC"                   # time zone of the timestamps in the files
    # optional: timestamp_column, timestamp_format (strptime), delimiter, decimal,
    #           tag_column, value_column, quality_column (long layout), encoding, settle_seconds
"""
import csv
import hashlib
import io
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..db import BAD, GOOD, SUSPECT
from .base import Adapter

TRUE_WORDS = {"true", "on", "yes", "run", "running", "open", "opened", "start", "started", "discharging", "active"}
FALSE_WORDS = {"false", "off", "no", "stop", "stopped", "closed", "shut", "idle", "not discharging", "inactive"}
QUALITY_WORDS = {"good": GOOD, "ok": GOOD, "0": GOOD, "192": GOOD, "uncertain": SUSPECT, "suspect": SUSPECT,
                 "1": SUSPECT, "bad": BAD, "2": BAD, "invalid": BAD}
COMMA_NUM = re.compile(r"^-?\d+,\d+$")
DOT_NUM = re.compile(r"^-?\d+\.\d+$")


def sniff(sample_lines, delimiter="auto", decimal="auto"):
    """Work out (delimiter, decimal mark) from the first lines of a file."""
    if delimiter == "auto":
        try:
            delimiter = csv.Sniffer().sniff("\n".join(sample_lines[:20]), delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    elif delimiter in ("tab", "\\t"):
        delimiter = "\t"
    if decimal == "auto":
        commas = dots = 0
        for line in sample_lines[1:50]:
            for tok in line.split(delimiter):
                tok = tok.strip().strip('"')
                commas += bool(COMMA_NUM.match(tok))
                dots += bool(DOT_NUM.match(tok))
        decimal = "," if commas > dots and delimiter != "," else "."
    return delimiter, decimal


def to_number(s, decimal):
    s = s.strip().strip('"')
    if not s:
        return None
    if decimal == ",":
        s = s.replace(".", "").replace(",", ".") if s.count(",") == 1 and s.count(".") >= 1 else s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        w = s.lower()
        if w in TRUE_WORDS:
            return 1.0
        if w in FALSE_WORDS:
            return 0.0
        raise
    if v != v or v in (float("inf"), float("-inf")):
        raise ValueError("not finite")
    return v


class TimeParser:
    """Text to UTC seconds. ISO 8601 by default; a strptime format when the source needs one.
    Times without a zone are read in the source's time zone (DST gaps and repeats follow
    zoneinfo: a repeated hour is taken as its first occurrence)."""

    def __init__(self, fmt=None, tz="UTC"):
        self.fmt = fmt
        self.tz = ZoneInfo(tz or "UTC")
        self._last = (None, None)

    def __call__(self, s):
        s = s.strip().strip('"')
        if s == self._last[0]:
            return self._last[1]
        if self.fmt:
            dt = datetime.strptime(s, self.fmt)
        else:
            t = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
            dt = datetime.fromisoformat(t.replace("/", "-"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        ts = int(dt.timestamp())
        self._last = (s, ts)
        return ts


class FileBatch:
    """One file. Readings stream out (a year of 5-minute data for 30 tags is millions of
    readings), and the counts are filled in as they do."""

    def __init__(self, path, digest, source):
        self.path, self.item, self.digest, self.source = path, path.name, digest, source
        self.rows_seen = self.skipped = 0
        self.problems = []
        self.tags_seen = set()

    def _problem(self, msg):
        self.skipped += 1
        if len(self.problems) < 8:
            self.problems.append(msg)

    @property
    def readings(self):
        s = self.source
        enc = s.get("encoding", "utf-8-sig")
        with open(self.path, "r", encoding=enc, errors="replace", newline="") as f:
            head = [f.readline() for _ in range(50)]
            delim, dec = sniff([h.rstrip("\r\n") for h in head if h.strip()], s.get("delimiter", "auto"),
                               s.get("decimal", "auto"))
            f.seek(0)
            reader = csv.reader(f, delimiter=delim)
            header = [h.strip() for h in next(reader, [])]
            if not header:
                self._problem("the file is empty")
                return
            parse_t = TimeParser(s.get("timestamp_format"), s.get("timezone", "UTC"))
            tcol = s.get("timestamp_column") or header[0]
            if tcol not in header:
                self._problem(f"no timestamp column {tcol!r} (columns: {', '.join(header[:8])}…)")
                return
            ti = header.index(tcol)
            horizon = time.time() + 3600
            if s.get("layout", "wide") == "long":
                cols = {}
                for name, default in (("tag_column", "tag"), ("value_column", "value"), ("quality_column", "quality")):
                    c = s.get(name, default)
                    cols[name] = header.index(c) if c in header else None
                if cols["tag_column"] is None or cols["value_column"] is None:
                    self._problem("long layout needs tag and value columns (set tag_column and value_column)")
                    return
                gi, vi, qi = cols["tag_column"], cols["value_column"], cols["quality_column"]
                for n, row in enumerate(reader, 2):
                    if not row or not any(c.strip() for c in row):
                        continue
                    self.rows_seen += 1
                    try:
                        ts = parse_t(row[ti])
                        tag = row[gi].strip()
                        v = to_number(row[vi], dec)
                    except (ValueError, IndexError) as e:
                        self._problem(f"line {n}: {type(e).__name__}: {e}"[:160])
                        continue
                    if v is None or not tag:
                        continue
                    if ts > horizon:
                        self._problem(f"line {n}: time is in the future")
                        continue
                    q = GOOD
                    if qi is not None and qi < len(row) and row[qi].strip():
                        q = QUALITY_WORDS.get(row[qi].strip().lower(), SUSPECT)
                    self.tags_seen.add(tag)
                    yield tag, ts, v, q
            else:
                skip = set(s.get("skip_columns", [])) | {tcol}
                tags = [(i, h) for i, h in enumerate(header) if h and h not in skip]
                self.tags_seen.update(h for _, h in tags)
                for n, row in enumerate(reader, 2):
                    if not row or not any(c.strip() for c in row):
                        continue
                    self.rows_seen += 1
                    try:
                        ts = parse_t(row[ti])
                    except (ValueError, IndexError) as e:
                        self._problem(f"line {n}: can't read the time {row[ti] if ti < len(row) else ''!r}"[:160])
                        continue
                    if ts > horizon:
                        self._problem(f"line {n}: time is in the future")
                        continue
                    for i, tag in tags:
                        if i >= len(row):
                            continue
                        try:
                            v = to_number(row[i], dec)
                        except ValueError:
                            self._problem(f"line {n}, {tag}: {row[i]!r} isn't a number"[:160])
                            continue
                        if v is not None:
                            yield tag, ts, v, GOOD


class CsvFolder(Adapter):
    network = False

    def files(self):
        p = self.cfg.resolve(self.source.get("path", "data/import"))
        if p.is_file():
            return [p]
        if not p.is_dir():
            return []
        return sorted(f for f in p.glob(self.source.get("pattern", "*.csv")) if f.is_file())

    def batches(self, conn, force=False):
        settle = float(self.source.get("settle_seconds", 10))
        for path in self.files():
            try:
                if time.time() - path.stat().st_mtime < settle:
                    continue   # still being written; next run
                h = hashlib.sha256()
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            except OSError:
                continue
            digest = h.hexdigest()
            if not force and conn.execute("SELECT 1 FROM imports WHERE source=? AND item=? AND digest=? AND ok=1 LIMIT 1",
                                          (self.key, path.name, digest)).fetchone():
                continue
            yield FileBatch(path, digest, self.source)
