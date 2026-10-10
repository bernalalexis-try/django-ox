"""
Saving a stored schedule's change form: what the write refuses, and the rows
that hold a value that does not read and are repaired by saving.

The form checks a submission, and django_ox.stored checks it again when it
writes, under the row's lock and against the row as it then stands. The two
can disagree: the write reads its clock after waiting for the lock, another
session can change or delete the row in between, and the row can hold a value
the write will not read.

Whatever the write refuses comes back on the form, as any other error does:
nothing is saved, nothing is logged, and what was typed is still there.

Some values that do not read can be shown as they are stored: text or a
fraction in a whole-number column on SQLite, an `enabled` of 2 on MySQL.
Django's own read hands each over unchanged, and each is in a field the form
writes back. A row holding only such values keeps its change form, which
says what does not read, and saving the form replaces it. Every other row
that does not read has the recovery page, which says how to repair it
through django_ox.stored, and that is checked here by doing what it says.

Every value is written by SQL in the form the engine that keeps it keeps it.
A case for another engine's value skips and says why.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.admin.models import CHANGE, DELETION, LogEntry
from django.contrib.admin.sites import site
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import DatabaseError, connection, connections, transaction
from django.db.models import Func, IntegerField
from django.urls import reverse
from django.utils import timezone

import django_ox.admin
import django_ox.exceptions
from django_ox.admin import OxScheduleAdmin, OxScheduleForm
from django_ox.models import OxSchedule
from django_ox.stored import update_schedule

from .dead_connection_tasks import from_another_connection
from .test_schedule_admin_unreadable import CHANGE as CHANGE_PAGE
from .test_schedule_admin_unreadable import (
    DELETE,
    HISTORY,
    SCHEDULES,
    UNREADABLE_PAGE,
    _registry,  # noqa: F401 (a fixture)
    a_schedule,
    body,
    by_sql,
    clock,  # noqa: F401 (a fixture)
    inputs,
    messages_of,
    only_on,
    resubmission,
    set_column,
    staff,
)
from .test_schedule_admin_unreadable_actions import everything_held, held, marker

ADD = "admin:django_ox_oxschedule_add"
CHANGE_FORM = "admin/change_form.html"


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


def in_the_form(at):
    """An instant as the form's two inputs take it: in the project's zone."""
    local = timezone.localtime(at) if timezone.is_aware(at) else at
    return {
        "end_time_0": local.strftime("%Y-%m-%d"),
        "end_time_1": local.strftime("%H:%M:%S"),
    }


def errors_of(response):
    return dict(response.context["adminform"].form.errors)


#: The form's own word for an end the start it will be given has passed.
THE_FORM_SAYS = {
    "end_time": [
        "The end time must be after the start time, which this submission sets to now."
    ]
}
#: The write's word for the same.
THE_WRITE_SAYS = {"end_time": ["The end time must be after the start time."]}


#: (engine, column, SQL literal, what the form is then given for it, what the
#: row holds once saved): a value that does not read as its field, in a field
#: of the form, that Django's own read hands over as it is stored.
SHOWN_AS_STORED = [
    pytest.param(
        "sqlite",
        "every_seconds",
        "'abc'",
        {"every_seconds": "30"},
        30,
        id="sqlite-interval-text",
    ),
    pytest.param(
        "sqlite",
        "every_seconds",
        "60.5",
        {"every_seconds": "30"},
        30,
        id="sqlite-interval-fraction",
    ),
    pytest.param(
        "sqlite",
        "phase_seconds",
        "'abc'",
        {"phase_seconds": "5"},
        5,
        id="sqlite-phase-text",
    ),
    pytest.param(
        "sqlite",
        "starting_deadline_seconds",
        "'abc'",
        {"starting_deadline_seconds": "600"},
        600,
        id="sqlite-deadline-text",
    ),
    pytest.param(
        "mysql", "enabled", "2", {"enabled": "on"}, True, id="mysql-enabled-2"
    ),
]


def shown_as_stored(vendor, column, literal, **over):
    only_on(vendor)
    row = a_schedule("bad", **over)
    set_column(row.pk, column, literal)
    return row


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("vendor", "column", "literal", "given", "saved"), SHOWN_AS_STORED
)
class TestARowThatKeepsItsForm:
    def test_saving_the_form_repairs_it(
        self, client, admin_user, vendor, column, literal, given, saved
    ):
        row = shown_as_stored(vendor, column, literal)
        told = marker()
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        assert opened.status_code == 200, opened.get("Location")
        assert "adminform" in opened.context, "the form was not offered"
        before = timezone.now()
        response = client.post(
            reverse(CHANGE_PAGE, args=[row.pk]), resubmission(opened, **given)
        )
        assert response.status_code == 302, errors_of(response)
        after = OxSchedule.objects.get(pk=row.pk)
        assert getattr(after, column) == saved
        assert type(getattr(after, column)) is type(saved)
        # What the row held is not known, so it starts afresh from the save,
        # as a retime does, and a heal a worker holds from before is fenced.
        assert after.start_time >= before
        assert after.boundary_generation == row.boundary_generation + 1
        assert marker() > told, "the workers were not told"
        entry = LogEntry.objects.get()
        assert (entry.action_flag, entry.object_id) == (CHANGE, str(row.pk))

    def test_its_form_says_what_does_not_read_and_what_saving_does(
        self, client, admin_user, vendor, column, literal, given, saved
    ):
        row = shown_as_stored(vendor, column, literal)
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        assert opened.status_code == 200, opened.get("Location")
        assert CHANGE_FORM in [t.name for t in opened.templates]
        assert UNREADABLE_PAGE not in [t.name for t in opened.templates]
        (warning,) = [m for m in messages_of(opened) if "cannot be read" in m]
        assert re.fullmatch(
            r"This schedule holds a stored value that cannot be read "
            rf"\({column} holds .+\)\. Saving this form replaces it and sets "
            r"the start time to now\.",
            warning,
        ), warning
        # What the input holds is what the row holds.
        assert inputs(opened, column) == literal.strip("'")

    def test_an_end_the_new_start_would_pass_is_the_forms_own_error(
        self, client, admin_user, vendor, column, literal, given, saved
    ):
        # The save gives such a row a start of now, so an end that has
        # already passed is refused, and by the form, in the words it uses
        # for a retime.
        row = shown_as_stored(
            vendor, column, literal, end_time=timezone.now() - timedelta(hours=1)
        )
        before = everything_held(row.pk)
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        response = client.post(
            reverse(CHANGE_PAGE, args=[row.pk]), resubmission(opened, **given)
        )
        assert response.status_code == 200
        assert errors_of(response) == THE_FORM_SAYS
        assert everything_held(row.pk) == before
        assert not LogEntry.objects.exists()

    def test_someone_who_may_not_save_it_has_the_recovery_page(
        self, client, vendor, column, literal, given, saved
    ):
        # The form is kept for the save that repairs the row. Without the
        # change permission it is a page of values to read, and Django draws
        # each as its field's, which a value that is not its field's is not.
        row = shown_as_stored(vendor, column, literal)
        client.force_login(staff("view_oxschedule"))
        response = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        assert response.status_code == 200
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        assert f"<strong>{column}</strong>" in body(response)
        assert not response.context["can_disable"]

    def test_its_history_and_delete_pages_are_the_ordinary_ones(
        self, client, admin_user, vendor, column, literal, given, saved
    ):
        row = shown_as_stored(vendor, column, literal)
        client.force_login(admin_user)
        history = client.get(reverse(HISTORY, args=[row.pk]))
        assert history.status_code == 200
        assert "admin/object_history.html" in [t.name for t in history.templates]
        asked = client.get(reverse(DELETE, args=[row.pk]))
        assert asked.status_code == 200
        assert "admin/delete_confirmation.html" in [t.name for t in asked.templates]
        deleted = client.post(reverse(DELETE, args=[row.pk]), {"post": "yes"})
        assert deleted.status_code == 302
        assert held(row.pk, "id") is None
        entry = LogEntry.objects.get()
        assert (entry.action_flag, entry.object_repr) == (DELETION, "bad")


#: Per engine, a value that does not read in each of three kinds of field:
#: one a caller passes a new value for, one any change writes afresh, and one
#: only django_ox.stored writes and cannot write again without reading.
NOT_SHOWN = {
    "sqlite": {
        "end_time": "'2026-02-30 00:00:00'",
        "start_time": "'banana'",
        "created_at": "'banana'",
        "boundary_generation": "'abc'",
    },
    "postgresql": {
        "end_time": "'infinity'",
        "start_time": "'-infinity'",
        "created_at": "'10000-01-01 00:00:00+00'",
    },
    "mysql": {
        "end_time": "'0000-00-00 00:00:00'",
        "start_time": "'2026-00-10 00:00:00'",
        "created_at": "'0000-00-00 00:00:00'",
    },
}


def not_shown(column):
    literal = NOT_SHOWN[connection.vendor].get(column)
    if literal is None:
        pytest.skip(f"{connection.vendor} keeps no such value in {column}")
    row = a_schedule("bad")
    set_column(row.pk, column, literal)
    return row


@pytest.mark.django_db
class TestTheRecoveryPagesInstructions:
    """Each of its instructions, followed as written."""

    def _page(self, client, admin_user, row):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        assert response.status_code == 200, response.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        return " ".join(body(response).split())

    def test_a_field_a_caller_writes_is_repaired_by_passing_it(
        self, client, admin_user
    ):
        row = not_shown("end_time")
        page = self._page(client, admin_user, row)
        assert f"update_schedule(OxSchedule(pk={row.pk}), end_time=...)" in page
        assert (
            "with replacement values for all these fields in the same call: end_time"
            in page
        )
        update_schedule(OxSchedule(pk=row.pk), end_time=None)
        assert OxSchedule.objects.get(pk=row.pk).end_time is None

    def test_a_field_any_change_writes_is_repaired_by_any_change(
        self, client, admin_user
    ):
        row = not_shown("start_time")
        page = self._page(client, admin_user, row)
        # Not a value for the start: update_schedule does not take one.
        assert f"update_schedule(OxSchedule(pk={row.pk}), name=...)" in page
        assert (
            "with a value for the example argument; its current value also works"
            in page
        )
        assert "writes start_time afresh" in page
        assert "start_time=..." not in page
        before = timezone.now()
        update_schedule(OxSchedule(pk=row.pk), name="bad")
        assert OxSchedule.objects.get(pk=row.pk).start_time >= before

    @pytest.mark.parametrize("column", ["created_at", "boundary_generation"])
    def test_a_field_only_the_module_writes_is_not_repaired(
        self, client, admin_user, column
    ):
        row = not_shown(column)
        page = self._page(client, admin_user, row)
        assert f"Only django_ox.stored writes these fields: {column}" in page
        assert "Neither the API nor the admin can repair them" in page
        assert "To repair it" not in page
        assert f"{column}=..." not in page
        with pytest.raises(django_ox.exceptions.StoredValueUnreadable):
            update_schedule(OxSchedule(pk=row.pk), name="renamed")
        # What the page says can still be done, and its button does.
        assert f"update_schedule(OxSchedule(pk={row.pk}), enabled=False)" in page
        assert 'name="action" value="disable_selected"' in page
        update_schedule(OxSchedule(pk=row.pk), enabled=False)
        assert held(row.pk, "enabled") in ("30", "false", "0")


def the_write_comes_later(monkeypatch, first=None):
    """
    Have an hour pass between the form's check of a submission and the
    write. django_ox.stored reads the clock once it holds the row's lock,
    and a wait for that lock is time the form's check knew nothing of.

    `first`, where given, runs in that hour: inside the transaction the
    submission is handled in, as anything the request writes before the
    save is.
    """
    hands = {"now": timezone.now()}
    monkeypatch.setattr(timezone, "now", lambda: hands["now"])
    checked = OxScheduleAdmin.save_form

    def save_form(self, request, form, change):
        hands["now"] += timedelta(hours=1)
        if first is not None:
            first()
        return checked(self, request, form, change)

    monkeypatch.setattr(OxScheduleAdmin, "save_form", save_form)


@pytest.mark.django_db
class TestASaveTheWriteRefuses:
    """
    The end submitted is half an hour ahead when the form checks it, and
    half an hour behind the start the write then gives the schedule. The
    form drawn after the refusal checks the submission again, at the later
    hour, and so says it in its own words.
    """

    def _retimed(self, client, row):
        opened = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        return resubmission(
            opened,
            name="renamed",
            every_seconds="30",
            **in_the_form(timezone.now() + timedelta(minutes=30)),
        )

    def _nothing_was_saved(self, row):
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.name, after.every_seconds, after.end_time) == (
            "nightly",
            60,
            None,
        )
        assert after.start_time == row.start_time
        assert after.updated_at == row.updated_at
        assert not LogEntry.objects.exists()

    def test_the_form_comes_back_with_the_refusal_and_nothing_is_saved(
        self, client, admin_user, monkeypatch
    ):
        row = a_schedule("nightly")
        told = marker()
        client.force_login(admin_user)
        submission = self._retimed(client, row)
        the_write_comes_later(monkeypatch)
        response = client.post(reverse(CHANGE_PAGE, args=[row.pk]), submission)
        assert response.status_code == 200
        assert CHANGE_FORM in [t.name for t in response.templates]
        assert errors_of(response) == THE_FORM_SAYS
        # What was typed is still in the form.
        assert inputs(response, "name") == "renamed"
        assert inputs(response, "every_seconds") == "30"
        assert inputs(response, "end_time_0") == submission["end_time_0"]
        self._nothing_was_saved(row)
        assert marker() == told, "the workers were told of a change not made"

    def test_a_new_schedule_the_write_refuses_is_not_added(
        self, client, admin_user, monkeypatch
    ):
        client.force_login(admin_user)
        submission = {
            "name": "new",
            "task_key": "report",
            "trigger": "interval",
            "every_seconds": "60",
            "phase_seconds": "0",
            "arguments": "{}",
            "enabled": "on",
            **in_the_form(timezone.now() + timedelta(minutes=30)),
        }
        the_write_comes_later(monkeypatch)
        response = client.post(reverse(ADD), submission)
        assert response.status_code == 200
        assert errors_of(response) == THE_FORM_SAYS
        assert inputs(response, "name") == "new"
        assert not OxSchedule.objects.exists()
        assert not LogEntry.objects.exists()

    def test_the_same_inside_a_request_that_is_one_transaction(
        self, client, admin_user, monkeypatch
    ):
        monkeypatch.setitem(
            connections.settings["default"], "ATOMIC_REQUESTS", value=True
        )
        row = a_schedule("nightly")
        client.force_login(admin_user)
        submission = self._retimed(client, row)
        the_write_comes_later(monkeypatch)
        response = client.post(reverse(CHANGE_PAGE, args=[row.pk]), submission)
        assert response.status_code == 200
        assert errors_of(response) == THE_FORM_SAYS
        self._nothing_was_saved(row)


@pytest.mark.django_db(transaction=True)
class TestARefusedSaveOutsideATransaction:
    """
    A request is not one transaction unless the project sets
    ATOMIC_REQUESTS, and Django opens one around a submitted form. Every
    test above runs inside the transaction its test is wrapped in, where
    that one is a savepoint.
    """

    def test_the_transaction_is_rolled_back_and_none_is_left_open(
        self, client, admin_user, monkeypatch
    ):
        row = a_schedule("nightly")
        beside = a_schedule("beside")
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        submission = resubmission(
            opened,
            name="renamed",
            every_seconds="30",
            **in_the_form(timezone.now() + timedelta(minutes=30)),
        )
        # Something the request writes before the save, which the refusal
        # has to take back with it.
        the_write_comes_later(
            monkeypatch,
            first=lambda: OxSchedule.objects.filter(pk=beside.pk).update(
                name="written-by-the-request"
            ),
        )
        response = client.post(reverse(CHANGE_PAGE, args=[row.pk]), submission)
        assert response.status_code == 200
        assert errors_of(response) == THE_FORM_SAYS
        assert connection.get_autocommit()
        assert not connection.in_atomic_block
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.name, after.every_seconds) == ("nightly", 60)
        assert OxSchedule.objects.get(pk=beside.pk).name == "beside"
        assert not LogEntry.objects.exists()
        assert client.get(reverse(CHANGE_PAGE, args=[row.pk])).status_code == 200


def another_session_meanwhile(monkeypatch, write):
    """
    Have another session run `write`, and commit it, once the form has read
    and checked the row and before the save.

    SQLite has no such moment. The request's transaction has read the table
    by then, and in SQLite's default journal mode no other connection can
    write until it ends; in WAL mode one can, and the request's own write is
    then refused as a locked database, which is the database's error and not
    the row's. Either way the save never meets a row the form did not, and
    these cases are skipped there.
    """
    if connection.vendor == "sqlite":
        pytest.skip(
            "on SQLite no other session's write can land between the form's "
            "read of the row and a save that goes on to write it"
        )
    checked = OxScheduleAdmin.save_form
    done = []

    def save_form(self, request, form, change):
        if not done:
            done.append(True)
            from_another_connection(lambda conn: write())
        return checked(self, request, form, change)

    monkeypatch.setattr(OxScheduleAdmin, "save_form", save_form)


#: (engine, column, SQL literal): a value that does not read, in a field the
#: form writes back.
REPLACED_BY_THE_FORM = [
    pytest.param("postgresql", "end_time", "'infinity'", id="postgresql-end-infinity"),
    pytest.param(
        "mysql", "end_time", "'0000-00-00 00:00:00'", id="mysql-end-zero-date"
    ),
    pytest.param("mysql", "enabled", "2", id="mysql-enabled-2"),
]

#: The same, in a field it does not.
NOT_REPLACED_BY_THE_FORM = [
    pytest.param(
        "postgresql",
        "created_at",
        "'10000-01-01 00:00:00+00'",
        id="postgresql-created-year-10000",
    ),
    pytest.param(
        "mysql", "created_at", "'0000-00-00 00:00:00'", id="mysql-created-zero-date"
    ),
]


@pytest.mark.django_db(transaction=True)
class TestARowAnotherSessionChangesBeforeTheSave:
    def _submission(self, client, row):
        opened = client.get(reverse(CHANGE_PAGE, args=[row.pk]))
        return resubmission(opened, name="renamed")

    @pytest.mark.parametrize(("vendor", "column", "literal"), REPLACED_BY_THE_FORM)
    def test_a_value_the_form_replaces_is_repaired_by_the_save(
        self, client, admin_user, monkeypatch, vendor, column, literal
    ):
        only_on(vendor)
        row = a_schedule("nightly")
        client.force_login(admin_user)
        submission = self._submission(client, row)
        another_session_meanwhile(
            monkeypatch, lambda: set_column(row.pk, column, literal)
        )
        before = timezone.now()
        response = client.post(reverse(CHANGE_PAGE, args=[row.pk]), submission)
        assert response.status_code == 302, errors_of(response)
        after = OxSchedule.objects.get(pk=row.pk)
        assert after.name == "renamed"
        assert (after.end_time, after.enabled) == (None, True)
        assert after.start_time >= before
        entry = LogEntry.objects.get()
        assert (entry.action_flag, entry.object_id) == (CHANGE, str(row.pk))

    @pytest.mark.parametrize(("vendor", "column", "literal"), NOT_REPLACED_BY_THE_FORM)
    def test_a_value_it_does_not_replace_is_an_error_on_the_form(
        self, client, admin_user, monkeypatch, vendor, column, literal
    ):
        only_on(vendor)
        row = a_schedule("nightly")
        client.force_login(admin_user)
        submission = self._submission(client, row)
        another_session_meanwhile(
            monkeypatch, lambda: set_column(row.pk, column, literal)
        )
        response = client.post(reverse(CHANGE_PAGE, args=[row.pk]), submission)
        assert response.status_code == 200
        assert CHANGE_FORM in [t.name for t in response.templates]
        (said,) = errors_of(response)["__all__"]
        assert re.fullmatch(
            r"Nothing was saved\. This schedule holds a stored value django-ox "
            r"cannot use, which this form does not replace: "
            rf"{column} holds .+\. You can still disable or delete the schedule\.",
            said,
        ), said
        assert inputs(response, "name") == "renamed"
        assert held(row.pk, "name") == "nightly"
        assert not LogEntry.objects.exists()
        assert connection.get_autocommit()
        assert not connection.in_atomic_block

    def test_a_schedule_paused_meanwhile_gets_the_writes_own_refusal(
        self, client, admin_user, monkeypatch
    ):
        # Its end has passed, which the form allows a schedule that is not
        # being restarted. Paused by another session, the same submission is
        # a resume, which restarts it from now, and only the write, which
        # reads the row under its lock, can know that.
        row = a_schedule("nightly", end_time=timezone.now() - timedelta(hours=1))
        client.force_login(admin_user)
        submission = self._submission(client, row)
        off = "false" if connection.vendor == "postgresql" else "0"
        another_session_meanwhile(
            monkeypatch, lambda: set_column(row.pk, "enabled", off)
        )
        response = client.post(reverse(CHANGE_PAGE, args=[row.pk]), submission)
        assert response.status_code == 200
        assert errors_of(response) == THE_WRITE_SAYS
        assert inputs(response, "name") == "renamed"
        assert held(row.pk, "name") == "nightly"
        assert not LogEntry.objects.exists()

    def test_a_row_deleted_meanwhile_is_djangos_does_not_exist(
        self, client, admin_user, monkeypatch
    ):
        row = a_schedule("nightly")
        client.force_login(admin_user)
        submission = self._submission(client, row)
        another_session_meanwhile(
            monkeypatch,
            lambda: by_sql(f"DELETE FROM {SCHEDULES} WHERE id = {int(row.pk)}"),  # noqa: S608
        )
        response = client.post(
            reverse(CHANGE_PAGE, args=[row.pk]), submission, follow=True
        )
        assert response.redirect_chain[0][1] == 302
        assert any("doesn\u2019t exist" in m for m in messages_of(response))
        assert not LogEntry.objects.exists()


@pytest.mark.django_db
class TestWhichRowsKeepTheirForm:
    """The read that decides it, where no page can show the difference."""

    def test_a_database_error_is_raised_and_not_taken_for_a_row_that_cannot_be_shown(
        self, client, admin_user, monkeypatch
    ):
        # A statement the database refuses is the database's failure. Taken
        # for a value that does not read, it would send the person to the
        # recovery page of a row there is nothing wrong with.
        if connection.vendor == "postgresql":
            pytest.skip(
                "PostgreSQL's columns keep no value that is shown as stored, so "
                "the read that decides it is never made"
            )
        column, literal = (
            ("enabled", "2")
            if connection.vendor == "mysql"
            else ("every_seconds", "'abc'")
        )
        row = a_schedule("bad")
        set_column(row.pk, column, literal)
        monkeypatch.setattr(
            django_ox.admin,
            "raw",
            lambda name: Func(
                function="ox_no_such_function", output_field=IntegerField()
            ),
        )
        client.force_login(admin_user)
        with pytest.raises(DatabaseError), transaction.atomic():
            client.get(reverse(CHANGE_PAGE, args=[row.pk]))

    def test_a_row_gone_by_then_is_not_shown(self):
        from django_ox._stored_read import UnreadableRow

        gone = UnreadableRow(
            "default", 987654, {"every_seconds": "holds 'abc'"}, {"id": 987654}
        )
        assert django_ox.admin._shown_in_the_form(gone) is None


@pytest.mark.django_db
class TestSaveModelCalledDirectly:
    """
    Code that builds the form and calls save_model itself, with no view
    around it, catches what the write raises: a ValidationError for a
    refusal, OxSchedule.DoesNotExist for a row that is gone. Each is still
    that.
    """

    def _form(self, admin_user, rf, row, **over):
        model_admin = site._registry[OxSchedule]
        request = rf.post("/")
        request.user = admin_user
        submitted = {
            "name": "nightly",
            "task_key": "report",
            "trigger": "interval",
            "every_seconds": "30",
            "phase_seconds": "0",
            "arguments": "{}",
            "enabled": "on",
            **over,
        }
        form = model_admin.get_form(request, row, change=True)(submitted, instance=row)
        assert form.is_valid(), form.errors
        return model_admin, request, form

    def test_a_refusal_is_a_validation_error_with_the_fields_message(
        self, admin_user, rf, monkeypatch
    ):
        row = a_schedule("nightly")
        ends = in_the_form(timezone.now() + timedelta(minutes=30))
        model_admin, request, form = self._form(admin_user, rf, row, **ends)
        later = timezone.now() + timedelta(hours=1)
        monkeypatch.setattr(timezone, "now", lambda: later)
        with pytest.raises(ValidationError) as refused:
            model_admin.save_model(request, form.save(commit=False), form, change=True)
        assert refused.value.message_dict == THE_WRITE_SAYS

    def test_a_row_that_is_gone_is_does_not_exist(self, admin_user, rf):
        row = a_schedule("nightly")
        model_admin, request, form = self._form(admin_user, rf, row)
        by_sql(f"DELETE FROM {SCHEDULES} WHERE id = {int(row.pk)}")  # noqa: S608
        with pytest.raises(OxSchedule.DoesNotExist):
            model_admin.save_model(request, form.save(commit=False), form, change=True)


@pytest.mark.django_db
class TestWhatTheFormDoesWithARefusal:
    """
    The two steps between a refusal and the page, each on its own: which
    error goes where, and what the form then shows.
    """

    def test_each_error_is_put_where_the_form_can_show_it(self):
        refused = ValidationError(
            {
                "every_seconds": [
                    ValidationError(
                        "“%(value)s” value must be an integer.",
                        code="invalid",
                        params={"value": "abc"},
                    )
                ],
                "start_time": ["This time must have no time zone."],
                "__all__": ["Two bounds cannot be compared."],
            }
        )
        errors = django_ox.admin._refusals(OxScheduleForm(), refused)
        assert set(errors) == {"every_seconds", None}
        (kept,) = errors["every_seconds"]
        assert (kept.code, kept.params) == ("invalid", {"value": "abc"})
        # A field the form has no input for is said of the form, by name.
        assert [message for error in errors[None] for message in error.messages] == [
            "start_time: This time must have no time zone.",
            "Two bounds cannot be compared.",
        ]

    def test_the_form_shows_it_and_does_not_say_twice_what_it_found_itself(self):
        a_schedule("taken")
        row = a_schedule("nightly")
        taken = "Ox schedule with this Name already exists."

        class Refused(OxScheduleForm):
            refused = {
                "name": [ValidationError(taken)],
                None: [ValidationError("start_time: no.")],
            }

        submitted = {
            "name": "taken",
            "task_key": "report",
            "trigger": "interval",
            "every_seconds": "60",
            "phase_seconds": "0",
            "arguments": "{}",
        }
        form = Refused(submitted, instance=OxSchedule.objects.get(pk=row.pk))
        assert not form.is_valid()
        assert dict(form.errors) == {"name": [taken], "__all__": ["start_time: no."]}
        # A form nothing was refused of validates as ever.
        submitted["name"] = "nightly"
        assert OxScheduleForm(submitted, instance=row).is_valid()
