"""
Pausing and repairing a stored schedule that holds a value that cannot be read.

A row written around django_ox.stored, by SQL, a fixture or a data
migration, can hold what its columns accept and Django cannot read back:
PostgreSQL keeps year 10000 and 'infinity', SQLite keeps a date that does
not exist, text in an integer column and text that is not UTF-8, MySQL
keeps zero dates. Django's read of such a row raises, so a caller has no
instance to hand `update_schedule`, only the row's key. On SQLite some of
these read as something else instead: an end that does not read comes back
as no end at all, which a write of the whole row would save.

`update_schedule` locks the row with the read of it and decodes it a value
at a time. `enabled=False` pauses it whatever else it holds, and reads no
more of the row than a pause needs. A change that replaces every value
that does not read repairs it, and starts it on a fresh boundary. Anything
else raises StoredValueUnreadable, naming what is left, and writes nothing.

Every value is written by SQL in the form the engine that keeps it keeps
it. A case for another engine's value skips and says why.
"""

import logging
import threading
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import (
    DatabaseError,
    DataError,
    connection,
    connections,
    transaction,
)
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ox import registry, stored
from django_ox._stored_read import UnreadableRow, read_schedules
from django_ox.exceptions import StoredValueUnreadable
from django_ox.models import OxSchedule, OxScheduleChange
from django_ox.registry import ScheduleKind, register
from django_ox.stored import (
    DatabaseScheduleSource,
    boundary_digest,
    create_schedule,
    delete_schedule,
    update_schedule,
    validate_schedule,
)

from . import tasks
from .test_stored_read import (
    Refusing,
    columns_read,
    largest_count,
    seen_waiting_for_the_lock,
)

SCHEDULES = "django_ox_oxschedule"
ALIAS = "default"
WRITTEN = "schedule_unreadable_row_written"

#: Text that is not UTF-8, as SQLite keeps it.
NOT_UTF_8 = "CAST(X'62616480FF' AS TEXT)"

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.record))
    register(
        ScheduleKind(key="guarded", task=tasks.record, permission="auth.view_user")
    )


def a_row(name="the-row", **over):
    """A valid stored schedule, written through the write functions."""
    fields = {
        "name": name,
        "task_key": "report",
        "trigger": "interval",
        "every_seconds": 60,
        "starting_deadline_seconds": 600,
        "start_time": timezone.now() - timedelta(minutes=10),
    }
    fields.update(over)
    return create_schedule(**fields)


def by_sql(statement, params=(), using=ALIAS):
    """
    Run one statement the way only SQL can write what it writes.

    MySQL refuses a zero date and a NULL in a NOT NULL column in the strict
    mode the suite runs in, so its session is let off for the statement and
    put back.
    """
    conn = connections[using]
    with conn.cursor() as cursor:
        if conn.vendor != "mysql":
            cursor.execute(statement, params)
            return
        cursor.execute("SELECT @@SESSION.sql_mode")
        (mode,) = cursor.fetchone()
        cursor.execute("SET SESSION sql_mode = ''")
        try:
            cursor.execute(statement, params)
        finally:
            cursor.execute("SET SESSION sql_mode = %s", [mode])


def set_column(pk, column, literal):
    by_sql(f"UPDATE {SCHEDULES} SET {column} = {literal} WHERE id = {int(pk)}")  # noqa: S608


def only_on(vendor, why):
    if connection.vendor != vendor:
        pytest.skip(f"a {vendor} value: {why}; this run is on {connection.vendor}")


def read(pk):
    """The row as the guarded read finds it: an OxSchedule or an UnreadableRow."""
    return read_schedules([pk], using=ALIAS)[pk]


def django_cannot_read(pk):
    """Django's own read of the row raises, or reads something else."""
    try:
        with transaction.atomic():
            OxSchedule.objects.get(pk=pk)
    except Exception:  # what it raises differs by engine; that it raises is the point
        return True
    return isinstance(read(pk), UnreadableRow)


def marker():
    return OxScheduleChange.objects.get().changed_at


@pytest.fixture
def committed(django_capture_on_commit_callbacks):
    """
    Run what the block leaves for its transaction's commit, as a commit
    would. A test here runs inside a transaction that is never committed.
    """
    return lambda: django_capture_on_commit_callbacks(execute=True)


def events(caplog, name):
    return [r for r in caplog.records if getattr(r, "event", None) == name]


#: The fields a write here sets for itself, which a repair is expected to move.
OWN = {"start_time", "updated_at", "boundary_for", "boundary_generation"}

COMPARED = [
    "name",
    "task_key",
    "trigger",
    "cron",
    "every_seconds",
    "phase_seconds",
    "arguments",
    "enabled",
    "end_time",
    "starting_deadline_seconds",
    "created_at",
]


def as_it_was(row, *leave_out):
    return {name: getattr(row, name) for name in COMPARED if name not in leave_out}


#: The columns a pause reads of the row it pauses, the key among them: the
#: task key the registry's permission is asked by, and what the boundary is
#: kept in and taken over.
PAUSE_READS = {
    "id",
    "task_key",
    "trigger",
    "cron",
    "every_seconds",
    "phase_seconds",
    "enabled",
    "boundary_for",
    "boundary_generation",
}


#: (engine, column, SQL literal, the change that replaces it, id). Each is a
#: value the engine keeps that Django's read raises on or reads as something
#: else. A column the write sets for itself (the start, updated_at) is
#: repaired by any change; `enabled=True` is the change used for those.
UNREADABLE = [
    pytest.param(
        "sqlite",
        "end_time",
        "'2026-02-30 00:00:00'",
        {"end_time": None},
        id="sqlite-end-does-not-exist",
    ),
    pytest.param(
        "sqlite",
        "end_time",
        "'10000-01-01 00:00:00'",
        {"end_time": None},
        id="sqlite-end-year-10000",
    ),
    pytest.param(
        "sqlite", "end_time", "'banana'", {"end_time": None}, id="sqlite-end-banana"
    ),
    pytest.param(
        "sqlite",
        "start_time",
        "'10000-01-01 00:00:00'",
        {"enabled": True},
        id="sqlite-start-year-10000",
    ),
    pytest.param(
        "sqlite", "start_time", "'banana'", {"enabled": True}, id="sqlite-start-banana"
    ),
    pytest.param(
        "sqlite",
        "updated_at",
        "'banana'",
        {"enabled": True},
        id="sqlite-updated-banana",
    ),
    pytest.param(
        "sqlite",
        "every_seconds",
        "'abc'",
        {"every_seconds": 60},
        id="sqlite-interval-text",
    ),
    pytest.param(
        "sqlite", "phase_seconds", "'abc'", {"phase_seconds": 0}, id="sqlite-phase-text"
    ),
    pytest.param(
        "sqlite",
        "starting_deadline_seconds",
        "'abc'",
        {"starting_deadline_seconds": None},
        id="sqlite-deadline-text",
    ),
    pytest.param("sqlite", "enabled", "2", {"enabled": True}, id="sqlite-enabled-2"),
    pytest.param(
        "sqlite", "enabled", "'abc'", {"enabled": True}, id="sqlite-enabled-text"
    ),
    pytest.param(
        "sqlite", "name", NOT_UTF_8, {"name": "renamed"}, id="sqlite-name-not-utf-8"
    ),
    pytest.param(
        "postgresql",
        "end_time",
        "'10000-01-01 00:00:00+00'",
        {"end_time": None},
        id="postgresql-end-year-10000",
    ),
    pytest.param(
        "postgresql",
        "end_time",
        "'infinity'",
        {"end_time": None},
        id="postgresql-end-infinity",
    ),
    pytest.param(
        "postgresql",
        "start_time",
        "'10000-01-01 00:00:00+00'",
        {"enabled": True},
        id="postgresql-start-year-10000",
    ),
    pytest.param(
        "postgresql",
        "start_time",
        "'-infinity'",
        {"enabled": True},
        id="postgresql-start-minus-infinity",
    ),
    pytest.param(
        "postgresql",
        "start_time",
        "'0001-01-01 00:00:00+00 BC'",
        {"enabled": True},
        id="postgresql-start-bc",
    ),
    pytest.param(
        "postgresql",
        "updated_at",
        "'10000-01-01 00:00:00+00'",
        {"enabled": True},
        id="postgresql-updated-year-10000",
    ),
    pytest.param(
        "mysql",
        "end_time",
        "'0000-00-00 00:00:00'",
        {"end_time": None},
        id="mysql-end-zero-date",
    ),
    pytest.param(
        "mysql",
        "start_time",
        "'2026-00-10 00:00:00'",
        {"enabled": True},
        id="mysql-start-zero-month",
    ),
    pytest.param(
        "mysql",
        "start_time",
        "NULL",
        {"enabled": True},
        id="mysql-start-null-kept-as-zero",
    ),
    pytest.param("mysql", "enabled", "2", {"enabled": True}, id="mysql-enabled-2"),
]

#: One value per engine for a column no change can set as a side effect:
#: the end of the schedule.
UNREADABLE_END = {
    "sqlite": "'2026-02-30 00:00:00'",
    "postgresql": "'infinity'",
    "mysql": "'0000-00-00 00:00:00'",
}


def an_unreadable_end(row):
    set_column(row.pk, "end_time", UNREADABLE_END[connection.vendor])
    assert django_cannot_read(row.pk)
    return row.pk


def unreadable(vendor, column, literal, name="the-row", **over):
    """A valid row with `column` then set to `literal` by SQL; it and its key."""
    only_on(vendor, f"{column} = {literal}")
    row = a_row(name, **over)
    set_column(row.pk, column, literal)
    found = read(row.pk)
    assert isinstance(found, UnreadableRow), found
    assert column in found.unreadable, found.unreadable
    assert django_cannot_read(row.pk)
    return row, row.pk


