"""
Reading the newest tick of each stored schedule on a page.

A page of stored schedules shows each one's last tick. Asked of the whole
tick log as a grouped maximum, that is every tick of every listed schedule
read on PostgreSQL and SQLite, on every view of the page. Asked a schedule
at a time, newest first with a limit of one, it is the end of that
schedule's entries in the index on (schedule_name, scheduled_for), whatever
the history behind it.

`_stored_read.newest_ticks` asks it that way, of every schedule on the page
in one statement, and decodes each tick itself. The database picks the
newest by its own ordering before anything is decoded, so there are three
answers and they are kept apart: no tick, a newest tick that reads, and a
newest tick that does not read. One that does not read is never passed over
for an older one that does.

Every unreadable value below is written by SQL, as only SQL can write it,
in the form the engine that keeps it keeps it. A test for another engine's
value skips and says why.
"""

import math
import re
from datetime import timedelta

import pytest
from django.db import DataError, OperationalError, connection, transaction
from django.test.utils import CaptureQueriesContext

from django_ox import _stored_read
from django_ox._stored_read import TickRead, UnreadableValue, newest_ticks
from django_ox.models import OxSchedule, OxScheduleTick

from .test_stored_read import (
    ALIAS,
    NOT_UTF_8,
    SCHEDULES,
    TICKS,
    UNREADABLE_NEWEST_TICKS,
    UNREADABLE_OLDEST_TICKS,
    Failing,
    Reads,
    Refusing,
    a_row,
    a_tick,
    by_sql,
    insert_tick,
    many_rows,
    now_ish,
    only_on,
    parameter_limit,  # noqa: F401 (a fixture)
    same,
    savepoints,
    selects,
    set_column,
)


def key(row):
    return f"db:{row.pk}"


def django_newest(row):
    """Django's own read of the schedule's newest tick, row and all."""
    return (
        OxScheduleTick.objects.filter(schedule_name=key(row))
        .order_by("-scheduled_for")
        .first()
    )


#: The parameters the statement carries for the prefix of a tick key, as each
#: backend's concatenation takes it, in the statement's two lookups.
PREFIX_PARAMETERS = {"postgresql": 6, "sqlite": 6, "mysql": 2}


