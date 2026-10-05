"""
Deleting several stored schedules together.

`django_ox.stored._delete_schedules` is `delete_schedule` for a selection,
for the admin's "Delete selected". One `delete_schedule` a row takes a lock,
a delete and a write to the change marker for every row, so a selection of
a hundred rows was several hundred statements. Here the selection is locked
and read by one statement, every row is asked what `delete_schedule` asks of
one before any is deleted, Django's collector deletes them with its signals
for each instance, and the workers are told once: all of the rows or none.

The rows are locked in primary-key order whatever order they were given in,
so two selections with rows in common cannot be left waiting on each other.
The tests of that, and of what another session does between the page and
the deletion, use a second connection for real: PostgreSQL and MySQL hold
row locks, and SQLite serialises its writers.
"""

import threading
from datetime import timedelta

import pytest
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.db import OperationalError, connection, connections, transaction
from django.db.models.signals import post_delete, pre_delete
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ox import registry, stored
from django_ox._stored_read import UnreadableRow, read_schedules
from django_ox.models import OxSchedule, OxScheduleChange
from django_ox.registry import ScheduleKind, register
from django_ox.stored import _delete_schedules, create_schedule, delete_schedule

from . import tasks
from .conftest import wait_for
from .test_stored_read import columns_read, seen_waiting_for_the_lock

SCHEDULES = "django_ox_oxschedule"
MARKER = "django_ox_oxschedulechange"
ALIAS = "default"

#: Text that is not UTF-8, as SQLite keeps it.
NOT_UTF_8 = "CAST(X'62616480FF' AS TEXT)"

#: One value per engine that Django's own read of a row raises on or
#: misreads, in a column a deletion has no use for.
UNREADABLE_END = {
    "sqlite": "'2026-02-30 00:00:00'",
    "postgresql": "'infinity'",
    "mysql": "'0000-00-00 00:00:00'",
}

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.record))
    register(ScheduleKind(key="nightly", task=tasks.record))
    register(
        ScheduleKind(key="guarded", task=tasks.record, permission="auth.view_user")
    )


def a_row(name, **over):
    """A valid stored schedule, written through the write functions."""
    fields = {
        "name": name,
        "task_key": "report",
        "trigger": "interval",
        "every_seconds": 60,
        "start_time": timezone.now() - timedelta(minutes=10),
    }
    fields.update(over)
    return create_schedule(**fields)


def by_sql(statement, params=()):
    """
    Run one statement the way only SQL can write what it writes. MySQL
    refuses a zero date in the strict mode the suite runs in, so its session
    is let off for the statement and put back.
    """
    with connection.cursor() as cursor:
        if connection.vendor != "mysql":
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


def selected(*rows):
    """
    The rows as the admin hands them over: each read by the guarded read,
    whole where it reads, and otherwise the fields of it that did.
    """
    found = read_schedules([row.pk for row in rows], using=ALIAS)
    return [
        row.deferred_instance() if isinstance(row, UnreadableRow) else row
        for row in found.values()
    ]


def an_unreadable_end(row):
    set_column(row.pk, "end_time", UNREADABLE_END[connection.vendor])
    assert isinstance(read_schedules([row.pk], using=ALIAS)[row.pk], UnreadableRow)
    return row


def marker():
    return OxScheduleChange.objects.get().changed_at


def there(*rows):
    """Which of these rows the table still holds, by key."""
    keys = [row.pk for row in rows]
    return set(OxSchedule.objects.filter(pk__in=keys).values_list("pk", flat=True))


def on_the_schedule_table(queries):
    """The statements among `queries` on the schedule table itself, in order."""
    beside_it = (f"{SCHEDULES}tick", MARKER)
    return [
        q["sql"].lstrip()
        for q in queries.captured_queries
        if SCHEDULES in q["sql"].replace(beside_it[0], "").replace(beside_it[1], "")
    ]


def kinds(queries):
    """Each statement sent: its first word, and the table it is about."""
    said = []
    for q in queries.captured_queries:
        sql = q["sql"].lstrip()
        table = "marker" if MARKER in sql else "schedules" if SCHEDULES in sql else ""
        said.append(f"{sql.split()[0].upper()} {table}".strip())
    return said


