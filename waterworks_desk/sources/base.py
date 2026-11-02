"""The shape every adapter produces, and the one method every adapter has."""
from typing import Iterator, NamedTuple, Optional


class Reading(NamedTuple):
    tag: str                 # the source's own name for the measurement (a column, tag or register)
    ts: int                  # UTC, whole seconds
    value: float             # in the unit the source sends; the core converts it
    quality: int = 0         # db.GOOD / SUSPECT / BAD / MANUAL


class Batch(NamedTuple):
    """One unit of work from a source (a file, a poll) with what was learned reading it."""
    item: str
    readings: list
    rows_seen: int
    skipped: int
    problems: list
    digest: Optional[str] = None


class Adapter:
    """Adapters only read. `batches()` yields what is new since last time; `remember()` is called
    after a batch is safely stored, so a crash mid-way means it is read again, never lost."""
    network = False          # True for adapters that talk to a server: they must pass the write-trap test

    def __init__(self, cfg, source):
        self.cfg, self.source, self.key = cfg, source, source["key"]

    def batches(self, conn) -> Iterator[Batch]:
        raise NotImplementedError

    def remember(self, conn, batch):
        pass