@pytest.mark.django_db
class TestTheNewestTickOfEachSchedule:
    def test_answers_a_page_of_schedules_in_one_statement(self):
        now = now_ish()
        rows = [a_row(f"row-{i}") for i in range(5)]
        for i, row in enumerate(rows[:4]):
            for back in range(3):
                a_tick(key(row), now - timedelta(minutes=10 * back + i))
        # A settings-declared schedule's ticks, under a name of its own.
        a_tick("nightly", now)
        pks = [row.pk for row in rows]

        with CaptureQueriesContext(connection) as queries:
            answers = newest_ticks(pks, using=ALIAS)

        assert len(queries.captured_queries) == 1
        assert list(answers) == pks
        for row in rows[:4]:
            newest = django_newest(row)
            answer = answers[row.pk]
            assert answer == TickRead(key(row), at=newest.scheduled_for, pk=newest.pk)
            assert same(answer.at, newest.scheduled_for)
            assert not answer.absent
            assert not answer.unreadable
        assert answers[rows[4].pk] == TickRead(key(rows[4]))
        assert answers[rows[4].pk].absent

    def test_each_lookup_is_newest_first_with_a_limit_of_one(self):
        row = a_row("row")
        a_tick(key(row), now_ish())
        reads = Reads(SCHEDULES)

        with connection.execute_wrapper(reads):
            newest_ticks([row.pk], using=ALIAS)

        ((sql, params),) = reads.sent
        quoted = connection.ops.quote_name
        # Two lookups a schedule: the newest tick's row, and its tick.
        newest_first = (
            rf"ORDER BY \S+{re.escape(quoted('schedule_name'))} DESC, "
            rf"\S+{re.escape(quoted('scheduled_for'))} DESC LIMIT 1\)"
        )
        assert len(re.findall(newest_first, sql)) == 2, sql
        # Of the schedule's own row, its key and nothing else: a value in it
        # that does not read is no concern of this read.
        outer = sql[sql.rindex(" FROM ") :]
        assert quoted(SCHEDULES) in outer
        taken = [
            model_field.column
            for model_field in OxSchedule._meta.concrete_fields
            if f"{quoted(SCHEDULES)}.{quoted(model_field.column)}" in sql
        ]
        assert taken == ["id"]
        # The tick comes back as text, cast in the query.
        as_text = (
            rf"{re.escape(quoted('scheduled_for'))}\)?(::text| AS text\)| AS char\))"
        )
        assert len(re.findall(as_text, sql)) == 1, sql
        assert len(params) == 1 + PREFIX_PARAMETERS[connection.vendor]

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_NEWEST_TICKS)
    @pytest.mark.parametrize("position", ["first", "middle", "last"])
    def test_a_newest_tick_that_does_not_read_is_its_schedules_answer_alone(
        self, vendor, literal, position
    ):
        only_on(vendor, "kept by that engine only")
        now = now_ish()
        rows = [a_row(f"row-{i}") for i in range(5)]
        ticks = {row.pk: a_tick(key(row), now) for row in rows}
        bad = rows[{"first": 0, "middle": 2, "last": 4}[position]]
        pk = insert_tick(key(bad), literal)

        with CaptureQueriesContext(connection) as queries:
            answers = newest_ticks([row.pk for row in rows], using=ALIAS)

        answer = answers[bad.pk]
        assert answer.unreadable
        assert not answer.absent
        # Not the tick an hour of history older that does read.
        assert answer.at is None
        assert answer.pk == pk
        assert answer.key == key(bad)
        assert answer.reason.startswith("scheduled_for holds ")
        assert isinstance(answer.cause, UnreadableValue)
        for row in rows:
            if row is not bad:
                assert answers[row.pk] == TickRead(
                    key(row), at=now, pk=ticks[row.pk].pk
                )
        # The row of the tick came with it: nothing more was asked.
        assert len(queries.captured_queries) == 1

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_OLDEST_TICKS)
    def test_an_only_tick_that_does_not_read_is_not_no_tick(self, vendor, literal):
        only_on(vendor, "kept by that engine only")
        row = a_row("row")
        never = a_row("never")
        pk = insert_tick(key(row), literal)

        answers = newest_ticks([row.pk, never.pk], using=ALIAS)

        assert answers[row.pk].unreadable
        assert not answers[row.pk].absent
        assert answers[row.pk].pk == pk
        assert answers[never.pk].absent

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_OLDEST_TICKS)
    def test_an_old_tick_that_does_not_read_is_not_the_newest(self, vendor, literal):
        only_on(vendor, "kept by that engine only")
        now = now_ish()
        row = a_row("row")
        insert_tick(key(row), literal)
        newest = a_tick(key(row), now)

        answers = newest_ticks([row.pk], using=ALIAS)

        assert answers[row.pk] == TickRead(key(row), at=now, pk=newest.pk)

    def test_a_tick_that_is_null_is_one_that_does_not_read(self):
        only_on(
            "postgresql", "only there can a column be let hold NULL in a transaction"
        )
        now = now_ish()
        row = a_row("row")
        never = a_row("never")
        # Before any tick is written: the table cannot be altered while a
        # row of it waits on its deferred check.
        by_sql(f"ALTER TABLE {TICKS} ALTER COLUMN scheduled_for DROP NOT NULL")
        a_tick(key(row), now)
        pk = insert_tick(key(row), "NULL")

        answers = newest_ticks([row.pk, never.pk], using=ALIAS)

        # PostgreSQL sorts NULL above every time, so it is the newest: not
        # the tick beside it that reads, and not a schedule that never ran.
        answer = answers[row.pk]
        assert answer.unreadable
        assert not answer.absent
        assert answer.pk == pk
        assert answer.reason == "scheduled_for is NULL, and this field needs a value"
        assert answers[never.pk] == TickRead(key(never))

    @pytest.mark.parametrize("position", ["first", "middle", "last"])
    def test_a_tick_the_driver_cannot_hand_over_is_found_by_halving(self, position):
        only_on("sqlite", "SQLite keeps text its driver cannot decode as UTF-8")
        now = now_ish()
        rows = [a_row(f"row-{i}") for i in range(8)]
        ticks = {row.pk: a_tick(key(row), now) for row in rows}
        bad = rows[{"first": 0, "middle": 3, "last": 7}[position]]
        pk = insert_tick(key(bad), NOT_UTF_8)

        # In this test's transaction, and in no savepoint.
        assert connection.in_atomic_block
        with CaptureQueriesContext(connection) as queries:
            answers = newest_ticks([row.pk for row in rows], using=ALIAS)

        assert not savepoints(queries)
        answer = answers[bad.pk]
        assert answer.unreadable
        assert answer.pk == pk
        assert answer.reason.startswith("scheduled_for could not be read")
        assert "UTF-8" in answer.reason
        assert isinstance(answer.cause, OperationalError)
        for row in rows:
            if row is not bad:
                assert answers[row.pk] == TickRead(
                    key(row), at=now, pk=ticks[row.pk].pk
                )
        # The page, each half the schedule was in, the schedule alone, and
        # then its newest tick's row by key.
        halvings = math.ceil(math.log2(len(rows)))
        assert len(selects(queries, TICKS)) <= 2 * halvings + 2

    def test_a_schedule_whose_own_row_does_not_read_still_has_its_tick_read(self):
        column, literal = {
            "sqlite": ("name", NOT_UTF_8),
            "postgresql": ("end_time", "'infinity'"),
            "mysql": ("end_time", "'0000-00-00 00:00:00'"),
        }[connection.vendor]
        now = now_ish()
        row = a_row("row")
        tick = a_tick(key(row), now)
        set_column(row.pk, column, literal)

        answers = newest_ticks([row.pk], using=ALIAS)

        assert answers[row.pk] == TickRead(key(row), at=now, pk=tick.pk)

    def test_a_column_of_the_tick_the_read_does_not_take_is_not_its_concern(self):
        only_on("postgresql", "PostgreSQL keeps year 10000")
        now = now_ish()
        row = a_row("row")
        tick = a_tick(key(row), now)
        by_sql(
            f"UPDATE {TICKS} SET created_at = '10000-01-01 00:00:00+00' WHERE id = %s",  # noqa: S608
            [tick.pk],
        )

        answers = newest_ticks([row.pk], using=ALIAS)

        assert answers[row.pk] == TickRead(key(row), at=now, pk=tick.pk)

    def test_a_schedule_that_is_gone_is_left_out(self):
        now = now_ish()
        row = a_row("row")
        tick = a_tick(key(row), now)
        # Ticks outlive their schedule, and nothing looks them up for one
        # that is not there.
        a_tick("db:987654", now)

        with CaptureQueriesContext(connection) as queries:
            answers = newest_ticks([987654, row.pk, row.pk], using=ALIAS)
            nothing = newest_ticks([], using=ALIAS)

        assert answers == {row.pk: TickRead(key(row), at=now, pk=tick.pk)}
        assert nothing == {}
        assert len(queries.captured_queries) == 1

    def test_a_tick_under_another_schedules_longer_key_is_not_this_ones(self):
        now = now_ish()
        row = a_row("row")
        # A key this one's is the start of, and one it is the end of.
        a_tick(f"{key(row)}0", now)
        a_tick(f"x{key(row)}", now)

        answers = newest_ticks([row.pk], using=ALIAS)

        assert answers[row.pk].absent

    def test_inside_a_transaction_the_transaction_still_works_after(self):
        now = now_ish()
        healthy, bad = a_row("healthy"), a_row("bad")
        a_tick(key(healthy), now)
        literal = {
            "postgresql": "'infinity'",
            "mysql": "'9999-00-00 00:00:00'",
            "sqlite": NOT_UTF_8,
        }[connection.vendor]
        insert_tick(key(bad), literal)

        with transaction.atomic():
            with CaptureQueriesContext(connection) as queries:
                answers = newest_ticks([healthy.pk, bad.pk], using=ALIAS)
            written = OxScheduleTick.objects.filter(schedule_name=key(healthy))
            assert written.update(task=None) == 1

        assert not savepoints(queries)
        assert answers[healthy.pk].at == now
        assert answers[bad.pk].unreadable


