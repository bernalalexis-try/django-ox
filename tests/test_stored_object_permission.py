"""
What a permission backend that decides by the row is handed.

A registry entry can name a permission. A user holding it model-wide may
change the entry's schedules; a user who does not is asked of the row itself,
so a backend can grant it on some schedules and not on others, by their
name, say. The row it is asked about is the row as the database holds it,
read from the write alias under the lock the call already holds, and in
full: a backend that looks at any field sees what is stored there, and not
what a replica that is behind holds, or nothing.

A field that does not read is not made up. The backend that touches one is
refused, as a backend that cannot see what its decision rests on has not
granted anything. The permission held model-wide asks the backend nothing
and costs no read of the row beyond the call's own.

The pause, the delete and the admin's batch delete read only the columns
they need, so each has to read the rest when a backend is to be asked.
"""

from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from django_ox._stored_read import UnreadableRow, lock_schedule
from django_ox.exceptions import StoredValueUnreadable
from django_ox.models import OxSchedule
from django_ox.registry import ScheduleKind, register
from django_ox.stored import (
    _delete_schedules,
    create_schedule,
    delete_schedule,
    update_schedule,
)

from . import tasks
from .test_replica_reads import PRIMARY, REPLICA, _ReplicaReadRouter
from .test_stored_read import NOT_UTF_8, by_sql, only_on, set_column

SCHEDULES = "django_ox_oxschedule"
PERMISSION = "auth.view_user"


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.record))
    register(ScheduleKind(key="guarded", task=tasks.record, permission=PERMISSION))


class Backend:
    """
    A user whose permission is an object-level backend's: it holds the
    entry's permission on a schedule whose name starts with the user's name
    and a dash, and on nothing else. `model_wide` is the same permission
    held for every schedule. `asked` is what each question about a row was
    handed: the fields that were not loaded on it.
    """

    is_active = True
    is_superuser = False

    def __init__(self, username="objuser", *, model_wide=False):
        self.username = username
        self.model_wide = model_wide
        self.asked = []

    def has_perm(self, perm, obj=None):
        assert perm == PERMISSION
        if obj is None:
            return self.model_wide
        self.asked.append(sorted(obj.get_deferred_fields()))
        name = obj.name
        return isinstance(name, str) and name.startswith(f"{self.username}-")


class ByTaskKey(Backend):
    """The same, deciding by the task key alone."""

    def has_perm(self, perm, obj=None):
        if obj is None:
            return self.model_wide
        self.asked.append(sorted(obj.get_deferred_fields()))
        return obj.task_key == "guarded"


def a_row(name, **over):
    fields = {
        "name": name,
        "task_key": "guarded",
        "trigger": "interval",
        "every_seconds": 60,
        "start_time": timezone.now() - timedelta(minutes=10),
    }
    fields.update(over)
    return create_schedule(**fields)


def pause(user, row):
    update_schedule(OxSchedule(pk=row.pk), user=user, enabled=False)


def delete(user, row):
    delete_schedule(OxSchedule(pk=row.pk), user=user)


def batch_delete(user, row):
    _delete_schedules([OxSchedule(pk=row.pk)], user=user)


OPERATIONS = [
    pytest.param(pause, id="pause"),
    pytest.param(delete, id="delete"),
    pytest.param(batch_delete, id="batch-delete"),
]


def stored(row):
    """The row as the database holds it, or None once it is gone."""
    return (
        OxSchedule.objects.using(row._state.db or "default").filter(pk=row.pk).first()
    )


def untouched(row, before):
    kept = stored(row)
    assert kept is not None, "the row was deleted"
    assert kept.enabled is True
    assert kept.name == before


@pytest.mark.django_db
class TestABackendIsHandedTheWholeRow:
    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_a_name_the_backend_grants_goes_through(self, operation):
        row = a_row("objuser-mine")
        user = Backend()

        operation(user, row)

        assert user.asked == [[]], "every field was loaded on the row it was handed"
        kept = stored(row)
        if operation is pause:
            assert kept.enabled is False
            assert kept.name == "objuser-mine"
        else:
            assert kept is None

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_a_name_the_backend_does_not_grant_is_refused(self, operation):
        row = a_row("other-team")
        user = Backend()

        with pytest.raises(PermissionDenied, match=r"needs the 'auth\.view_user'"):
            operation(user, row)

        assert user.asked == [[]]
        untouched(row, "other-team")

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_a_task_key_backend_is_still_asked_and_still_decides(self, operation):
        row = a_row("other-team")
        user = ByTaskKey()

        operation(user, row)

        assert user.asked == [[]]

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_the_permission_held_model_wide_asks_the_backend_nothing(self, operation):
        row = a_row("other-team")
        user = Backend(model_wide=True)

        operation(user, row)

        assert user.asked == []

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_the_row_in_full_is_one_more_read_for_whoever_lacks_the_permission(
        self, operation
    ):
        def reads_of(user, row):
            with CaptureQueriesContext(connection) as queries:
                operation(user, row)
            return [
                q["sql"]
                for q in queries.captured_queries
                if q["sql"].lstrip().upper().startswith("SELECT")
                and f"FROM {connection.ops.quote_name(SCHEDULES)}" in q["sql"]
            ]

        held = reads_of(Backend(model_wide=True), a_row("other-team"))
        asked = reads_of(Backend(), a_row("objuser-mine"))

        assert len(asked) == len(held) + 1

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_a_row_whose_key_this_process_does_not_register_is_for_superusers(
        self, operation
    ):
        row = a_row("objuser-mine")
        by_sql(f"UPDATE {SCHEDULES} SET task_key = 'removed' WHERE id = {row.pk}")  # noqa: S608
        user = Backend(model_wide=True)

        with pytest.raises(PermissionDenied):
            operation(user, row)

        assert user.asked == []
        assert stored(row) is not None