class Sent:
    """Every delete signal sent for a stored schedule while it listens."""

    def __init__(self):
        self.signals = []

    def _keep(self, signal):
        def receive(sender, instance, using, origin, **kwargs):
            self.signals.append((signal, sender, instance, instance.pk, using, origin))

        return receive

    def __enter__(self):
        self._pre, self._post = self._keep("pre_delete"), self._keep("post_delete")
        pre_delete.connect(self._pre, sender=OxSchedule)
        post_delete.connect(self._post, sender=OxSchedule)
        return self

    def __exit__(self, *exc):
        pre_delete.disconnect(self._pre, sender=OxSchedule)
        post_delete.disconnect(self._post, sender=OxSchedule)

    def of(self, signal):
        """The instances `signal` was sent for, by the key each had then."""
        return {
            pk: instance for s, _, instance, pk, _, _ in self.signals if s == signal
        }


@pytest.fixture
def sent():
    with Sent() as listening:
        yield listening


@pytest.fixture
def staff():
    """
    A user who holds every permission on the schedule table and not the one
    the guarded entry asks for.
    """
    user = User.objects.create_user("staff", "staff@example.com", "pw", is_staff=True)
    user.user_permissions.add(
        *Permission.objects.filter(content_type__model="oxschedule")
    )
    return User.objects.get(pk=user.pk)  # drop the permission cache


def granted(user):
    user.user_permissions.add(Permission.objects.get(codename="view_user"))
    return User.objects.get(pk=user.pk)  # drop the permission cache


@pytest.fixture
def root():
    return User.objects.create_superuser("root", "root@example.com", "pw")


class TestDeletingASelection:
    def test_the_rows_go_and_the_workers_are_told_once(self):
        rows = [a_row(name) for name in ("first", "second", "third")]
        beside = a_row("beside-them")
        changed = marker()

        with CaptureQueriesContext(connection) as queries:
            _delete_schedules(selected(*rows))

        assert there(*rows, beside) == {beside.pk}
        assert marker() > changed
        written = [k for k in kinds(queries) if k in ("UPDATE marker", "INSERT marker")]
        assert written == ["UPDATE marker"]

    def test_each_instance_is_sent_its_signals_once(self, sent):
        rows = [a_row(name) for name in ("first", "second", "third")]
        # Handed over in an order of their own, which is not their keys'.
        third, first, second = handed = selected(rows[2], rows[0], rows[1])
        origin = OxSchedule.objects.filter(pk__in=[row.pk for row in rows])

        _delete_schedules(handed, origin=origin)

        assert [signal for signal, *_ in sent.signals] == ["pre_delete"] * 3 + [
            "post_delete"
        ] * 3
        for signal in ("pre_delete", "post_delete"):
            got = sent.of(signal)
            assert got.keys() == {row.pk for row in rows}
            # The instance the caller handed over, and no copy of it.
            assert got[rows[0].pk] is first
            assert got[rows[1].pk] is second
            assert got[rows[2].pk] is third
        for _signal, sender, _instance, _pk, using, came_from in sent.signals:
            assert sender is OxSchedule
            assert using == ALIAS
            assert came_from is origin
        assert there(*rows) == set()

    def test_without_an_origin_the_signals_carry_none(self, sent):
        row = a_row("only")

        _delete_schedules(selected(row))

        assert [(signal, origin) for signal, *_, origin in sent.signals] == [
            ("pre_delete", None),
            ("post_delete", None),
        ]
        assert there(row) == set()

    def test_an_empty_selection_sends_nothing(self):
        a_row("beside-it")
        changed = marker()

        with CaptureQueriesContext(connection) as queries:
            _delete_schedules([])

        assert queries.captured_queries == []
        assert marker() == changed

    def test_nothing_of_a_row_is_read_to_delete_it(self, sent):
        """
        A row holding a value that does not read is handed over as the
        fields of it that did, and neither the collector nor the signals
        read the rest: the only read is the one under the lock, of the key
        and the task key.
        """
        healthy = a_row("healthy")
        bad = an_unreadable_end(a_row("bad"))
        handed = selected(healthy, bad)
        assert handed[1].get_deferred_fields() == {"end_time"}

        with CaptureQueriesContext(connection) as queries:
            _delete_schedules(handed)

        assert there(healthy, bad) == set()
        reads = [
            sql
            for sql in on_the_schedule_table(queries)
            if sql.upper().startswith("SELECT")
        ]
        (under_the_lock,) = reads
        assert columns_read(under_the_lock) == {"id", "task_key"}
        assert sent.of("post_delete")[bad.pk] is handed[1]
        assert handed[1].get_deferred_fields() == {"end_time"}

    def test_a_failure_while_deleting_takes_everything_back(self):
        rows = [a_row(name) for name in ("first", "second", "third")]
        changed = marker()
        seen = []

        def fails_on_the_second(sender, instance, **kwargs):
            seen.append(instance.pk)
            if len(seen) == 2:
                raise RuntimeError("a receiver of the project's own failed")

        # After the rows were deleted, so the deletion itself has to go back.
        post_delete.connect(fails_on_the_second, sender=OxSchedule)
        try:
            with pytest.raises(RuntimeError, match="a receiver of the project"):
                _delete_schedules(selected(*rows))
        finally:
            post_delete.disconnect(fails_on_the_second, sender=OxSchedule)

        assert len(seen) == 2
        assert there(*rows) == {row.pk for row in rows}
        assert marker() == changed