def by_the_key_and_then_the_tick(row):
    """Django's own read of the newest tick in the order the lookup asks for."""
    return (
        OxScheduleTick.objects.filter(schedule_name=key(row))
        .order_by("-schedule_name", "-scheduled_for")
        .first()
    )


@pytest.mark.django_db
class TestTheLookupFindsTheTickNewestFirstAloneWouldFind:
    """
    Each lookup is ordered by the schedule's key and then by the tick, both
    descending, under an equality on the key. The key is the same in every
    row the lookup can see, so ordering by it first changes nothing about
    which row comes first: it is there because it is the index's own order,
    which MySQL then reads from its end instead of sorting every tick of
    the key. What is checked here is the answer, against Django's own read
    ordered by the tick alone.
    """

    def test_for_several_ticks_a_key_whichever_was_written_last(self):
        now = now_ish()
        rows = [a_row(f"row-{i}") for i in range(3)]
        # Minutes back, in the order written: the newest is written neither
        # first nor last, and is a different one for each schedule.
        written = ([30, 5, 45, 0, 20], [0, 50, 10, 40], [15, 25, 35, 2, 60, 8])
        for row, backs in zip(rows, written, strict=True):
            for back in backs:
                a_tick(key(row), now - timedelta(minutes=back, seconds=row.pk))

        answers = newest_ticks([row.pk for row in rows], using=ALIAS)

        for row, backs in zip(rows, written, strict=True):
            newest = django_newest(row)
            assert newest.scheduled_for == now - timedelta(
                minutes=min(backs), seconds=row.pk
            )
            assert by_the_key_and_then_the_tick(row).pk == newest.pk
            assert answers[row.pk] == TickRead(
                key(row), at=newest.scheduled_for, pk=newest.pk
            )
            assert same(answers[row.pk].at, newest.scheduled_for)

    def test_for_the_same_tick_times_under_different_keys(self):
        now = now_ish()
        rows = [a_row(f"row-{i}") for i in range(4)]
        # Every schedule ticked at the same instants, as schedules on one
        # timing do, so the newest instant is in the log once a key.
        for back in (20, 0, 10):
            for row in rows:
                a_tick(key(row), now - timedelta(minutes=back))
        # A settings schedule under a name of its own, at that instant too.
        a_tick("nightly", now)

        answers = newest_ticks([row.pk for row in rows], using=ALIAS)

        found = set()
        for row in rows:
            newest = django_newest(row)
            assert newest.scheduled_for == now
            assert newest.schedule_name == key(row)
            assert by_the_key_and_then_the_tick(row).pk == newest.pk
            # Its own row of the log, and not another key's at that instant.
            assert answers[row.pk] == TickRead(key(row), at=now, pk=newest.pk)
            found.add(answers[row.pk].pk)
        assert len(found) == len(rows)

    def test_for_a_key_with_no_tick_beside_keys_with_newer_ones(self):
        now = now_ish()
        never, ticked = a_row("never"), a_row("ticked")
        older = a_tick(key(ticked), now - timedelta(hours=1))
        # Keys that sort on either side of both schedules', each with a tick
        # newer than any of theirs: one a key is the start of, one it is the
        # end of, the bare prefix, and names from either end of the alphabet.
        for other in (
            f"{key(never)}0",
            f"{key(ticked)}0",
            f"0{key(never)}",
            "db:",
            "aaa",
            "zzz",
        ):
            a_tick(other, now)

        answers = newest_ticks([never.pk, ticked.pk], using=ALIAS)

        assert django_newest(never) is None
        assert by_the_key_and_then_the_tick(never) is None
        assert answers[never.pk] == TickRead(key(never))
        assert answers[never.pk].absent
        assert django_newest(ticked).pk == older.pk
        assert answers[ticked.pk] == TickRead(
            key(ticked), at=older.scheduled_for, pk=older.pk
        )