class TestPausingByKey:
    """
    `update_schedule(OxSchedule(pk=pk), enabled=False)` stops a row that
    holds a value that does not read, without reading or writing that value.
    """

    @pytest.mark.parametrize(("vendor", "column", "literal", "repair"), UNREADABLE)
    def test_the_row_is_paused_and_the_workers_told(
        self, vendor, column, literal, repair, caplog, committed
    ):
        _, pk = unreadable(vendor, column, literal)
        before = read(pk)
        changed = marker()
        bare = OxSchedule(pk=pk)
        with caplog.at_level(logging.INFO, logger="django_ox"), committed():
            returned = update_schedule(bare, enabled=False)
        assert isinstance(returned, OxSchedule)
        assert returned.pk == pk
        assert returned.enabled is False
        after = read(pk)
        values = after.values if isinstance(after, UnreadableRow) else after.__dict__
        assert values["enabled"] is False
        assert marker() > changed, "the workers were not told"
        if column in ("enabled", "updated_at"):
            # Values the pause writes, so the row reads again, and is handed
            # back on the caller's own instance.
            assert isinstance(after, OxSchedule)
            assert returned is bare
            assert after.boundary_for == boundary_digest(after)
        else:
            # The value that did not read is where it was, untouched.
            assert isinstance(after, UnreadableRow)
            assert after.unreadable == before.unreadable
            assert returned is not bare
        count = before.values["boundary_generation"]
        if column in PAUSE_READS:
            # A value the pause reads, so it cannot say the boundary is
            # current: a heal a worker holds from before the pause is fenced
            # by the count.
            assert values["boundary_generation"] == count + 1
        else:
            # A value the pause does not read. The boundary is handled as
            # a row that reads has it handled: its digest is over the
            # paused row, and the count stays.
            assert values["boundary_generation"] == count
            assert values["boundary_for"] != before.values["boundary_for"]
        for name in ("name", "task_key", "every_seconds", "cron", "arguments"):
            if name in before.values:
                assert values[name] == before.values[name], name
        if column == "updated_at":
            # Written by every pause and read by none: the value that did
            # not read is replaced without the pause having met it.
            assert not events(caplog, WRITTEN)
            return
        (written,) = events(caplog, WRITTEN)
        assert written.schedule_pk == pk
        assert written.database == ALIAS
        assert written.operation == "paused"
        assert column in written.getMessage()
        assert "paused" in written.getMessage()

    @pytest.mark.parametrize(("vendor", "column", "literal", "repair"), UNREADABLE)
    def test_what_is_handed_back_can_be_deleted(self, vendor, column, literal, repair):
        _, pk = unreadable(vendor, column, literal)
        returned = update_schedule(OxSchedule(pk=pk), enabled=False)
        delete_schedule(returned)
        assert not OxSchedule.objects.filter(pk=pk).exists()

    @pytest.mark.parametrize(("vendor", "column", "literal", "repair"), UNREADABLE)
    def test_a_value_the_pause_does_not_read_is_not_selected_by_it(
        self, vendor, column, literal, repair
    ):
        """
        Under the lock a pause takes the columns it needs and no other, so
        a value that does not read in any other column is not in what the
        database is asked for: it cannot fail the pause, and the pause
        does not go looking for it. Text SQLite's driver cannot decode,
        which fails a read of the whole row and has it read again a column
        at a time, costs a pause nothing either.
        """
        if column in PAUSE_READS:
            pytest.skip(f"a pause reads {column}")
        _, pk = unreadable(vendor, column, literal)

        with CaptureQueriesContext(connection) as queries:
            returned = update_schedule(OxSchedule(pk=pk), enabled=False)

        assert returned.enabled is False
        under_the_lock = locked_read(queries)
        assert column not in columns_read(under_the_lock)
        assert columns_read(under_the_lock) == PAUSE_READS

    def test_a_field_left_deferred_still_reads_as_django_reads_it(self):
        # What the pause hands back holds the fields that read. The others
        # are deferred, and reading one is Django's own read of it.
        only_on("postgresql", "a timestamp psycopg cannot load")
        row = a_row()
        set_column(row.pk, "end_time", "'infinity'")
        returned = update_schedule(OxSchedule(pk=row.pk), enabled=False)
        assert "end_time" in returned.get_deferred_fields()
        with pytest.raises(DatabaseError), transaction.atomic():
            returned.end_time  # noqa: B018 - the read is the assertion

    def test_an_enabled_that_does_not_read_moves_the_boundary(self):
        # Whether the boundary was current depends on what `enabled` held,
        # which is not known, so it is moved as a stale one is. Here the
        # digest is the one a paused row has, which SQLite's own reading of
        # 2 as false would take for current.
        if connection.vendor == "postgresql":
            pytest.skip("PostgreSQL's boolean column holds only true and false")
        row = a_row()
        paused = OxSchedule.objects.get(pk=row.pk)
        paused.enabled = False
        OxSchedule.objects.filter(pk=row.pk).update(
            boundary_for=boundary_digest(paused)
        )
        set_column(row.pk, "enabled", "2")
        started = timezone.now()
        update_schedule(OxSchedule(pk=row.pk), enabled=False)
        after = OxSchedule.objects.get(pk=row.pk)
        assert after.enabled is False
        assert after.start_time >= started
        assert after.boundary_generation == row.boundary_generation + 1
        assert after.boundary_for == boundary_digest(after)

    @pytest.mark.parametrize(
        ("column", "literal"),
        [
            pytest.param("every_seconds", "'abc'", id="interval-text"),
            pytest.param("boundary_generation", "'abc'", id="count-text"),
        ],
    )
    def test_a_boundary_it_cannot_move_and_count_is_left_where_it_is(
        self, column, literal
    ):
        # A digest of timing that did not read would be made up, and a
        # count that did not read cannot be moved on from what it was. The
        # row is retimed around the write functions first, so its boundary
        # is stale and would otherwise be moved.
        only_on("sqlite", "text in an integer column")
        row = a_row()
        OxSchedule.objects.filter(pk=row.pk).update(every_seconds=120)
        set_column(row.pk, column, literal)
        before = read(row.pk)
        update_schedule(OxSchedule(pk=row.pk), enabled=False)
        after = read(row.pk)
        assert after.values["enabled"] is False
        assert after.values["boundary_for"] == before.values["boundary_for"]
        assert after.values["start_time"] == before.values["start_time"]
        assert after.unreadable == before.unreadable


class TestRepairingByReplacingWhatDoesNotRead:
    @pytest.mark.parametrize(("vendor", "column", "literal", "repair"), UNREADABLE)
    def test_the_row_reads_again_and_starts_from_now(
        self, vendor, column, literal, repair, caplog, committed
    ):
        row, pk = unreadable(vendor, column, literal)
        changed = marker()
        bare = OxSchedule(pk=pk)
        started = timezone.now()
        with caplog.at_level(logging.INFO, logger="django_ox"), committed():
            returned = update_schedule(bare, **repair)
        assert returned is bare
        after = read(pk)
        assert isinstance(after, OxSchedule), after
        # Django reads it again, and it validates.
        assert OxSchedule.objects.get(pk=pk).pk == pk
        validate_schedule(after)
        for name, value in repair.items():
            assert getattr(after, name) == value
            assert getattr(returned, name) == value
        # The values it held before are not known, so it starts afresh, as
        # a retime does: nothing it may have owed while it did not read
        # fires now.
        assert after.start_time >= started
        assert after.boundary_generation == row.boundary_generation + 1
        assert after.boundary_for == boundary_digest(after)
        assert marker() > changed
        # Everything else is as it was written.
        assert as_it_was(after, column, *repair) == as_it_was(row, column, *repair)
        (written,) = events(caplog, WRITTEN)
        assert written.schedule_pk == pk
        assert written.operation == "changed"
        assert "changed" in written.getMessage()
        assert set(repair) <= set(written.written_fields)
        assert not hasattr(written, "fields")

    @pytest.mark.parametrize(("vendor", "column", "literal", "repair"), UNREADABLE)
    def test_a_change_that_leaves_it_in_place_is_refused_with_its_name(
        self, vendor, column, literal, repair
    ):
        if column in ("start_time", "updated_at"):
            pytest.skip(f"any change sets {column} for itself")
        _, pk = unreadable(vendor, column, literal)
        before = read(pk)
        changed = marker()
        bystander = (
            {"starting_deadline_seconds": 30}
            if column == "name"
            else {"name": "renamed"}
        )
        with pytest.raises(StoredValueUnreadable) as caught:
            update_schedule(OxSchedule(pk=pk), **bystander)
        exc = caught.value
        assert not isinstance(exc, DatabaseError)
        assert (exc.alias, exc.model, exc.pk, exc.fields) == (
            ALIAS,
            "django_ox.OxSchedule",
            pk,
            (column,),
        )
        assert column in exc.reason
        assert exc.__cause__ is not None
        assert any(column in note for note in exc.__notes__)
        # Nothing was written: not the change, not the value Django's read
        # would have put in its place.
        after = read(pk)
        assert after.unreadable == before.unreadable
        assert after.values == before.values
        assert marker() == changed

    @pytest.mark.parametrize(
        ("vendor", "column", "literal"),
        [
            pytest.param(
                "sqlite", "boundary_generation", "'abc'", id="sqlite-count-text"
            ),
            pytest.param(
                "sqlite",
                "created_at",
                "'2026-02-30 00:00:00'",
                id="sqlite-created-does-not-exist",
            ),
            pytest.param(
                "mysql",
                "created_at",
                "'0000-00-00 00:00:00'",
                id="mysql-created-zero-date",
            ),
        ],
    )
    def test_a_value_only_this_module_writes_is_not_guessed(
        self, vendor, column, literal
    ):
        # No caller can pass these, and this module cannot write them again
        # without the value they hold: the count of boundary writes is moved
        # on from what it was, and the creation time is when it was. The row
        # can be paused and deleted, and is not changed.
        _, pk = unreadable(vendor, column, literal)
        before = read(pk)
        with pytest.raises(StoredValueUnreadable) as caught:
            update_schedule(OxSchedule(pk=pk), enabled=True, end_time=None)
        assert caught.value.fields == (column,)
        assert any("delete_schedule" in note for note in caught.value.__notes__)
        assert read(pk).values == before.values
        update_schedule(OxSchedule(pk=pk), enabled=False)
        assert read(pk).values["enabled"] is False
        delete_schedule(OxSchedule(pk=pk))
        assert not OxSchedule.objects.filter(pk=pk).exists()

    def test_several_values_are_replaced_in_one_call(self):
        only_on("sqlite", "text in integer columns")
        row = a_row()
        set_column(row.pk, "every_seconds", "'abc'")
        set_column(row.pk, "phase_seconds", "'abc'")
        with pytest.raises(StoredValueUnreadable) as caught:
            update_schedule(OxSchedule(pk=row.pk), every_seconds=90)
        assert caught.value.fields == ("phase_seconds",)
        update_schedule(OxSchedule(pk=row.pk), every_seconds=90, phase_seconds=5)
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.every_seconds, after.phase_seconds) == (90, 5)

    def test_the_admin_s_whole_form_repairs_it(self):
        # The change form submits every field it shows, the one that did
        # not read among them.
        only_on("sqlite", "text in an integer column")
        row = a_row()
        set_column(row.pk, "every_seconds", "'abc'")
        submitted = {
            "name": row.name,
            "task_key": row.task_key,
            "trigger": "interval",
            "cron": "",
            "every_seconds": 60,
            "phase_seconds": 0,
            "arguments": {},
            "enabled": True,
            "end_time": None,
            "starting_deadline_seconds": 600,
        }
        update_schedule(OxSchedule(pk=row.pk), **submitted)
        assert OxSchedule.objects.get(pk=row.pk).every_seconds == 60