class TestWhoMayDeleteASelection:
    """
    With `user`, every row is asked what `delete_schedule` asks of one, by
    the task key the database holds, and all of them before any is deleted.
    """

    def test_one_row_the_user_may_not_delete_keeps_them_all(self, staff, sent):
        # The guarded row has the highest key, so it is the last one asked.
        rows = [a_row("first"), a_row("second"), a_row("guarded", task_key="guarded")]
        handed = selected(*rows)
        before = list(OxSchedule.objects.order_by("pk").values())
        changed = marker()

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(PermissionDenied) as caught,
        ):
            _delete_schedules(handed, user=staff)

        assert "'auth.view_user'" in str(caught.value)
        assert list(OxSchedule.objects.order_by("pk").values()) == before
        assert marker() == changed
        assert sent.signals == []
        # Refused on the reads under the lock: the one for the selection and
        # the one in full, which only the person without the permission
        # model-wide costs, for the backend's question. No delete was sent,
        # and nothing at all to the change marker.
        on_the_table = [
            sql.split()[0].upper() for sql in on_the_schedule_table(queries)
        ]
        if connection.features.has_select_for_update:
            assert on_the_table == ["SELECT", "SELECT"]
        else:
            assert on_the_table == ["UPDATE", "SELECT", "UPDATE", "SELECT"]
        assert not [k for k in kinds(queries) if k.endswith(" marker")]

    def test_with_the_permission_they_all_go(self, staff, sent):
        rows = [a_row("first"), a_row("guarded", task_key="guarded")]
        changed = marker()

        _delete_schedules(selected(*rows), user=granted(staff))

        assert there(*rows) == set()
        assert marker() > changed
        assert sent.of("post_delete").keys() == {row.pk for row in rows}

    def test_a_key_changed_on_a_callers_instance_authorizes_nothing(self, staff):
        guarded = a_row("guarded", task_key="guarded")
        open_to_all = a_row("open")
        mine = selected(guarded, open_to_all)
        mine[0].task_key = "report"

        with pytest.raises(PermissionDenied):
            _delete_schedules(mine, user=staff)

        assert there(guarded, open_to_all) == {guarded.pk, open_to_all.pk}
        # Nor does one refuse a row anyone may schedule.
        theirs = selected(open_to_all)
        theirs[0].task_key = "guarded"
        _delete_schedules(theirs, user=staff)
        assert there(guarded, open_to_all) == {guarded.pk}

    def test_keys_written_as_text_are_asked_the_same(self, staff, sent):
        guarded = a_row("guarded", task_key="guarded")
        open_to_all = a_row("open")
        named = [OxSchedule(pk=str(row.pk)) for row in (open_to_all, guarded)]
        told = marker()

        with pytest.raises(PermissionDenied):
            _delete_schedules(named, user=staff)

        assert there(guarded, open_to_all) == {guarded.pk, open_to_all.pk}
        assert sent.signals == []
        assert marker() == told

    def test_an_object_level_backend_is_handed_each_row_as_the_database_holds_it(self):
        rows = [
            a_row("first", task_key="guarded"),
            a_row("open"),
            a_row("second", task_key="guarded"),
        ]
        asked = []

        class _ByObject:
            def has_perm(self, perm, obj=None):
                if obj is not None:
                    asked.append((perm, obj.pk, obj.task_key))
                return obj is not None

        # Given highest key first, and asked in the order of the keys.
        _delete_schedules(list(reversed(selected(*rows))), user=_ByObject())

        assert asked == [
            ("auth.view_user", rows[0].pk, "guarded"),
            ("auth.view_user", rows[2].pk, "guarded"),
        ]
        assert there(*rows) == set()

    @pytest.mark.parametrize("who", ["staff", "retired", "root", "no user"])
    def test_a_task_key_not_registered_here_takes_a_superuser(
        self, staff, root, sent, who
    ):
        """
        A selection of a healthy row, a row holding a value that does not
        read, and a row whose task key this process does not register: the
        last names no entry, so the permission it requires is not known.
        """
        retired = User.objects.create_superuser("retired", "r@example.com", "pw")
        retired.is_active = False
        retired.save()
        healthy = a_row("healthy")
        bad = an_unreadable_end(a_row("bad"))
        orphan = a_row("orphan", task_key="nightly")
        registry._registry.pop("nightly")
        rows = [healthy, bad, orphan]
        changed = marker()
        by = {
            "staff": {"user": granted(staff)},
            "retired": {"user": retired},
            "root": {"user": root},
            "no user": {},
        }[who]

        if who in ("root", "no user"):
            _delete_schedules(selected(*rows), **by)
            assert there(*rows) == set()
            assert marker() > changed
            assert sent.of("post_delete").keys() == {row.pk for row in rows}
            return
        with pytest.raises(PermissionDenied) as caught:
            _delete_schedules(selected(*rows), **by)
        assert f"Schedule {orphan.pk}'s task key 'nightly' is not registered" in str(
            caught.value
        )
        assert there(*rows) == {row.pk for row in rows}
        assert marker() == changed
        assert sent.signals == []

    @pytest.mark.parametrize("who", ["staff", "root", "no user"])
    def test_a_task_key_that_does_not_read_takes_a_superuser(
        self, staff, root, sent, who
    ):
        if connection.vendor != "sqlite":
            pytest.skip(
                "a task key that does not read: only SQLite keeps text that is "
                f"not UTF-8; this run is on {connection.vendor}"
            )
        healthy = a_row("healthy")
        orphan = a_row("orphan", task_key="nightly")
        registry._registry.pop("nightly")
        unread = a_row("unread")
        set_column(unread.pk, "task_key", NOT_UTF_8)
        rows = [healthy, unread, orphan] if who != "staff" else [healthy, unread]
        handed = selected(*rows)
        assert "task_key" in handed[1].get_deferred_fields()
        by = {"staff": {"user": granted(staff)}, "root": {"user": root}, "no user": {}}

        if who == "staff":
            with pytest.raises(PermissionDenied) as caught:
                _delete_schedules(handed, **by[who])
            assert f"Schedule {unread.pk}'s task key cannot be read" in str(
                caught.value
            )
            assert there(*rows) == {row.pk for row in rows}
            assert sent.signals == []
            return
        _delete_schedules(handed, **by[who])
        assert there(*rows) == set()
        assert sent.of("pre_delete").keys() == {row.pk for row in rows}

    def test_a_row_that_is_already_gone_is_treated_as_delete_schedule_treats_one(
        self, staff
    ):
        """
        `delete_schedule` asks nothing about a row that is gone, raises
        nothing, tells the workers, and hands the instance to Django all the
        same, which sends its signals for it. A selection does each of those
        for a row of its own that is gone, and deletes the rest.
        """
        alone = a_row("gone-alone", task_key="guarded")
        (alone_instance,) = selected(alone)
        OxSchedule.objects.filter(pk=alone.pk).delete()
        with Sent() as for_one:
            delete_schedule(alone_instance, user=staff)

        gone = a_row("gone", task_key="guarded")
        live = a_row("live")
        handed = selected(gone, live)
        OxSchedule.objects.filter(pk=gone.pk).delete()
        changed = marker()
        with Sent() as for_many:
            _delete_schedules(handed, user=staff)

        assert there(gone, live) == set()
        assert marker() > changed
        assert [signal for signal, *_ in for_one.signals] == [
            "pre_delete",
            "post_delete",
        ]
        of_the_gone_row = [s for s, _, _, pk, _, _ in for_many.signals if pk == gone.pk]
        assert of_the_gone_row == ["pre_delete", "post_delete"]
        assert for_many.of("post_delete").keys() == {gone.pk, live.pk}