@pytest.mark.django_db
class TestReadingAPageInBatches:
    def test_five_hundred_schedules_a_statement(self):
        pks = many_rows(1200)
        reads = Reads(TICKS)

        with connection.execute_wrapper(reads):
            answers = newest_ticks(pks, using=ALIAS)

        prefix = PREFIX_PARAMETERS[connection.vendor]
        assert reads.parameters == [500 + prefix, 500 + prefix, 200 + prefix]
        assert list(answers) == pks
        assert all(answer.absent for answer in answers.values())

    def test_a_connection_that_takes_fewer_parameters_is_read_in_smaller_batches(
        self,
        parameter_limit,  # noqa: F811 (the fixture, by its name)
    ):
        now = now_ish()
        pks = many_rows(60)
        for pk in pks:
            a_tick(f"db:{pk}", now)
        # On SQLite the connection's own limit too, so a statement that
        # carried more would be refused and not only counted.
        parameter_limit(25)
        reads = Reads(TICKS)

        with connection.execute_wrapper(reads):
            answers = newest_ticks(pks, using=ALIAS)

        assert all(count <= 25 for count in reads.parameters), reads.parameters
        # Room is kept for the prefix's parameters, whatever this backend
        # takes for them, and the rest is schedules.
        assert PREFIX_PARAMETERS[connection.vendor] <= _stored_read._KEY_PARAMETERS
        assert len(reads.sent) == math.ceil(60 / (25 - _stored_read._KEY_PARAMETERS))
        assert list(answers) == pks
        assert all(answer.at == now for answer in answers.values())