class TestTheCallersOwnValues:
    """
    A value given to `update_schedule` that its field refuses is refused
    against that field, with the field's own code and params, before it is
    compared with anything, and nothing is written.
    """

    @pytest.mark.parametrize("field", ["every_seconds", "phase_seconds"])
    def test_text_where_a_number_goes_is_refused_against_its_field(self, field):
        row = a_row()
        written = OxSchedule.objects.get(pk=row.pk)
        changed = marker()
        with pytest.raises(ValidationError) as caught:
            update_schedule(row, **{field: "abc"})
        assert caught.value.message_dict == {field: ["“abc” value must be an integer."]}
        (error,) = caught.value.error_dict[field]
        assert (error.code, error.params) == ("invalid", {"value": "abc"})
        after = OxSchedule.objects.get(pk=row.pk)
        assert as_it_was(after) == as_it_was(written)
        assert (after.updated_at, after.start_time) == (
            written.updated_at,
            written.start_time,
        )
        assert marker() == changed

    def test_beside_a_rule_the_row_breaks_both_are_said(self):
        row = a_row()
        with pytest.raises(ValidationError) as caught:
            update_schedule(row, every_seconds="abc", cron="* * * * *")
        assert set(caught.value.message_dict) == {"every_seconds", "cron"}

    def test_a_replacement_its_field_refuses_is_refused_and_nothing_written(self):
        row = a_row()
        pk = an_unreadable_end(row)
        before = read(pk)
        changed = marker()
        with pytest.raises(ValidationError) as caught:
            update_schedule(OxSchedule(pk=pk), end_time="soon")
        assert list(caught.value.message_dict) == ["end_time"]
        assert [e.code for e in caught.value.error_dict["end_time"]] == ["invalid"]
        assert read(pk).unreadable == before.unreadable
        assert marker() == changed

    def test_text_replacing_text_is_refused_against_its_field(self):
        only_on("sqlite", "text in an integer column")
        row = a_row()
        set_column(row.pk, "every_seconds", "'abc'")
        with pytest.raises(ValidationError) as caught:
            update_schedule(OxSchedule(pk=row.pk), every_seconds="xyz")
        assert caught.value.message_dict == {
            "every_seconds": ["“xyz” value must be an integer."]
        }
        assert read(row.pk).unreadable["every_seconds"]


class TestOnlyTheRowAsItIsNowIsWritten:
    def test_a_stale_instance_is_used_for_its_key_alone(self):
        row = a_row()
        stale = OxSchedule.objects.get(pk=row.pk)
        OxSchedule.objects.filter(pk=row.pk).update(
            name="renamed-since", starting_deadline_seconds=45
        )
        pk = an_unreadable_end(row)
        returned = update_schedule(stale, end_time=None)
        assert returned is stale
        after = OxSchedule.objects.get(pk=pk)
        assert (after.name, after.starting_deadline_seconds, after.end_time) == (
            "renamed-since",
            45,
            None,
        )
        assert (stale.name, stale.starting_deadline_seconds) == ("renamed-since", 45)

    def test_a_bare_instance_writes_none_of_its_defaults(self):
        row = a_row(cron="", arguments={"label": "kept"})
        pk = an_unreadable_end(row)
        update_schedule(OxSchedule(pk=pk), end_time=None)
        after = OxSchedule.objects.get(pk=pk)
        assert as_it_was(after, "end_time") == as_it_was(row, "end_time")

    def test_a_row_that_is_gone_says_so(self):
        row = a_row()
        OxSchedule.objects.filter(pk=row.pk).delete()
        with pytest.raises(OxSchedule.DoesNotExist):
            update_schedule(OxSchedule(pk=row.pk), end_time=None)
        with pytest.raises(OxSchedule.DoesNotExist):
            update_schedule(OxSchedule(pk=row.pk), enabled=False)


class TestAWorkersPendingHealIsFenced:
    """
    A worker that met a row's boundary stale records the boundary it saw,
    and heals it on a later pass only if nothing has written the boundary
    since. A pause or a repair of a row that did not read writes the count
    of boundary writes, so a heal from before it stands down, even when the
    clock and the digest the write lands on are the ones the worker saw.
    """

    @pytest.fixture
    def frozen(self, monkeypatch):
        fixed = timezone.now().replace(microsecond=0)
        monkeypatch.setattr(timezone, "now", lambda: fixed)
        return fixed

    def _sighting(self, row, frozen):
        """A row a worker has met stale, at a boundary of `frozen`."""
        OxSchedule.objects.filter(pk=row.pk).update(start_time=frozen)
        source = DatabaseScheduleSource({}, ALIAS)
        source._build()
        assert row.pk in source._needs_heal
        return source

    def test_a_repair_fences_it(self, frozen, caplog, committed):
        row = a_row(trigger="cron", cron="0 3 * * *", every_seconds=None)
        # Retimed around the write functions, so the boundary is stale.
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 4 * * *")
        source = self._sighting(row, frozen)
        observed = source._needs_heal[row.pk]
        pk = an_unreadable_end(row)
        # Back onto the timing the boundary was set for, at the instant the
        # worker saw it: only the count tells the two writes apart.
        update_schedule(OxSchedule(pk=pk), end_time=None, cron="0 3 * * *")
        after = OxSchedule.objects.get(pk=pk)
        assert (after.boundary_for, after.start_time) == observed[:2]
        assert after.boundary_generation == observed[2] + 1
        with caplog.at_level(logging.INFO, logger="django_ox"), committed():
            source._heal()
        assert not events(caplog, "schedule_boundary_healed")
        assert row.pk not in source._needs_heal, "the sighting was not settled"
        assert OxSchedule.objects.get(pk=pk).boundary_generation == observed[2] + 1

    def test_a_pause_fences_it(self, frozen, caplog, committed):
        if connection.vendor == "postgresql":
            pytest.skip(
                "PostgreSQL's boolean column holds only true and false, and a "
                "paused row whose other values do not read cannot be healed at all"
            )
        row = a_row()
        paused = OxSchedule.objects.get(pk=row.pk)
        paused.enabled = False
        # Paused through the write functions and resumed around them: the
        # boundary is over the paused row, and is stale for a running one.
        OxSchedule.objects.filter(pk=row.pk).update(
            boundary_for=boundary_digest(paused)
        )
        source = self._sighting(row, frozen)
        observed = source._needs_heal[row.pk]
        set_column(row.pk, "enabled", "2")
        assert isinstance(read(row.pk), UnreadableRow)
        update_schedule(OxSchedule(pk=row.pk), enabled=False)
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.boundary_for, after.start_time) == observed[:2]
        assert after.boundary_generation == observed[2] + 1
        with caplog.at_level(logging.INFO, logger="django_ox"), committed():
            source._heal()
        assert not events(caplog, "schedule_boundary_healed")
        assert row.pk not in source._needs_heal, "the sighting was not settled"
        assert OxSchedule.objects.get(pk=row.pk).boundary_generation == observed[2] + 1

    def test_without_the_count_the_heal_would_have_gone_ahead(
        self, frozen, caplog, committed
    ):
        # The control: the same sighting, the boundary written back to what
        # the worker saw with the count left alone, and the heal runs.
        row = a_row(trigger="cron", cron="0 3 * * *", every_seconds=None)
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 4 * * *")
        source = self._sighting(row, frozen)
        observed = source._needs_heal[row.pk]
        OxSchedule.objects.filter(pk=row.pk).update(cron="0 3 * * *")
        with caplog.at_level(logging.INFO, logger="django_ox"), committed():
            source._heal()
        assert len(events(caplog, "schedule_boundary_healed")) == 1
        assert OxSchedule.objects.get(pk=row.pk).boundary_generation == observed[2] + 1


