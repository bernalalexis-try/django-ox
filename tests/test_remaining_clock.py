"""
`remaining()` and the watchdog must not disagree about the same instant.

The watchdog fires on `time.monotonic()`. Read from the wall clock instead,
the two sit on different sides of the deadline for as long as an NTP
correction lasts: a backwards step tells a task it has seconds in hand after
the timeout has already fired, and a forwards step makes a well-behaved
cooperative task give up early.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from django.utils import timezone

from django_ox.timeouts import _deadline, _deadline_monotonic, deadline, remaining

pytestmark = pytest.mark.django_db


@contextmanager
def _no_deadline() -> Iterator[None]:
    """Clear both deadline variables, then hand back whatever was there.

    The tests below set them directly, outside any task. Left in place, the
    values read as a live deadline to whichever test runs next in the same
    context, which the default suite order happens to hide.
    """
    wall = _deadline.set(None)
    mono = _deadline_monotonic.set(None)
    try:
        yield
    finally:
        _deadline_monotonic.reset(mono)
        _deadline.reset(wall)


@pytest.fixture(autouse=True)
def _isolated_deadline() -> Iterator[None]:
    with _no_deadline():
        yield


class _WallClockJumped:
    """`timezone.now` as a clock that has just been stepped."""

    def __init__(self, monkeypatch, by_seconds):
        real = timezone.now
        monkeypatch.setattr(
            timezone, "now", lambda: real() + timezone.timedelta(seconds=by_seconds)
        )


class TestRemainingIsMeasuredOnTheEnforcersClock:
    def test_a_backwards_step_does_not_invent_time(self, monkeypatch):
        # Deadline one second out, on both clocks.
        _deadline.set(timezone.now() + timezone.timedelta(seconds=1))
        _deadline_monotonic.set(time.monotonic() + 1)
        # NTP puts the wall clock back an hour. The watchdog is unaffected.
        _WallClockJumped(monkeypatch, -3600)
        assert remaining() < 2, (
            "the task was told it has an hour left; the watchdog will fire in "
            "one second"
        )

    def test_a_forwards_step_does_not_end_the_task_early(self, monkeypatch):
        _deadline.set(timezone.now() + timezone.timedelta(seconds=60))
        _deadline_monotonic.set(time.monotonic() + 60)
        _WallClockJumped(monkeypatch, 3600)
        assert remaining() > 0, (
            "the task was told its deadline had passed while the watchdog "
            "still had 60 seconds on it"
        )

    def test_deadline_stays_a_wall_clock_answer(self):
        at = timezone.now() + timezone.timedelta(seconds=30)
        _deadline.set(at)
        _deadline_monotonic.set(time.monotonic() + 30)
        assert deadline() == at, "deadline() answers when, and when is wall clock"

    def test_no_limit_still_reads_as_no_limit(self):
        _deadline.set(None)
        _deadline_monotonic.set(None)
        assert remaining() is None
        assert deadline() is None


class TestDeadlineIsolation:
    """The isolation must restore what it found, not just clear it."""

    @pytest.fixture
    def incoming(self):
        wall = timezone.now() + timezone.timedelta(seconds=90)
        mono = time.monotonic() + 90
        _deadline.set(wall)
        _deadline_monotonic.set(mono)
        return wall, mono

    def test_clears_both_variables_inside(self, incoming):
        with _no_deadline():
            assert _deadline.get() is None
            assert _deadline_monotonic.get() is None

    def test_restores_incoming_values_afterwards(self, incoming):
        with _no_deadline():
            _deadline.set(timezone.now())
            _deadline_monotonic.set(time.monotonic())
        assert (_deadline.get(), _deadline_monotonic.get()) == incoming

    def test_restores_incoming_values_when_the_body_fails(self, incoming):
        with pytest.raises(AssertionError), _no_deadline():
            _deadline.set(timezone.now())
            _deadline_monotonic.set(time.monotonic())
            raise AssertionError("a failing test")
        assert (_deadline.get(), _deadline_monotonic.get()) == incoming