class TestTheStatementsOfASelection:
    """
    What a selection costs does not grow with it: one read under the locks,
    the collector's delete, which it sends a hundred keys at a time, and one
    write to the change marker.
    """

    def _rows(self, count):
        now = timezone.now()
        OxSchedule.objects.bulk_create(
            OxSchedule(
                name=f"row-{i}",
                task_key="report",
                trigger="interval",
                every_seconds=60,
                start_time=now - timedelta(minutes=10),
                created_at=now,
                updated_at=now,
            )
            for i in range(count)
        )
        return list(OxSchedule.objects.order_by("pk"))

    def _sent_for(self, count, **by):
        rows = self._rows(count)
        with CaptureQueriesContext(connection) as queries:
            _delete_schedules(rows, **by)
        assert not OxSchedule.objects.exists()
        return kinds(queries)

    def test_three_thirty_and_a_hundred_rows_are_the_same_statements(self, staff):
        # Inside this test's transaction the function's own block is a
        # savepoint, and so is the one Django writes the marker in.
        expected = [
            "SAVEPOINT",
            "SELECT schedules",
            "DELETE schedules",
            "SAVEPOINT",
            "SELECT marker",
            "UPDATE marker",
            "RELEASE",
            "RELEASE",
        ]
        if not connection.features.has_select_for_update:
            # The no-op write that makes the transaction the writer.
            expected.insert(1, "UPDATE schedules")
        a_row("so-that-the-marker-exists")
        OxSchedule.objects.all().delete()

        for count in (3, 30, 100):
            assert self._sent_for(count, user=staff) == expected, count
            assert self._sent_for(count) == expected, count

    def test_past_a_hundred_only_the_collectors_deletes_are_added(self, staff):
        a_row("so-that-the-marker-exists")
        OxSchedule.objects.all().delete()

        few = self._sent_for(3, user=staff)
        many = self._sent_for(250, user=staff)

        assert many.count("DELETE schedules") == 3
        assert few.count("DELETE schedules") == 1
        without_deletes = [k for k in many if k != "DELETE schedules"]
        assert without_deletes == [k for k in few if k != "DELETE schedules"]

    def test_past_five_hundred_they_are_locked_a_read_at_a_time_in_key_order(
        self, staff
    ):
        """
        One statement reads five hundred keys. A selection past that is
        locked by a read for each five hundred, each taking the next keys
        up, so the order of the locks holds across them.
        """
        rows = self._rows(1050)
        keys = [row.pk for row in rows]
        under_the_lock = []

        def reads(execute, sql, params, many, context):
            on_the_rows = SCHEDULES in sql and MARKER not in sql
            if on_the_rows and sql.lstrip().upper().startswith("SELECT"):
                under_the_lock.append(sorted(params))
            return execute(sql, params, many, context)

        with connection.execute_wrapper(reads):
            _delete_schedules(list(reversed(rows)), user=staff)

        assert not OxSchedule.objects.exists()
        assert under_the_lock == [keys[:500], keys[500:1000], keys[1000:]]

    def test_the_rows_are_locked_by_one_read_in_the_order_of_their_keys(self, staff):
        rows = [a_row(name) for name in ("first", "second", "third")]
        handed = list(reversed(selected(*rows)))

        with CaptureQueriesContext(connection) as queries:
            _delete_schedules(handed, user=staff)

        on_the_table = on_the_schedule_table(queries)
        if not connection.features.has_select_for_update:
            # The write that makes the transaction the writer names one key.
            made_the_writer, read, _delete = on_the_table
            assert made_the_writer.upper().startswith("UPDATE")
            assert "FOR UPDATE" not in read
        else:
            read, _delete = on_the_table
            assert [sql for sql in on_the_table if "FOR UPDATE" in sql] == [read]
            key = connection.ops.quote_name("id")
            assert f"{key} ASC" in read.split("ORDER BY")[1]
        assert columns_read(read) == {"id", "task_key"}