class TestTheRegistryPermission:
    @pytest.fixture
    def staff(self):
        return User.objects.create_user(
            "staff", "staff@example.com", "pw", is_staff=True
        )

    def _granted(self, user):
        user.user_permissions.add(Permission.objects.get(codename="view_user"))
        return User.objects.get(pk=user.pk)  # drop the permission cache

    def test_without_it_nothing_is_paused_or_changed(self, staff):
        row = a_row(task_key="guarded")
        pk = an_unreadable_end(row)
        before = read(pk)
        changed = marker()
        with pytest.raises(PermissionDenied):
            update_schedule(OxSchedule(pk=pk), user=staff, enabled=False)
        with pytest.raises(PermissionDenied):
            update_schedule(OxSchedule(pk=pk), user=staff, end_time=None)
        assert read(pk).values == before.values
        assert marker() == changed

    def test_with_it_the_row_is_paused_and_repaired(self, staff):
        row = a_row(task_key="guarded")
        pk = an_unreadable_end(row)
        staff = self._granted(staff)
        update_schedule(OxSchedule(pk=pk), user=staff, enabled=False)
        update_schedule(OxSchedule(pk=pk), user=staff, end_time=None, enabled=True)
        after = OxSchedule.objects.get(pk=pk)
        assert (after.enabled, after.end_time) == (True, None)

    @pytest.mark.parametrize("change", [{"enabled": False}, {"every_seconds": 90}])
    def test_a_key_written_as_text_names_the_same_row(self, staff, change):
        # `OxSchedule(pk=...)` is how a caller names a row, and a key from
        # a URL or a form is text.
        row = a_row(task_key="guarded")
        before = as_it_was(OxSchedule.objects.get(pk=row.pk))
        with pytest.raises(PermissionDenied):
            update_schedule(OxSchedule(pk=str(row.pk)), user=staff, **change)
        assert as_it_was(OxSchedule.objects.get(pk=row.pk)) == before

        after = update_schedule(OxSchedule(pk=str(row.pk)), **change)

        ((field, value),) = change.items()
        assert getattr(after, field) == value
        assert getattr(OxSchedule.objects.get(pk=row.pk), field) == value

    def test_a_new_key_does_not_authorize_changing_the_old_one(self, staff):
        # The row as it stands is what is asked about, and then the row as
        # it will be: a key without a permission does not let anyone take
        # over a row that has one.
        row = a_row(task_key="guarded")
        with pytest.raises(PermissionDenied):
            update_schedule(row, user=staff, task_key="report")
        assert OxSchedule.objects.get(pk=row.pk).task_key == "guarded"

    @pytest.mark.parametrize(
        "change",
        [
            {"every_seconds": 5},
            {"name": "renamed"},
            {"enabled": False},
            {"task_key": "report"},
        ],
        ids=["retime", "rename", "pause", "the-same-key-as-a-change"],
    )
    def test_a_key_changed_on_the_callers_instance_authorizes_nothing(
        self, staff, change
    ):
        """
        The row as the database holds it is what is asked about, read
        under its lock. An instance whose task key was set in memory to a
        key anyone may schedule is still an instance of a row that has a
        permission, whatever the call goes on to change.
        """
        row = a_row(task_key="guarded")
        changed = marker()
        mine = OxSchedule.objects.get(pk=row.pk)
        mine.task_key = "report"
        with pytest.raises(PermissionDenied):
            update_schedule(mine, user=staff, **change)
        after = OxSchedule.objects.get(pk=row.pk)
        assert as_it_was(after) == as_it_was(row)
        assert marker() == changed

    def test_nor_does_one_refuse_a_row_anyone_may_schedule(self, staff):
        # The other way about: the instance names the key that has a
        # permission, and the row does not.
        row = a_row()
        mine = OxSchedule.objects.get(pk=row.pk)
        mine.task_key = "guarded"
        update_schedule(mine, user=staff, starting_deadline_seconds=90)
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.task_key, after.starting_deadline_seconds) == ("report", 90)

    def test_a_row_is_not_moved_to_a_key_the_user_may_not_schedule(self, staff):
        row = a_row()
        changed = marker()
        with pytest.raises(PermissionDenied):
            update_schedule(row, user=staff, task_key="guarded")
        assert OxSchedule.objects.get(pk=row.pk).task_key == "report"
        assert marker() == changed

    def test_a_task_key_that_does_not_read_takes_a_superuser(self, staff):
        only_on("sqlite", "text that is not UTF-8")
        _, pk = unreadable("sqlite", "task_key", NOT_UTF_8)
        staff = self._granted(staff)
        for change in ({"enabled": False}, {"task_key": "report"}):
            with pytest.raises(PermissionDenied) as caught:
                update_schedule(OxSchedule(pk=pk), user=staff, **change)
            assert "task key cannot be read" in str(caught.value)
        assert read(pk).values["enabled"] is True
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        update_schedule(OxSchedule(pk=pk), user=root, enabled=False)
        assert read(pk).values["enabled"] is False
        # Code that names no user stands where it always has.
        update_schedule(OxSchedule(pk=pk), task_key="report", enabled=True)
        after = OxSchedule.objects.get(pk=pk)
        assert (after.task_key, after.enabled) == ("report", True)


class TestDeletingForAUser:
    """
    `delete_schedule(schedule, user=...)` asks the permission of the
    registry entry the row names in the database, as a change is asked it
    of the row as it stands. Without `user` it asks nothing and reads
    nothing of the row, as it always has.
    """

    @pytest.fixture
    def staff(self):
        return User.objects.create_user(
            "staff", "staff@example.com", "pw", is_staff=True
        )

    def _granted(self, user):
        user.user_permissions.add(Permission.objects.get(codename="view_user"))
        return User.objects.get(pk=user.pk)  # drop the permission cache

    def test_without_the_permission_the_row_stays(self, staff):
        row = a_row(task_key="guarded")
        changed = marker()
        with pytest.raises(PermissionDenied) as caught:
            delete_schedule(row, user=staff)
        assert "'auth.view_user'" in str(caught.value)
        with pytest.raises(PermissionDenied):
            delete_schedule(OxSchedule(pk=row.pk), user=staff)
        assert as_it_was(OxSchedule.objects.get(pk=row.pk)) == as_it_was(row)
        assert marker() == changed

    def test_with_it_the_row_goes_and_the_workers_are_told(self, staff):
        row = a_row(task_key="guarded")
        changed = marker()
        delete_schedule(OxSchedule(pk=row.pk), user=self._granted(staff))
        assert not OxSchedule.objects.filter(pk=row.pk).exists()
        assert marker() > changed

    def test_a_row_anyone_may_schedule_goes_for_anyone(self, staff):
        row = a_row()
        delete_schedule(row, user=staff)
        assert not OxSchedule.objects.exists()

    def test_a_key_changed_on_the_callers_instance_authorizes_nothing(self, staff):
        row = a_row(task_key="guarded")
        mine = OxSchedule.objects.get(pk=row.pk)
        mine.task_key = "report"
        with pytest.raises(PermissionDenied):
            delete_schedule(mine, user=staff)
        assert OxSchedule.objects.get(pk=row.pk).task_key == "guarded"

    def test_nor_does_one_refuse_a_row_anyone_may_schedule(self, staff):
        row = a_row()
        mine = OxSchedule.objects.get(pk=row.pk)
        mine.task_key = "guarded"
        delete_schedule(mine, user=staff)
        assert not OxSchedule.objects.exists()

    @pytest.mark.parametrize(
        "written", [str, lambda pk: f" {pk} ", float], ids=["text", "padded", "float"]
    )
    def test_a_key_written_as_something_other_than_its_number_is_asked_the_same(
        self, staff, written
    ):
        # As a URL or a form hands a key over, and as Django's delete takes
        # it. A row that such a key did not find would be deleted unasked.
        row = a_row(task_key="guarded")
        changed = marker()
        with pytest.raises(PermissionDenied):
            delete_schedule(OxSchedule(pk=written(row.pk)), user=staff)
        assert as_it_was(OxSchedule.objects.get(pk=row.pk)) == as_it_was(row)
        assert marker() == changed

        delete_schedule(OxSchedule(pk=written(row.pk)), user=self._granted(staff))

        assert not OxSchedule.objects.filter(pk=row.pk).exists()

    @pytest.mark.parametrize(("vendor", "column", "literal", "repair"), UNREADABLE)
    def test_a_row_holding_a_value_that_does_not_read_is_asked_the_same(
        self, staff, vendor, column, literal, repair
    ):
        if column == "task_key":
            pytest.skip("a task key that does not read names no entry")
        _, pk = unreadable(vendor, column, literal, task_key="guarded")
        with pytest.raises(PermissionDenied):
            delete_schedule(OxSchedule(pk=pk), user=staff)
        assert OxSchedule.objects.filter(pk=pk).count() == 1

        with CaptureQueriesContext(connection) as queries:
            delete_schedule(OxSchedule(pk=pk), user=self._granted(staff))

        assert not OxSchedule.objects.filter(pk=pk).exists()
        # Asked by the task key alone: the value that does not read is in
        # a column the delete never takes.
        assert columns_read(locked_read(queries)) == {"id", "task_key"}

    def test_a_task_key_that_does_not_read_takes_a_superuser(self, staff):
        only_on("sqlite", "text that is not UTF-8")
        _, pk = unreadable("sqlite", "task_key", NOT_UTF_8)
        retired = User.objects.create_superuser("retired", "r@example.com", "pw")
        retired.is_active = False
        retired.save()
        for user in (self._granted(staff), retired):
            with pytest.raises(PermissionDenied) as caught:
                delete_schedule(OxSchedule(pk=pk), user=user)
            assert "task key cannot be read" in str(caught.value)
        assert OxSchedule.objects.filter(pk=pk).count() == 1
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        delete_schedule(OxSchedule(pk=pk), user=root)
        assert not OxSchedule.objects.filter(pk=pk).exists()

    def test_without_a_user_a_task_key_that_does_not_read_is_no_obstacle(self):
        only_on("sqlite", "text that is not UTF-8")
        _, pk = unreadable("sqlite", "task_key", NOT_UTF_8)
        delete_schedule(OxSchedule(pk=pk))
        assert not OxSchedule.objects.filter(pk=pk).exists()

    def test_a_row_that_is_already_gone_has_nothing_to_ask(self, staff):
        row = a_row(task_key="guarded")
        OxSchedule.objects.filter(pk=row.pk).delete()
        # As without a user: no row, and no error.
        delete_schedule(OxSchedule(pk=row.pk), user=staff)
        delete_schedule(OxSchedule(pk=row.pk))
        assert not OxSchedule.objects.exists()

    def test_without_a_user_nothing_of_the_row_is_read(self):
        row = a_row(task_key="guarded")
        pk = an_unreadable_end(row)

        with CaptureQueriesContext(connection) as queries:
            delete_schedule(OxSchedule(pk=pk))

        sent = [sql.split()[0].upper() for sql in on_the_schedule_table(queries)]
        assert sent == ["DELETE"]
        assert not OxSchedule.objects.filter(pk=pk).exists()

    def test_for_a_user_the_task_key_is_read_under_the_lock_and_nothing_else(
        self, staff
    ):
        row = a_row()

        with CaptureQueriesContext(connection) as queries:
            delete_schedule(row, user=staff)

        assert columns_read(locked_read(queries)) == {"id", "task_key"}
        sent = [sql.split()[0].upper() for sql in on_the_schedule_table(queries)]
        if connection.features.has_select_for_update:
            assert sent == ["SELECT", "DELETE"]
        else:
            assert sent == ["UPDATE", "SELECT", "DELETE"]

    def test_a_refusal_inside_a_callers_transaction_leaves_it_usable(self, staff):
        healthy = a_row("healthy")
        guarded = a_row(task_key="guarded")
        with transaction.atomic():
            update_schedule(healthy, starting_deadline_seconds=90)
            with pytest.raises(PermissionDenied):
                delete_schedule(guarded, user=staff)
            assert OxSchedule.objects.filter(pk=guarded.pk).exists()
        assert OxSchedule.objects.get(pk=healthy.pk).starting_deadline_seconds == 90
        assert OxSchedule.objects.filter(pk=guarded.pk).exists()

    def test_an_object_level_backend_is_handed_the_row_as_the_database_holds_it(
        self,
    ):
        row = a_row(task_key="guarded")
        mine = OxSchedule.objects.get(pk=row.pk)
        mine.task_key = "report"
        asked = []

        class _ByObject:
            def has_perm(self, perm, obj=None):
                if obj is not None:
                    asked.append((perm, obj.pk, obj.task_key))
                return obj is not None

        delete_schedule(mine, user=_ByObject())

        assert asked == [("auth.view_user", row.pk, "guarded")]
        assert not OxSchedule.objects.filter(pk=row.pk).exists()


