"""Data source adapters. Every adapter turns its source into one shape, Reading(tag, ts, value,
quality), and the core never knows which protocol a reading came from.

READ-ONLY RULE: nothing in this package may write to a source, ever. No adapter has a write
path; tests/test_readonly.py fails the build if one appears (a code scan of this folder) and
runs network adapters against a trap server that fails on any write.
"""
from .base import Adapter, Reading  # noqa: F401
from .csvfile import CsvFolder
from .openmeteo import OpenMeteo

ADAPTERS = {"csv": CsvFolder, "open_meteo": OpenMeteo}


def make(cfg, source):
    return ADAPTERS[source["type"]](cfg, source)