@pytest.mark.django_db
class TestAFieldThatDoesNotReadIsNotMadeUp:
    VALUES = [
        pytest.param(NOT_UTF_8, id="not-utf-8"),
        pytest.param("X'626c6f622d6e616d65'", id="bytes"),
    ]

    @pytest.mark.parametrize("operation", OPERATIONS)
    @pytest.mark.parametrize("literal", VALUES)
    def test_a_backend_that_touches_it_is_refused(self, operation, literal):
        only_on("sqlite", "SQLite keeps a name that is not text")
        row = a_row("objuser-mine")
        set_column(row.pk, "name", literal)
        user = Backend()

        with pytest.raises(PermissionDenied):
            operation(user, row)

        # Asked once, with the name not loaded, and refused.
        assert user.asked == [["name"]]
        assert stored_alive(row)

    @pytest.mark.parametrize("operation", OPERATIONS)
    @pytest.mark.parametrize("literal", VALUES)
    def test_a_backend_that_does_not_touch_it_decides_as_it_would(
        self, operation, literal
    ):
        only_on("sqlite", "SQLite keeps a name that is not text")
        row = a_row("objuser-mine")
        set_column(row.pk, "name", literal)
        user = ByTaskKey()

        operation(user, row)

        assert user.asked == [["name"]]
        found = lock_schedule(row.pk, using="default")
        if operation is pause:
            assert isinstance(found, UnreadableRow)
            assert found.values["enabled"] is False
        else:
            assert found is None


def stored_alive(row):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT enabled FROM {SCHEDULES} WHERE id = %s", [row.pk])  # noqa: S608
        (enabled,) = cursor.fetchone()
    return bool(enabled)


@pytest.mark.django_db(databases=[REPLICA, PRIMARY])
class TestUnderAReplicaThatIsBehind:
    """
    The primary is the write alias and the replica serves reads, and holds
    another name for the same row. The name that decides is the primary's.
    """

    @pytest.fixture(autouse=True)
    def _router(self, settings):
        settings.DATABASE_ROUTERS = [_ReplicaReadRouter()]

    def a_lagging_row(self, on_primary, on_replica):
        row = a_row(on_primary)
        assert row._state.db == PRIMARY
        copy = OxSchedule.objects.using(PRIMARY).get(pk=row.pk)
        copy.name = on_replica
        copy._state.adding = True
        copy.save(using=REPLICA, force_insert=True)
        return row

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_the_primarys_name_refuses_what_the_replicas_would_grant(self, operation):
        row = self.a_lagging_row("other-team", "objuser-mine")
        user = Backend()

        with pytest.raises(PermissionDenied):
            operation(user, row)

        assert user.asked == [[]]
        kept = OxSchedule.objects.using(PRIMARY).get(pk=row.pk)
        assert (kept.name, kept.enabled) == ("other-team", True)

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_the_primarys_name_grants_what_the_replicas_would_refuse(self, operation):
        row = self.a_lagging_row("objuser-mine", "other-team")
        user = Backend()

        operation(user, row)

        assert user.asked == [[]]
        kept = OxSchedule.objects.using(PRIMARY).filter(pk=row.pk).first()
        if operation is pause:
            assert kept.enabled is False
        else:
            assert kept is None

    @pytest.mark.parametrize("operation", OPERATIONS)
    def test_the_replica_is_not_read_for_the_question(self, operation):
        row = self.a_lagging_row("objuser-mine", "other-team")
        sent = []

        def on_the_replica(execute, sql, params, many, context):
            sent.append(sql)
            return execute(sql, params, many, context)

        with connections[REPLICA].execute_wrapper(on_the_replica):
            operation(Backend(), row)

        assert sent == []


@pytest.mark.django_db
def test_a_field_that_did_not_read_raises_where_it_is_touched_and_asks_no_database():
    only_on("sqlite", "SQLite keeps a name that is not UTF-8")
    row = a_row("objuser-mine")
    set_column(row.pk, "name", NOT_UTF_8)
    found = lock_schedule(row.pk, using="default")
    assert isinstance(found, UnreadableRow)
    instance = found.guarded_instance()

    with CaptureQueriesContext(connection) as queries:
        assert instance.task_key == "guarded"
        with pytest.raises(StoredValueUnreadable) as caught:
            instance.name  # noqa: B018
        with pytest.raises(StoredValueUnreadable):
            instance.refresh_from_db()

    assert caught.value.fields == ("name",)
    assert caught.value.pk == row.pk
    assert not queries.captured_queries