def everything_of(pk):
    """Every column of a stored row, as Django reads it."""
    row = OxSchedule.objects.get(pk=pk)
    fields = OxSchedule._meta.concrete_fields
    return {field.attname: getattr(row, field.attname) for field in fields}


def as_sent(queries):
    """Each statement a call sent: its first word, and whether it takes a lock."""
    return [
        (q["sql"].split()[0].upper(), "FOR UPDATE" in q["sql"])
        for q in queries.captured_queries
    ]


#: What can be done to a stored row through the write functions, each by the
#: row's key alone. `who` is `user=...`, or nothing for a call without one.
WRITES = {
    "pause": lambda pk, **who: update_schedule(OxSchedule(pk=pk), enabled=False, **who),
    "retime": lambda pk, **who: update_schedule(
        OxSchedule(pk=pk), every_seconds=120, **who
    ),
    "rename": lambda pk, **who: update_schedule(
        OxSchedule(pk=pk), name=f"renamed-{pk}", **who
    ),
    "move": lambda pk, **who: update_schedule(
        OxSchedule(pk=pk), task_key="report", **who
    ),
    "delete": lambda pk, **who: delete_schedule(OxSchedule(pk=pk), **who),
}

#: The row's key when it was written: one whose entry carried a permission,
#: and one anyone could schedule.
ONCE_REGISTERED = ["guarded", "nightly"]


class TestARowWhoseTaskKeyIsNotRegisteredHere:
    """
    A stored row names its registry entry by task key, and the entry is
    where a permission is declared. A row whose key this process does not
    register names no entry here: the entry was taken out of the code, or it
    is in newer code than this process runs, as it is for a time during a
    rolling deploy. The permission the row requires is then not known, which
    is not the same as there being none, so with `user` only an active
    superuser may pause, change or delete it, as for a row whose task key
    cannot be read. The row is still there to read. A call made without
    `user` stands where it did, and a change must still end on a key that is
    registered, whoever makes it.
    """

    @pytest.fixture(autouse=True)
    def _another_entry(self):
        register(ScheduleKind(key="nightly", task=tasks.record))

    @pytest.fixture
    def staff(self):
        """
        A user who holds every permission on the schedule table, and the
        one the guarded entry asks for.
        """
        user = User.objects.create_user(
            "staff", "staff@example.com", "pw", is_staff=True
        )
        user.user_permissions.add(
            Permission.objects.get(codename="view_user"),
            *Permission.objects.filter(content_type__model="oxschedule"),
        )
        user = User.objects.get(pk=user.pk)  # drop the permission cache
        assert user.has_perm("auth.view_user")
        assert user.has_perm("django_ox.change_oxschedule")
        assert user.has_perm("django_ox.delete_oxschedule")
        return user

    @pytest.fixture
    def root(self):
        return User.objects.create_superuser("root", "root@example.com", "pw")

    def _orphan(self, key, name="the-row"):
        """A row written while `key` was registered, and the entry then gone."""
        row = a_row(name, task_key=key)
        registry._registry.pop(key)
        return row

    @pytest.mark.parametrize("key", ONCE_REGISTERED)
    @pytest.mark.parametrize("write", WRITES)
    def test_with_a_user_who_is_no_superuser_nothing_is_done_to_it(
        self, staff, write, key
    ):
        row = self._orphan(key)
        before = everything_of(row.pk)
        changed = marker()

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(PermissionDenied) as caught,
        ):
            WRITES[write](row.pk, user=staff)

        said = str(caught.value)
        assert f"Schedule {row.pk}'s task key '{key}' is not registered" in said
        assert "Only a superuser" in said
        # Still there, as it was, and no worker has been told of a change.
        assert everything_of(row.pk) == before
        assert marker() == changed
        # Refused on the read under the row's lock: nothing was sent to the
        # table after it, and nothing to the change marker at all.
        on_the_table = on_the_schedule_table(queries)
        if connection.features.has_select_for_update:
            (under_the_lock,) = on_the_table
            assert "FOR UPDATE" in under_the_lock
        else:
            made_the_writer, under_the_lock = on_the_table
            assert made_the_writer.upper().startswith("UPDATE")
        assert under_the_lock.upper().startswith("SELECT")
        assert not [
            q["sql"]
            for q in queries.captured_queries
            if f"{SCHEDULES}change" in q["sql"]
        ]

    @pytest.mark.parametrize("key", ONCE_REGISTERED)
    @pytest.mark.parametrize("who", ["a superuser", "no user"])
    def test_a_superuser_and_a_call_without_a_user_may(self, root, who, key):
        by = {"user": root} if who == "a superuser" else {}

        paused = self._orphan(key, "to-pause")
        changed = marker()
        WRITES["pause"](paused.pk, **by)
        assert OxSchedule.objects.get(pk=paused.pk).enabled is False
        assert marker() > changed

        register(ScheduleKind(key=key, task=tasks.record))
        moved = self._orphan(key, "to-move")
        WRITES["move"](moved.pk, **by)
        assert OxSchedule.objects.get(pk=moved.pk).task_key == "report"

        register(ScheduleKind(key=key, task=tasks.record))
        deleted = self._orphan(key, "to-delete")
        WRITES["delete"](deleted.pk, **by)
        assert not OxSchedule.objects.filter(pk=deleted.pk).exists()

    @pytest.mark.parametrize("who", ["a superuser", "no user"])
    @pytest.mark.parametrize(
        "change",
        [{"every_seconds": 120}, {"name": "renamed"}, {"task_key": "gone.too"}],
        ids=["retime", "rename", "another-key-not-registered"],
    )
    def test_a_change_must_still_end_on_a_key_that_is_registered(
        self, root, change, who
    ):
        by = {"user": root} if who == "a superuser" else {}
        row = self._orphan("guarded")
        before = everything_of(row.pk)
        changed = marker()

        with pytest.raises(ValidationError) as caught:
            update_schedule(OxSchedule(pk=row.pk), **change, **by)

        assert list(caught.value.message_dict) == ["task_key"]
        assert "is not a schedulable task" in caught.value.message_dict["task_key"][0]
        assert everything_of(row.pk) == before
        assert marker() == changed

    @pytest.mark.parametrize("write", WRITES)
    def test_a_superuser_who_is_not_active_is_refused(self, write):
        retired = User.objects.create_superuser("retired", "r@example.com", "pw")
        retired.is_active = False
        retired.save()
        row = self._orphan("guarded")
        before = everything_of(row.pk)

        with pytest.raises(PermissionDenied) as caught:
            WRITES[write](row.pk, user=retired)

        assert "is not registered" in str(caught.value)
        assert everything_of(row.pk) == before

    @pytest.mark.parametrize("write", ["pause", "rename", "move", "delete"])
    def test_a_key_changed_on_the_callers_instance_authorizes_nothing(
        self, staff, write
    ):
        """
        The key asked about is the one the database holds, read under the
        row's lock. An instance whose task key was set in memory to one
        that is registered, and that anyone may schedule, is still an
        instance of a row whose own key is not.
        """
        row = self._orphan("guarded")
        before = everything_of(row.pk)
        changed = marker()
        mine = OxSchedule.objects.get(pk=row.pk)
        mine.task_key = "report"
        writes = {
            "pause": lambda: update_schedule(mine, user=staff, enabled=False),
            "rename": lambda: update_schedule(mine, user=staff, name="renamed"),
            "move": lambda: update_schedule(mine, user=staff, task_key="report"),
            "delete": lambda: delete_schedule(mine, user=staff),
        }

        with pytest.raises(PermissionDenied) as caught:
            writes[write]()

        assert "task key 'guarded' is not registered" in str(caught.value)
        assert everything_of(row.pk) == before
        assert marker() == changed

    @pytest.mark.parametrize("write", WRITES)
    def test_an_object_level_permission_is_not_asked(self, write):
        row = self._orphan("guarded")
        before = everything_of(row.pk)
        asked = []

        class _ByObject:
            def has_perm(self, perm, obj=None):
                asked.append((perm, obj))
                return True

        with pytest.raises(PermissionDenied):
            WRITES[write](row.pk, user=_ByObject())

        # There is no permission to ask it by: the entry that named one is
        # not known here.
        assert asked == []
        assert everything_of(row.pk) == before

    def test_a_row_that_also_holds_a_value_that_does_not_read_is_asked_the_same(
        self, staff, root
    ):
        # Its task key reads, under the lock, whatever else in the row does
        # not, and the key is all that is asked about.
        pk = an_unreadable_end(self._orphan("guarded"))
        before = read(pk)
        changed = marker()

        for write in ("pause", "delete"):
            with pytest.raises(PermissionDenied) as caught:
                WRITES[write](pk, user=staff)
            assert "task key 'guarded' is not registered" in str(caught.value)
        assert read(pk).values == before.values
        assert marker() == changed

        WRITES["pause"](pk, user=root)
        assert read(pk).values["enabled"] is False
        WRITES["delete"](pk, user=root)
        assert not OxSchedule.objects.filter(pk=pk).exists()

    def test_the_refusal_names_the_key_as_stored_and_safe_to_print(self, staff):
        # A key is whatever was stored. Written by SQL it need never have
        # been registered, and can hold what no key does.
        row = a_row()
        hostile = "\x1b]0;owned\x07\r\nforged line"
        by_sql(
            f"UPDATE {SCHEDULES} SET task_key = %s WHERE id = %s",  # noqa: S608
            [hostile, row.pk],
        )

        with pytest.raises(PermissionDenied) as caught:
            update_schedule(OxSchedule(pk=row.pk), user=staff, enabled=False)

        said = str(caught.value)
        assert said.startswith(f"Schedule {row.pk}'s task key '")
        assert "forged line' is not registered" in said
        assert said.isprintable()
        assert OxSchedule.objects.get(pk=row.pk).enabled is True

    def test_while_the_entry_is_registered_its_permission_is_what_is_asked(self, staff):
        # The same row and the same user, before the entry goes and after.
        row = a_row(task_key="guarded")
        update_schedule(OxSchedule(pk=row.pk), user=staff, starting_deadline_seconds=90)
        registry._registry.pop("guarded")
        with pytest.raises(PermissionDenied):
            update_schedule(
                OxSchedule(pk=row.pk), user=staff, starting_deadline_seconds=30
            )
        # Registered again, as once every process runs the code that has it.
        register(
            ScheduleKind(key="guarded", task=tasks.record, permission="auth.view_user")
        )
        update_schedule(OxSchedule(pk=row.pk), user=staff, starting_deadline_seconds=30)
        assert OxSchedule.objects.get(pk=row.pk).starting_deadline_seconds == 30

    def test_a_row_cannot_be_created_under_or_moved_to_such_a_key(self, staff, root):
        """
        Unchanged, and no question of permission: a key that is not
        registered is refused against its field, for a row to be created
        and for a row to be moved, whoever asks.
        """
        registry._registry.pop("guarded")
        for by in ({}, {"user": staff}, {"user": root}):
            with pytest.raises(ValidationError) as caught:
                a_row("new", task_key="guarded", **by)
            assert list(caught.value.message_dict) == ["task_key"]
        row = a_row()
        for by in ({}, {"user": staff}, {"user": root}):
            with pytest.raises(ValidationError) as caught:
                update_schedule(row, task_key="guarded", **by)
            assert list(caught.value.message_dict) == ["task_key"]
        assert OxSchedule.objects.get(pk=row.pk).task_key == "report"

    @pytest.mark.parametrize("write", ["pause", "move", "delete"])
    def test_for_a_superuser_the_statements_are_those_of_a_registered_row(
        self, root, write
    ):
        """
        Whether the key is registered is asked of this process's registry
        and costs no statement: what is sent for a row whose key is not is
        what is sent for one whose key is.
        """
        registered = a_row("registered", task_key="guarded")
        with CaptureQueriesContext(connection) as queries:
            WRITES[write](registered.pk, user=root)
        expected = as_sent(queries)

        orphan = self._orphan("guarded")
        with CaptureQueriesContext(connection) as queries:
            WRITES[write](orphan.pk, user=root)

        assert as_sent(queries) == expected
        if write == "pause":
            assert columns_read(locked_read(queries)) == PAUSE_READS
        if write == "delete":
            assert columns_read(locked_read(queries)) == {"id", "task_key"}

    @pytest.mark.parametrize("write", ["pause", "rename", "move"])
    def test_asking_a_user_costs_no_statement_of_its_own(self, staff, write):
        """
        For a row whose key is registered, a pause or a change made for a
        user sends what the same call sends without one. The permission is
        asked of the user, here with the user's permissions already loaded.
        """
        without = a_row("without", task_key="guarded")
        with CaptureQueriesContext(connection) as queries:
            WRITES[write](without.pk)
        expected = as_sent(queries)

        asked = a_row("with", task_key="guarded")
        with CaptureQueriesContext(connection) as queries:
            WRITES[write](asked.pk, user=staff)

        assert as_sent(queries) == expected