# -- a read the database refuses ----------------------------------------------


def a_page():
    rows = [a_row(f"row-{i}") for i in range(4)]
    for row in rows:
        a_tick(key(row), now_ish())
    return [row.pk for row in rows]


@pytest.mark.django_db
def test_in_a_transaction_a_read_the_database_refuses_is_raised_as_it_was():
    """
    Halves are for a failure in this process on one value. What the database
    itself refuses says nothing of any one schedule's tick, and inside a
    PostgreSQL transaction no statement can follow it. It is raised as it
    is, with nothing sent after it and no schedule answered as one whose
    tick does not read.
    """
    pks = a_page()
    refusing = Refusing(TICKS)

    with (
        pytest.raises(DataError) as caught,
        transaction.atomic(),
        connection.execute_wrapper(refusing),
    ):
        newest_ticks(pks, using=ALIAS)

    refusing.assert_raised_as_it_was(caught)
    # Rolled back by its owner, the connection reads again.
    assert OxSchedule.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_with_no_transaction_open_a_read_the_database_refuses_is_raised_as_it_was():
    pks = a_page()
    refusing = Refusing(TICKS)

    assert connection.get_autocommit()
    assert not connection.in_atomic_block
    with pytest.raises(DataError) as caught, connection.execute_wrapper(refusing):
        newest_ticks(pks, using=ALIAS)

    refusing.assert_raised_as_it_was(caught)
    assert OxSchedule.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_with_no_transaction_open_a_read_that_fails_on_one_schedule_is_narrowed_to_it():
    """
    A statement fails as a whole when one value in its answer cannot be
    turned into Python, in this process, once the database has answered. The
    page is then read again in halves until the schedule it fails on is
    alone. That one is answered as a schedule whose newest tick does not
    read, with the tick's row named, and every other schedule as it stands.
    """
    now = now_ish()
    rows = [a_row(f"row-{i}") for i in range(4)]
    ticks = {row.pk: a_tick(key(row), now) for row in rows}
    bad = rows[2]
    # Every statement that asks about this schedule by its primary key
    # fails as the driver fails on a value it cannot hand over.
    failing = Failing(SCHEDULES, "id", bad.pk)

    assert connection.get_autocommit()
    assert not connection.in_atomic_block
    with connection.execute_wrapper(failing):
        answers = newest_ticks([row.pk for row in rows], using=ALIAS)

    # The page, the half the schedule is in, and the schedule alone.
    assert len(failing.failed) == 3
    answer = answers[bad.pk]
    assert answer.unreadable
    assert answer.pk == ticks[bad.pk].pk
    assert answer.reason.startswith("scheduled_for could not be read: ")
    assert type(answer.cause) is Failing.RAISES[connection.vendor]
    for row in rows:
        if row is not bad:
            assert answers[row.pk] == TickRead(key(row), at=now, pk=ticks[row.pk].pk)


@pytest.mark.django_db
def test_in_a_transaction_a_refused_lookup_of_the_unreadable_ticks_row_is_raised():
    """
    A schedule whose tick the driver could not hand over is read alone, and
    its newest tick's row is then looked up by key. Each of those reads is
    held to the same rule as the first.
    """
    only_on("sqlite", "SQLite keeps text its driver cannot decode as UTF-8")
    row = a_row("row")
    insert_tick(key(row), NOT_UTF_8)
    # The page, which the driver fails, and then the row of the tick.
    refusing = Refusing(TICKS, nth=2)

    with (
        pytest.raises(DataError) as caught,
        transaction.atomic(),
        connection.execute_wrapper(refusing),
    ):
        newest_ticks([row.pk], using=ALIAS)

    refusing.assert_raised_as_it_was(caught)