def waiting_for_a_lock():
    """
    How many sessions the database says are waiting for a lock just now.

    Asked on the test's own connection, which has no transaction open, so
    each asking is answered afresh. Not on one more connection: with the
    three sessions the test already holds beside this one, a connection
    pool of the default size has none left to give.
    """
    assert connection.get_autocommit()
    assert not connection.in_atomic_block
    query = {
        "postgresql": (
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE wait_event_type = 'Lock' AND datname = current_database()"
        ),
        "mysql": (
            "SELECT count(*) FROM information_schema.innodb_trx "
            "WHERE trx_state = 'LOCK WAIT'"
        ),
    }[connection.vendor]
    with connection.cursor() as cursor:
        cursor.execute(query)
        return cursor.fetchone()[0]


def in_a_session_of_its_own(work, out, name):
    """A thread that runs `work` on its own connection and keeps what it raised."""

    def run():
        try:
            out[name] = work()
        except BaseException as exc:  # handed to the test, which fails on it
            out[f"{name} raised"] = exc
        finally:
            connections.close_all()

    return threading.Thread(target=run, name=name)


@pytest.mark.django_db(transaction=True)
class TestASelectionBesideAnotherSession:
    """
    What another session does to a selected row between the page that
    listed it and the deletion is met under the row's lock: the deletion
    waits for the session that holds it and goes by what that session
    committed.
    """

    def _held_while(self, pk, write, monkeypatch):
        """
        Hold the row's lock on another connection, run `write` under it,
        and commit once `_delete_schedules` here has come to take its
        locks and been given the time to be seen waiting.
        """
        entered = threading.Event()
        locked = threading.Event()
        committing = threading.Event()
        out = {}
        lock_schedules = stored.lock_schedules

        def entering(keys, *, using, fields=None):
            entered.set()
            return lock_schedules(keys, using=using, fields=fields)

        def other():
            with transaction.atomic():
                stored.lock_schedule(pk, using=ALIAS)
                write()
                locked.set()
                assert entered.wait(timeout=30)
                out["waited"] = seen_waiting_for_the_lock()
                committing.set()

        monkeypatch.setattr(stored, "lock_schedules", entering)
        thread = in_a_session_of_its_own(other, out, "other")
        return thread, locked, committing, out

    def test_a_row_given_a_guarded_key_since_the_page_is_refused_by_that_key(
        self, monkeypatch
    ):
        staff = User.objects.create_user("staff", "staff@example.com", "pw")
        first, rekeyed = a_row("first"), a_row("re-keyed")
        handed = selected(first, rekeyed)
        changed = marker()

        def given_a_key_with_a_permission():
            OxSchedule.objects.filter(pk=rekeyed.pk).update(task_key="guarded")

        thread, locked, committing, out = self._held_while(
            rekeyed.pk, given_a_key_with_a_permission, monkeypatch
        )
        thread.start()
        try:
            assert locked.wait(timeout=30)
            # Anyone may delete both rows as the page read them.
            with Sent() as sent, pytest.raises(PermissionDenied) as caught:
                _delete_schedules(handed, user=staff)
            assert committing.is_set(), "the deletion did not wait for the lock"
        finally:
            thread.join(timeout=60)
        assert not thread.is_alive()
        assert "other raised" not in out, out
        assert out["waited"] is not False, "the deletion was not seen waiting"
        assert "'auth.view_user'" in str(caught.value)
        assert there(first, rekeyed) == {first.pk, rekeyed.pk}
        assert OxSchedule.objects.get(pk=rekeyed.pk).task_key == "guarded"
        assert marker() == changed
        assert sent.signals == []

    def test_a_row_deleted_since_the_page_is_passed_over_and_the_rest_go(
        self, monkeypatch
    ):
        staff = User.objects.create_user("staff", "staff@example.com", "pw")
        # Guarded when the page listed it: gone, there is nothing to ask.
        gone, live = a_row("gone", task_key="guarded"), a_row("live")
        handed = selected(gone, live)
        changed = marker()

        def deleted():
            OxSchedule.objects.filter(pk=gone.pk).delete()

        thread, locked, committing, out = self._held_while(
            gone.pk, deleted, monkeypatch
        )
        thread.start()
        try:
            assert locked.wait(timeout=30)
            _delete_schedules(handed, user=staff)
            assert committing.is_set(), "the deletion did not wait for the lock"
        finally:
            thread.join(timeout=60)
        assert not thread.is_alive()
        assert "other raised" not in out, out
        assert out["waited"] is not False, "the deletion was not seen waiting"
        assert there(gone, live) == set()
        assert marker() > changed

    def test_two_selections_given_in_opposite_orders_meet_at_their_lowest_key(self):
        """
        Each waits for the lowest key the two share before it holds any
        other, so neither can hold a row the other is waiting for. A third
        session holds that key here, so that both are waiting at once, and
        the rows above it are shown to be free while they wait.
        """
        if not connection.features.has_select_for_update:
            pytest.skip(
                "row locks and the order they are taken in: PostgreSQL and MySQL; "
                f"{connection.vendor} has one writer at a time and no row locks"
            )
        lowest, middle, highest = rows = [a_row(n) for n in ("low", "mid", "high")]
        # Written again, so that on PostgreSQL the lowest key is no longer
        # the first row the table hands over.
        OxSchedule.objects.filter(pk=lowest.pk).update(cron="")
        ascending = selected(lowest, middle, highest)
        descending = list(reversed(selected(lowest, middle, highest)))
        release = threading.Event()
        holding = threading.Event()
        out = {}

        def holds_the_lowest():
            with transaction.atomic():
                stored.lock_schedule(lowest.pk, using=ALIAS)
                holding.set()
                assert release.wait(timeout=60)

        holder = in_a_session_of_its_own(holds_the_lowest, out, "holder")
        one = in_a_session_of_its_own(lambda: _delete_schedules(ascending), out, "one")
        two = in_a_session_of_its_own(lambda: _delete_schedules(descending), out, "two")
        holder.start()
        try:
            assert holding.wait(timeout=30)
            one.start()
            two.start()
            assert wait_for(lambda: waiting_for_a_lock() == 2, timeout=30, interval=0.1)
            # Both wait at the lowest key, and neither holds a row above it:
            # each can be locked at once from here. One row a statement, by
            # its key alone: MySQL locks every row a locking read's plan
            # passes over, and a read of both may be given a plan that
            # passes over the lowest.
            with transaction.atomic():
                for row in (middle, highest):
                    free = OxSchedule.objects.select_for_update(nowait=True)
                    assert free.filter(pk=row.pk).values_list("pk").get() == (row.pk,)
        finally:
            release.set()
            for thread in (holder, one, two):
                thread.join(timeout=60)
        assert not [t for t in (holder, one, two) if t.is_alive()]
        raised = {name: exc for name, exc in out.items() if name.endswith("raised")}
        assert not raised, raised
        assert there(*rows) == set()

    def test_a_selection_whose_lowest_key_is_gone_still_waits_its_turn(self):
        """
        SQLite has no row locks: what serialises it is being the writer,
        which one no-op write naming the lowest key makes the transaction
        before it reads, whether or not that key still has a row.
        """
        if connection.vendor != "sqlite":
            pytest.skip(
                "the write that makes a SQLite transaction the writer; "
                f"{connection.vendor} locks rows with the read itself"
            )
        gone, live = a_row("gone"), a_row("live")
        handed = selected(gone, live)
        OxSchedule.objects.filter(pk=gone.pk).delete()
        out = {}

        def writes_meanwhile():
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA busy_timeout = 300")
            try:
                OxSchedule.objects.filter(pk=live.pk).update(cron="")
            except OperationalError as exc:
                return exc
            return None

        with transaction.atomic():
            stored.lock_schedules([row.pk for row in handed], using=ALIAS)
            other = in_a_session_of_its_own(writes_meanwhile, out, "other")
            other.start()
            other.join(timeout=30)
        assert "other raised" not in out, out
        assert isinstance(out["other"], OperationalError), out
        assert "locked" in str(out["other"])