COUNT = "boundary_generation"


class TestACountOfBoundaryWritesAtItsMaximum:
    """
    A row whose count of boundary writes is the most its column can hold
    reads, as far as Django's own read goes, and cannot be written by
    anything that moves its boundary, since that adds one to the count:
    PostgreSQL and MySQL refuse the write, and SQLite keeps the sum as a
    float. SQL puts the count there, or one below it, from which the next
    change that moves the boundary does. No call here can lower it.

    So the write functions meet the row as one that does not read, by its
    count. A pause stops it and leaves the count alone. A retime, a rename
    or any other change is refused, naming the count, and writes nothing. A
    delete removes it. Whether its boundary is stale or current.
    """

    @pytest.fixture(params=["stale", "current"])
    def full(self, request):
        """
        A valid row whose count SQL then set to the most the column holds:
        its key, and whether its boundary is stale. Stale, it was retimed
        around the write functions first, so that a write here would move
        its boundary if it could.
        """
        row = a_row()
        stale = request.param == "stale"
        if stale:
            OxSchedule.objects.filter(pk=row.pk).update(every_seconds=120)
        set_column(row.pk, COUNT, str(largest_count()))
        return row.pk, stale

    def _count_as_stored(self, pk):
        """The count as the database holds it, and on SQLite how it is kept."""
        kind = f"typeof({COUNT})" if connection.vendor == "sqlite" else "NULL"
        statement = f"SELECT {COUNT}, {kind} FROM {SCHEDULES} WHERE id = %s"  # noqa: S608
        with connection.cursor() as cursor:
            cursor.execute(statement, [pk])
            return cursor.fetchone()

    def _still_full(self, pk):
        whole = "integer" if connection.vendor == "sqlite" else None
        return self._count_as_stored(pk) == (largest_count(), whole)

    def test_django_reads_the_row_and_the_guarded_read_names_its_count(self, full):
        pk, _stale = full

        assert OxSchedule.objects.get(pk=pk).boundary_generation == largest_count()
        found = read(pk)
        assert isinstance(found, UnreadableRow), found
        assert found.fields == (COUNT,)

    def test_a_pause_stops_it_and_leaves_the_count_alone(self, full, caplog, committed):
        pk, stale = full
        before = everything_of(pk)
        changed = marker()

        with (
            caplog.at_level(logging.INFO, logger="django_ox"),
            committed(),
            CaptureQueriesContext(connection) as queries,
        ):
            returned = update_schedule(OxSchedule(pk=pk), enabled=False)

        assert returned.enabled is False
        assert COUNT in returned.get_deferred_fields()
        after = everything_of(pk)
        assert after["enabled"] is False
        assert self._still_full(pk)
        assert after["start_time"] == before["start_time"]
        if stale:
            # A boundary moved without the count moving is one a worker
            # could not tell had been moved, so it stays where it was.
            assert after["boundary_for"] == before["boundary_for"]
        else:
            paused = OxSchedule.objects.get(pk=pk)
            assert after["boundary_for"] == boundary_digest(paused)
        untouched = set(before) - {"enabled", "updated_at", "boundary_for"}
        assert {n: after[n] for n in untouched} == {n: before[n] for n in untouched}
        assert marker() > changed, "the workers were not told"
        # The one write of the pause names the count nowhere.
        (write,) = [
            sql
            for sql in on_the_schedule_table(queries)
            if sql.upper().startswith("UPDATE") and "enabled" in sql
        ]
        assert COUNT not in write
        (written,) = events(caplog, WRITTEN)
        assert (written.schedule_pk, written.operation) == (pk, "paused")
        assert COUNT in written.getMessage()
        assert COUNT not in written.written_fields

    @pytest.mark.parametrize(
        "change",
        [
            {"every_seconds": 300},
            {"name": "renamed"},
            {"enabled": True, "end_time": None},
            {"task_key": "guarded"},
        ],
        ids=["retime", "rename", "resume", "another-task"],
    )
    def test_any_other_change_is_refused_naming_the_count(self, full, change):
        pk, _stale = full
        before = everything_of(pk)
        changed = marker()

        with pytest.raises(StoredValueUnreadable) as caught:
            update_schedule(OxSchedule(pk=pk), **change)

        exc = caught.value
        assert not isinstance(exc, DatabaseError)
        assert (exc.alias, exc.model, exc.pk, exc.fields) == (
            ALIAS,
            "django_ox.OxSchedule",
            pk,
            (COUNT,),
        )
        assert exc.reason == (
            f"{COUNT} holds {largest_count()}, the most its column can hold, so "
            "the count cannot be raised"
        )
        # What can be done instead is said with it: pause it or delete it.
        (note,) = exc.__notes__
        assert COUNT in note
        assert "enabled=False" in note
        assert "delete_schedule" in note
        # Nothing was written, and no worker told.
        assert everything_of(pk) == before
        assert self._still_full(pk)
        assert marker() == changed

    def test_one_below_it_a_retime_takes_the_count_to_it(self):
        """
        SQL can leave the count one below the maximum. The retime that adds
        one succeeds, and the row it leaves is this one: every later change
        is refused, and it can still be paused and deleted.
        """
        row = a_row()
        set_column(row.pk, COUNT, str(largest_count() - 1))

        returned = update_schedule(OxSchedule(pk=row.pk), every_seconds=120)

        assert OxSchedule.objects.filter(pk=row.pk, every_seconds=120).exists()
        assert COUNT in returned.get_deferred_fields()
        assert self._still_full(row.pk)
        for change in ({"every_seconds": 300}, {"name": "renamed"}):
            with pytest.raises(StoredValueUnreadable) as caught:
                update_schedule(OxSchedule(pk=row.pk), **change)
            assert caught.value.fields == (COUNT,)
        assert self._still_full(row.pk)
        update_schedule(OxSchedule(pk=row.pk), enabled=False)
        assert OxSchedule.objects.get(pk=row.pk).enabled is False
        assert self._still_full(row.pk)

    def test_a_delete_removes_it(self, full):
        pk, _stale = full
        changed = marker()

        delete_schedule(OxSchedule(pk=pk))

        assert not OxSchedule.objects.filter(pk=pk).exists()
        assert marker() > changed

    def test_for_a_user_it_is_asked_and_deleted_by_its_task_key_alone(self, full):
        pk, _stale = full
        OxSchedule.objects.filter(pk=pk).update(task_key="guarded")
        staff = User.objects.create_user("staff", "staff@example.com", "pw")

        for write in ("pause", "delete"):
            with pytest.raises(PermissionDenied) as caught:
                WRITES[write](pk, user=staff)
            assert "'auth.view_user'" in str(caught.value)
        assert OxSchedule.objects.get(pk=pk).enabled is True
        assert self._still_full(pk)

        staff.user_permissions.add(Permission.objects.get(codename="view_user"))
        staff = User.objects.get(pk=staff.pk)  # drop the permission cache
        WRITES["pause"](pk, user=staff)
        assert OxSchedule.objects.get(pk=pk).enabled is False
        assert self._still_full(pk)
        with CaptureQueriesContext(connection) as queries:
            WRITES["delete"](pk, user=staff)
        assert not OxSchedule.objects.filter(pk=pk).exists()
        # The delete never takes the count's column.
        assert columns_read(locked_read(queries)) == {"id", "task_key"}

    def test_paused_it_can_still_be_deleted_and_not_resumed(self, full):
        pk, _stale = full
        update_schedule(OxSchedule(pk=pk), enabled=False)

        with pytest.raises(StoredValueUnreadable) as caught:
            update_schedule(OxSchedule(pk=pk), enabled=True)

        assert caught.value.fields == (COUNT,)
        assert OxSchedule.objects.get(pk=pk).enabled is False
        delete_schedule(OxSchedule(pk=pk))
        assert not OxSchedule.objects.filter(pk=pk).exists()

    def test_lowered_by_sql_the_row_takes_a_change_again(self, full):
        pk, _stale = full
        set_column(pk, COUNT, "5")
        started = timezone.now()

        update_schedule(OxSchedule(pk=pk), every_seconds=300)

        after = OxSchedule.objects.get(pk=pk)
        assert after.every_seconds == 300
        assert after.boundary_generation == 6
        assert after.start_time >= started
        assert after.boundary_for == boundary_digest(after)

    def test_one_below_it_a_retime_is_the_last_write_the_count_has_room_for(self):
        row = a_row()
        set_column(row.pk, COUNT, str(largest_count() - 1))

        update_schedule(OxSchedule(pk=row.pk), every_seconds=300)

        assert self._still_full(row.pk)
        assert OxSchedule.objects.get(pk=row.pk).every_seconds == 300
        with pytest.raises(StoredValueUnreadable) as caught:
            update_schedule(OxSchedule(pk=row.pk), every_seconds=600)
        assert caught.value.fields == (COUNT,)
        assert OxSchedule.objects.get(pk=row.pk).every_seconds == 300

    def test_a_worker_reading_it_after_a_pause_says_nothing_and_moves_nothing(
        self, full, caplog
    ):
        pk, _stale = full
        update_schedule(OxSchedule(pk=pk), enabled=False)
        before = everything_of(pk)
        source = DatabaseScheduleSource({}, ALIAS)

        with caplog.at_level(logging.WARNING, logger="django_ox"):
            for _ in range(3):
                source._last_read = None
                assert source.schedules() == []

        assert not caplog.records
        assert not source._needs_heal
        assert everything_of(pk) == before
        assert self._still_full(pk)


