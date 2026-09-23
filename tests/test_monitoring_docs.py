"""Every log event the package emits is documented."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
DOC = Path(__file__).resolve().parent.parent / "docs" / "monitoring.md"


#: Events are emitted two ways: an inline `extra={"event": "..."}`, and as
#: the first argument to a Worker helper that builds the extra itself,
#: _log_extra or _complete. Both are matched, or the coverage claim below
#: is not true.
_INLINE = re.compile(r'"event":\s*"([a-z_]+)"')
_HELPER = re.compile(r'\b(?:_log_extra|_complete)\(\s*"([a-z_]+)"')
#: The first cell of a table row, where the events table names each event.
_ROW = re.compile(r"^\| `([a-z_]+)` \|", re.MULTILINE)


def emitted_events() -> set[str]:
    events: set[str] = set()
    for path in SRC.rglob("*.py"):
        text = path.read_text()
        events |= set(_INLINE.findall(text)) | set(_HELPER.findall(text))
    return events


def test_every_event_is_documented():
    # An operator alerts on these names, so one that exists and is written
    # down nowhere is a signal nobody knows to watch for.
    documented = DOC.read_text()
    events = emitted_events()
    # A floor, so a regex that stops matching reports an empty set and fails
    # here rather than passing with nothing to check.
    assert len(events) >= 30, f"the event scanner found only {len(events)}"
    # A row of its own rather than a mention: most events are also named in
    # the key table or the prose, which would still be there for an event
    # whose own row was deleted.
    rows = set(_ROW.findall(documented))
    missing = sorted(events - rows)
    assert not missing, f"events with no row in docs/monitoring.md: {missing}"


# There is deliberately no test for the reverse direction, a documented
# event the code never emits. monitoring.md holds several tables and the
# field and metric names in them are indistinguishable from event names by
# any cheap parse, so such a check would either need the doc structure
# hard-coded or pass by matching too little. A test weakened until it
# passes is worse than the gap it covers.