class TestInsideACallersTransaction:
    def test_a_refusal_leaves_the_transaction_usable(self):
        healthy = a_row("healthy")
        pk = an_unreadable_end(a_row())
        with transaction.atomic():
            update_schedule(healthy, starting_deadline_seconds=90)
            with pytest.raises(StoredValueUnreadable):
                update_schedule(OxSchedule(pk=pk), name="renamed")
            assert OxSchedule.objects.filter(pk=healthy.pk).exists()
        assert OxSchedule.objects.get(pk=healthy.pk).starting_deadline_seconds == 90

    def test_a_pause_rolled_back_with_it_leaves_no_trace(self, caplog, committed):
        pk = an_unreadable_end(a_row())
        before = read(pk)
        changed = marker()
        with (
            caplog.at_level(logging.INFO, logger="django_ox"),
            committed(),
            pytest.raises(RuntimeError),
            transaction.atomic(),
        ):
            update_schedule(OxSchedule(pk=pk), enabled=False)
            raise RuntimeError("the caller's own failure")
        assert read(pk).values == before.values
        assert marker() == changed
        assert not events(caplog, WRITTEN), "a write that never happened was logged"

    @pytest.mark.parametrize(
        "change", [{"name": "renamed"}, {"enabled": False}], ids=["change", "pause"]
    )
    def test_a_read_the_database_refuses_is_raised_as_it_was(self, change):
        """
        The read of the row under its lock takes no savepoint of its own,
        so one the database itself refuses is not read again more narrowly:
        on PostgreSQL the transaction takes nothing more. The database's
        own error leaves `update_schedule`, never a StoredValueUnreadable
        naming a field, and as it unwinds through the caller's transaction
        it takes the caller's earlier writes with it. On PostgreSQL and
        MySQL the server refuses the read for real; SQLite has no statement
        it refuses that way, and the error is raised in the read's place.
        """
        earlier = a_row("earlier")
        row = a_row()
        # A column a change and a pause both read under the lock.
        refusing = Refusing("ox_stored_enabled")

        with pytest.raises(DataError) as caught, transaction.atomic():
            update_schedule(earlier, starting_deadline_seconds=90)
            with connection.execute_wrapper(refusing):
                update_schedule(OxSchedule(pk=row.pk), **change)

        assert refusing.refused is not None, "the read was never sent"
        assert type(caught.value) is DataError
        assert Refusing.SAYS[connection.vendor] in str(caught.value)
        # Nothing was read after it. All that followed was the write
        # function's own block being rolled back, which inside the caller's
        # transaction is a savepoint.
        assert [sql for sql in refusing.after if "SAVEPOINT" not in sql] == []
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.name, after.enabled) == (row.name, True)
        assert OxSchedule.objects.get(pk=earlier.pk).starting_deadline_seconds == 600

    def test_a_caller_that_catches_it_goes_on_in_its_transaction(self):
        """
        `update_schedule` runs in a block of its own, a savepoint inside a
        caller's transaction. The refused read rolls back to it on the way
        out, so a caller that catches the error still has a transaction,
        and what it wrote before is still there to commit.
        """
        earlier = a_row("earlier")
        row = a_row()
        refusing = Refusing("ox_stored_start_time")

        with transaction.atomic():
            update_schedule(earlier, starting_deadline_seconds=90)
            with pytest.raises(DataError), connection.execute_wrapper(refusing):
                update_schedule(row, name="renamed")
            assert OxSchedule.objects.filter(pk=row.pk).update(cron="") == 1

        assert OxSchedule.objects.get(pk=earlier.pk).starting_deadline_seconds == 90
        assert OxSchedule.objects.get(pk=row.pk).name == row.name


def on_the_schedule_table(queries):
    """The statements among `queries` on the schedule table itself, in order."""
    beside_it = (f"{SCHEDULES}tick", f"{SCHEDULES}change")
    return [
        q["sql"].lstrip()
        for q in queries.captured_queries
        if SCHEDULES in q["sql"].replace(beside_it[0], "").replace(beside_it[1], "")
    ]


def locked_read(queries):
    """
    The statement that read the row under its lock, the first a write
    function sends to the schedule table or on SQLite the second: checked
    for how the lock was taken, and handed back.
    """
    statements = on_the_schedule_table(queries)
    first, second = statements[:2]
    if connection.features.has_select_for_update:
        # The read is what locks, and it is the one statement that does.
        assert first.upper().startswith("SELECT")
        assert [sql for sql in statements if "FOR UPDATE" in sql] == [first]
        return first
    # The no-op write that makes the transaction the writer, and then the
    # row, with no statement between them asking whether it is there.
    assert first.upper().startswith("UPDATE")
    assert second.upper().startswith("SELECT")
    return second


class TestTheLockAndTheReadUnderIt:
    """
    A write function takes the row's lock and reads the row in the fewest
    statements that do both, and sends nothing else to do it: on
    PostgreSQL and MySQL the one locking read, on SQLite the no-op write
    that makes the transaction the writer and then the read. There is no
    savepoint around either and no statement asking whether the row is
    there, which the read answers. A change reads the whole row, which it
    has to validate. A pause reads what a pause needs and no other column.
    """

    EVERY_COLUMN = frozenset(f.column for f in OxSchedule._meta.concrete_fields)

    def test_a_pause_reads_what_it_needs_and_no_other_column(self):
        row = a_row()

        with CaptureQueriesContext(connection) as queries:
            update_schedule(row, enabled=False)

        assert columns_read(locked_read(queries)) == PAUSE_READS
        # All a pause sends to the table: the lock and the read, its one
        # write, and the whole row read back for the caller.
        sent = [sql.split()[0].upper() for sql in on_the_schedule_table(queries)]
        if connection.features.has_select_for_update:
            assert sent == ["SELECT", "UPDATE", "SELECT"]
        else:
            assert sent == ["UPDATE", "SELECT", "UPDATE", "SELECT"]
        back = on_the_schedule_table(queries)[-1]
        assert columns_read(back) == self.EVERY_COLUMN
        assert "FOR UPDATE" not in back

    def test_a_change_reads_the_whole_row(self):
        row = a_row()

        with CaptureQueriesContext(connection) as queries:
            update_schedule(row, name="renamed")

        assert columns_read(locked_read(queries)) == self.EVERY_COLUMN
        on_the_row = [
            sql
            for sql in on_the_schedule_table(queries)
            if "ox_stored_start_time" in sql
        ]
        # The read under the lock, and the read back once the change is in.
        assert len(on_the_row) == 2
        assert "FOR UPDATE" not in on_the_row[1]

    @pytest.mark.parametrize(
        "change", [{"name": "renamed"}, {"enabled": False}], ids=["change", "pause"]
    )
    def test_a_row_that_is_gone(self, change):
        row = a_row()
        OxSchedule.objects.filter(pk=row.pk).delete()

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(OxSchedule.DoesNotExist),
        ):
            update_schedule(OxSchedule(pk=row.pk), **change)

        sent = on_the_schedule_table(queries)
        # The read that found no row, behind the no-op write on SQLite.
        # Nothing asked whether it was there.
        assert len(sent) == (1 if connection.features.has_select_for_update else 2)
        assert sent[-1].upper().startswith("SELECT")
        assert "ox_stored_enabled" in sent[-1]

    @pytest.mark.parametrize(
        "change", [{"name": "renamed"}, {"enabled": False}], ids=["change", "pause"]
    )
    def test_no_savepoint_is_taken_around_either(self, change):
        row = a_row()

        with CaptureQueriesContext(connection) as queries:
            update_schedule(row, **change)

        # From the first statement after the write function's own block
        # opened, which inside this test's transaction is a savepoint.
        sent = [q["sql"].lstrip() for q in queries.captured_queries]
        opened = next(i for i, sql in enumerate(sent) if sql.startswith("SAVEPOINT"))
        after = sent[opened + 1 :]
        if not connection.features.has_select_for_update:
            # The no-op write that makes the transaction the writer.
            assert after[0].upper().startswith("UPDATE")
            after = after[1:]
        under_the_lock, following = after[:2]
        assert under_the_lock.upper().startswith("SELECT")
        assert "ox_stored_enabled" in under_the_lock
        assert "SAVEPOINT" not in following.upper()


@pytest.mark.django_db(transaction=True)
class TestAConcurrentWriter:
    """
    The row is locked by the read of it, so a change or a pause waits for a
    writer holding the lock and then works from what that writer committed,
    on every engine: a locking read on PostgreSQL and MySQL, which reads
    the row as it was committed rather than as the statement first found
    it, and the writer's own lock on SQLite.
    """

    def _held_while(self, pk, write):
        """
        Hold the row's lock on another connection, run `write` under it,
        and commit once `update_schedule` here is waiting for the lock.
        `committing` is set as the last thing before the commit, and `out`
        holds whether the wait was seen and anything the thread raised.
        """
        entered = threading.Event()
        locked = threading.Event()
        committing = threading.Event()
        out = {}
        lock_schedule = stored.lock_schedule

        def entering(key, *, using, fields=None):
            entered.set()
            return lock_schedule(key, using=using, fields=fields)

        def other():
            try:
                with transaction.atomic():
                    lock_schedule(pk, using=ALIAS)
                    write()
                    locked.set()
                    assert entered.wait(timeout=30)
                    out["waited"] = seen_waiting_for_the_lock()
                    committing.set()
            except BaseException as exc:
                out["raised"] = exc
            finally:
                connections[ALIAS].close()

        return other, entering, locked, committing, out

    def test_a_repair_waits_and_validates_what_the_other_wrote(self, monkeypatch):
        row = a_row()
        pk = an_unreadable_end(row)

        def retimed_badly():
            OxSchedule.objects.filter(pk=pk).update(
                trigger="cron", cron="banana", every_seconds=None
            )

        other, entering, locked, committing, out = self._held_while(pk, retimed_badly)
        monkeypatch.setattr(stored, "lock_schedule", entering)
        thread = threading.Thread(target=other)
        thread.start()
        try:
            assert locked.wait(timeout=30)
            with pytest.raises(ValidationError) as caught:
                update_schedule(OxSchedule(pk=pk), end_time=None)
            assert committing.is_set(), "the change did not wait for the lock"
        finally:
            thread.join(timeout=60)
        assert not thread.is_alive()
        assert "raised" not in out, out
        assert out["waited"] is not False, "the change was not seen waiting"
        # Validated as the other writer left it, so its cron is refused,
        # and nothing of this call was written over it.
        assert "cron" in caught.value.message_dict
        found = read(pk)
        assert isinstance(found, UnreadableRow)
        assert found.values["cron"] == "banana"

    def test_a_pause_waits_and_works_from_what_the_other_wrote(self, monkeypatch):
        row = a_row()
        pk = row.pk

        def retimed_around_the_write_functions():
            OxSchedule.objects.filter(pk=pk).update(every_seconds=300)

        other, entering, locked, committing, out = self._held_while(
            pk, retimed_around_the_write_functions
        )
        monkeypatch.setattr(stored, "lock_schedule", entering)
        thread = threading.Thread(target=other)
        thread.start()
        try:
            assert locked.wait(timeout=30)
            started = timezone.now()
            update_schedule(OxSchedule(pk=pk), enabled=False)
            assert committing.is_set(), "the pause did not wait for the lock"
        finally:
            thread.join(timeout=60)
        assert not thread.is_alive()
        assert "raised" not in out, out
        assert out["waited"] is not False, "the pause was not seen waiting"
        after = OxSchedule.objects.get(pk=pk)
        assert (after.enabled, after.every_seconds) == (False, 300)
        # The digest is over the timing the other writer committed, and
        # the boundary, which that write left stale, was moved: read from
        # before the wait, the row would have had neither done to it.
        assert after.boundary_for == boundary_digest(after)
        assert after.start_time >= started
        assert after.boundary_generation == row.boundary_generation + 1

    def test_a_delete_for_a_user_waits_and_is_asked_by_what_the_other_wrote(
        self, monkeypatch
    ):
        row = a_row()
        pk = row.pk
        staff = User.objects.create_user("staff", "staff@example.com", "pw")

        def given_a_key_with_a_permission():
            OxSchedule.objects.filter(pk=pk).update(task_key="guarded")

        other, entering, locked, committing, out = self._held_while(
            pk, given_a_key_with_a_permission
        )
        monkeypatch.setattr(stored, "lock_schedule", entering)
        thread = threading.Thread(target=other)
        thread.start()
        try:
            assert locked.wait(timeout=30)
            # Anyone may delete the row as this connection last saw it.
            with pytest.raises(PermissionDenied):
                delete_schedule(OxSchedule(pk=pk), user=staff)
            assert committing.is_set(), "the delete did not wait for the lock"
        finally:
            thread.join(timeout=60)
        assert not thread.is_alive()
        assert "raised" not in out, out
        assert out["waited"] is not False, "the delete was not seen waiting"
        assert OxSchedule.objects.get(pk=pk).task_key == "guarded"


@pytest.mark.django_db(transaction=True)
class TestAPauseTheRowCannotBeReadBackAfter:
    """
    A pause is committed before the whole row is read back for the caller.
    A read back the database refuses is raised as it is, and the schedule
    stays paused: what the rest of the row holds can no more undo a pause
    than stop one. On PostgreSQL and MySQL the server refuses the read for
    real; SQLite has no such statement, and the error is raised in the
    read's place.
    """

    def _with_the_read_back_refused(self, pk):
        # A column the read back takes and a pause does not.
        refusing = Refusing("ox_stored_start_time")
        with pytest.raises(DataError) as caught, connection.execute_wrapper(refusing):
            update_schedule(OxSchedule(pk=pk), enabled=False)
        refusing.assert_raised_as_it_was(caught)

    def test_the_schedule_stays_paused_and_the_workers_are_told(self):
        row = a_row()
        changed = marker()

        self._with_the_read_back_refused(row.pk)

        assert OxSchedule.objects.get(pk=row.pk).enabled is False
        assert marker() > changed

    def test_a_value_it_met_under_the_lock_is_still_reported(self, caplog):
        if connection.vendor == "postgresql":
            pytest.skip("PostgreSQL's boolean column holds only true and false")
        row = a_row()
        set_column(row.pk, "enabled", "2")

        with caplog.at_level(logging.INFO, logger="django_ox"):
            self._with_the_read_back_refused(row.pk)

        assert OxSchedule.objects.get(pk=row.pk).enabled is False
        (written,) = events(caplog, WRITTEN)
        assert written.schedule_pk == row.pk
        assert "enabled" in written.getMessage()
        assert "paused" in written.getMessage()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("ending", ["commit", "rollback"])
def test_with_autocommit_off_the_pause_is_reported_at_the_callers_commit(
    ending, caplog
):
    """
    A project that ends its transactions by hand has autocommit off and no
    block open around the call. The pause is then the caller's to commit,
    and the line saying that a row that did not read was paused waits for
    that commit, or goes with a rollback, as the write does. Django runs
    what was left for a commit made by hand once autocommit is back on.
    """
    if ending == "rollback" and connection.vendor == "sqlite":
        pytest.skip(
            "with autocommit off on SQLite, Django's block is a savepoint that "
            "opens the transaction, and SQLite commits it when that is released: "
            "nothing is left for the caller to roll back"
        )
    pk = an_unreadable_end(a_row())
    transaction.set_autocommit(False)
    try:
        with caplog.at_level(logging.INFO, logger="django_ox"):
            update_schedule(OxSchedule(pk=pk), enabled=False)
            assert not events(caplog, WRITTEN)
            getattr(transaction, ending)()
            transaction.set_autocommit(True)
    finally:
        if not transaction.get_autocommit():
            transaction.rollback()
            transaction.set_autocommit(True)
    committed = ending == "commit"
    assert len(events(caplog, WRITTEN)) == (1 if committed else 0)
    assert read(pk).values["enabled"] is not committed


class TestWhatAPauseReturnsForAName:
    """
    `update_schedule` returns the row it paused, and a caller that prints
    it, as an interactive prompt does, asks for its name. A name that does
    not read as text is shown as the row's key, and nothing is written for
    it: the pause stays, and the name stays as it was stored.
    """

    NAMES = [
        pytest.param(NOT_UTF_8, id="not-utf-8"),
        pytest.param("X'626c6f622d6e616d65'", id="bytes"),
    ]

    @staticmethod
    def stored_name(pk):
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT hex(name) FROM {SCHEDULES} WHERE id = %s", [pk])  # noqa: S608
            return cursor.fetchone()[0]

    @pytest.mark.parametrize("literal", NAMES)
    def test_the_pause_is_shown_by_the_key_and_the_name_is_left_alone(self, literal):
        only_on("sqlite", "SQLite keeps a name that is not text")
        row = a_row("the-row")
        set_column(row.pk, "name", literal)
        before = self.stored_name(row.pk)

        result = update_schedule(OxSchedule(pk=row.pk), enabled=False)

        assert "name" in result.get_deferred_fields()
        assert str(result) == f"pk {row.pk}"
        assert f"pk {row.pk}" in repr(result)
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT enabled FROM {SCHEDULES} WHERE id = %s", [row.pk])  # noqa: S608
            assert cursor.fetchone() == (0,)
        assert self.stored_name(row.pk) == before

    @pytest.mark.parametrize("literal", NAMES)
    def test_giving_it_a_name_is_what_repairs_it(self, literal):
        only_on("sqlite", "SQLite keeps a name that is not text")
        row = a_row("the-row")
        set_column(row.pk, "name", literal)

        repaired = update_schedule(OxSchedule(pk=row.pk), name="renamed")

        assert str(repaired) == "renamed"
        assert OxSchedule.objects.get(pk=row.pk).name == "renamed"
