"""
The schedule admin inside a request that is one transaction.

A project that sets ATOMIC_REQUESTS has Django run each view in a
transaction, committed when the view returns and rolled back when it
raises. Every read these pages make is then a statement in that
transaction, and no read here takes a savepoint to come back to. Two things
have to hold, and they are told apart by where a read failed.

A stored value that does not read fails in this process, once the database
has answered, and leaves the transaction as it was. So the page goes on as
it does outside one: the changelist lists the row in its place, the change
page is the recovery page or the form that repairs it, a save the write
refuses comes back on the form, and whatever the request wrote commits.

A statement the database itself refuses is another matter. On PostgreSQL it
ends the transaction for every statement after it, and it says nothing
about any one row. So the database's own error leaves the request as it was
raised: no row is named for it, no narrower read is sent into a transaction
that may be over, no "current transaction is aborted" takes its place, and
everything the request had written goes back with it.

Every test here runs with ATOMIC_REQUESTS on and no transaction around the
test, so the transaction the request runs in is a real one, and each checks
that it was. A value is written by SQL in the form the engine that keeps it
keeps it; a case for another engine's value skips and says why.
"""

import re
from datetime import timedelta

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import User
from django.db import DataError, connection, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from django_ox.models import OxSchedule

from .test_schedule_admin_save import (
    CHANGE_FORM,
    NOT_REPLACED_BY_THE_FORM,
    SHOWN_AS_STORED,
    THE_FORM_SAYS,
    another_session_meanwhile,
    errors_of,
    in_the_form,
    shown_as_stored,
    the_write_comes_later,
)
from .test_schedule_admin_unreadable import (
    CHANGE,
    CHANGELIST,
    DELETE,
    RECOVERY_ROWS,
    UNREADABLE_PAGE,
    UNREADABLE_ROWS,
    _registry,  # noqa: F401 (a fixture)
    a_schedule,
    a_tick,
    an_unreadable_schedule,
    body,
    clock,  # noqa: F401 (a fixture)
    inputs,
    insert_tick,
    label,
    messages_of,
    only_on,
    resubmission,
    set_column,
)
from .test_schedule_admin_unreadable_actions import held, marker, paused
from .test_stored_read import Refusing

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture(autouse=True)
def atomic_requests(monkeypatch):
    monkeypatch.setitem(connections.settings["default"], "ATOMIC_REQUESTS", value=True)


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


def ended(queries):
    """
    How the one transaction a request ran in ended: "COMMIT" or "ROLLBACK".
    Fails if the request opened none, or more than one, or sent a statement
    about a schedule outside it.
    """
    sent = [q["sql"] for q in queries.captured_queries]
    opened = [i for i, sql in enumerate(sent) if sql == "BEGIN"]
    closed = [i for i, sql in enumerate(sent) if sql in ("COMMIT", "ROLLBACK")]
    assert len(opened) == 1, sent
    assert len(closed) == 1, sent
    outside = sent[: opened[0]] + sent[closed[0] + 1 :]
    assert not [sql for sql in outside if "django_ox_" in sql], outside
    return sent[closed[0]]


def left_as_found():
    """Nothing is left open behind a request, however it ended."""
    return connection.get_autocommit() and not connection.in_atomic_block


def named_unreadable(caplog):
    return [
        record.event
        for record in caplog.records
        if getattr(record, "event", None)
        in ("schedule_admin_row_unreadable", "schedule_tick_unreadable")
    ]


# -- a statement the database refuses ----------------------------------------

#: What marks each guarded read in the statement it sends: the names its
#: values are fetched under.
ROWS = "ox_stored_enabled"
NEWEST_TICKS = "ox_stored_tick"
KEEPS_ITS_FORM = "ox_held_"


def changelist(client, row):
    return client.get(reverse(CHANGELIST))


def change_page(client, row):
    return client.get(reverse(CHANGE, args=[row.pk]))


def delete_page(client, row):
    return client.get(reverse(DELETE, args=[row.pk]))


def disable(client, *rows):
    return client.post(
        reverse(CHANGELIST),
        {
            "action": "disable_selected",
            "_selected_action": [str(row.pk) for row in rows],
        },
    )


#: (the read, the request that makes it, what marks its statement, and which
#: of the request's statements so marked it is).
REFUSED_READS = [
    pytest.param(changelist, ROWS, 1, id="the-changelists-rows"),
    pytest.param(changelist, NEWEST_TICKS, 1, id="the-changelists-newest-ticks"),
    pytest.param(change_page, ROWS, 1, id="the-change-pages-row"),
    pytest.param(change_page, NEWEST_TICKS, 1, id="the-change-forms-newest-tick"),
    pytest.param(delete_page, ROWS, 1, id="the-delete-pages-row"),
    # The page the action was chosen on is read first, then its selection.
    pytest.param(disable, ROWS, 2, id="an-actions-selection"),
]


class TestAReadTheDatabaseRefuses:
    @pytest.mark.parametrize(("request_of", "marked", "nth"), REFUSED_READS)
    def test_its_own_error_leaves_the_request(
        self, client, admin_user, caplog, request_of, marked, nth
    ):
        row = a_schedule("healthy")
        a_tick(row, timezone.now() - timedelta(hours=1))
        a_schedule("beside")
        client.force_login(admin_user)
        caplog.set_level("WARNING", logger="django_ox")
        refusing = Refusing(marked, nth=nth)

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(DataError) as caught,
            connection.execute_wrapper(refusing),
        ):
            request_of(client, row)

        # The database's error, of the class and in the words it was raised
        # with, and no statement sent after it: not a narrower read, and so
        # not PostgreSQL's "current transaction is aborted" either.
        refusing.assert_raised_as_it_was(caught)
        assert ended(queries) == "ROLLBACK"
        assert left_as_found()
        # No row and no tick was put down as one that does not read.
        assert named_unreadable(caplog) == []
        # Rolled back by the request that owned it, the connection reads on.
        assert client.get(reverse(CHANGELIST)).status_code == 200

    def test_nor_is_it_taken_for_a_row_that_cannot_keep_its_form(
        self, client, admin_user, caplog
    ):
        # The read that decides whether a row holding such a value keeps its
        # change form is Django's own read of the row, in the request's
        # transaction like the rest.
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
        client.force_login(admin_user)
        refusing = Refusing(KEEPS_ITS_FORM)

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(DataError) as caught,
            connection.execute_wrapper(refusing),
        ):
            change_page(client, row)

        refusing.assert_raised_as_it_was(caught)
        assert ended(queries) == "ROLLBACK"
        assert left_as_found()

    def test_what_the_request_had_written_goes_back_with_it(self, client, admin_user):
        first, second = a_schedule("first"), a_schedule("second")
        told = marker()
        client.force_login(admin_user)
        # The page's rows, the selection, the first row read under its lock,
        # and then that row read back, once its pause is written and the
        # block it was written in has ended.
        refusing = Refusing(ROWS, nth=4)

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(DataError) as caught,
            connection.execute_wrapper(refusing),
        ):
            disable(client, first, second)

        refusing.assert_raised_as_it_was(caught)
        written = [q["sql"] for q in queries.captured_queries if "UPDATE" in q["sql"]]
        assert any('"enabled"' in sql or "`enabled`" in sql for sql in written), (
            "the first row was never paused, so there was nothing to take back"
        )
        assert ended(queries) == "ROLLBACK"
        assert left_as_found()
        assert not paused(first.pk)
        assert not paused(second.pk)
        assert marker() == told, "the workers were told of a pause that was undone"
        assert not LogEntry.objects.exists()

    def test_a_read_refused_inside_a_write_takes_the_request_with_it_too(
        self, client, admin_user
    ):
        first, second = a_schedule("first"), a_schedule("second")
        told = marker()
        client.force_login(admin_user)
        # One further: the second row's read under its lock, which the write
        # makes in a block of its own inside the request's transaction.
        refusing = Refusing(ROWS, nth=5)

        with (
            CaptureQueriesContext(connection) as queries,
            pytest.raises(DataError) as caught,
            connection.execute_wrapper(refusing),
        ):
            disable(client, first, second)

        assert type(caught.value) is DataError
        assert Refusing.SAYS[connection.vendor] in str(caught.value)
        # Nothing follows it but that block being rolled back on the way out.
        assert [
            sql
            for sql in refusing.after
            if not sql.startswith(("ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT"))
        ] == []
        assert ended(queries) == "ROLLBACK"
        assert left_as_found()
        assert not paused(first.pk)
        assert not paused(second.pk)
        assert marker() == told


# -- a stored value that does not read ----------------------------------------


class TestAValueThatDoesNotRead:
    @pytest.mark.parametrize(("vendor", "column", "literal"), UNREADABLE_ROWS)
    def test_the_changelist_lists_it_in_its_place(
        self, client, admin_user, vendor, column, literal
    ):
        a_schedule("healthy")
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        page = body(response)
        assert ">healthy</a>" in page
        assert label(pk, column) in page
        assert f"Stored values django-ox cannot use: {column}" in page
        assert ended(queries) == "COMMIT"
        assert left_as_found()

    def test_a_tick_that_does_not_read_is_shown_as_one(self, client, admin_user):
        broken = a_schedule("broken")
        ran = a_schedule("ran")
        at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        a_tick(broken, at)
        a_tick(ran, at)
        literal = {
            "postgresql": "'infinity'",
            "mysql": "'9999-00-00 00:00:00'",
            # Text SQLite's driver cannot hand over at all, which fails the
            # whole statement and is found by reading again in halves.
            "sqlite": "CAST(X'62616480FF' AS TEXT)",
        }[connection.vendor]
        insert_tick(f"db:{broken.pk}", literal)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        page = body(response)
        assert page.count(">Unreadable tick history<") == 1
        assert f">{at:%Y-%m-%d %H:%M}<" in page
        assert ended(queries) == "COMMIT"
        assert left_as_found()

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_its_change_page_is_the_recovery_page(
        self, client, admin_user, vendor, column, literal
    ):
        # Among these are the rows whose every such value the form would
        # replace, which Django's own read is then tried on, and raises on.
        # That read failed in this process too, and the page is drawn in the
        # transaction it failed in.
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGE, args=[pk]))

        assert response.status_code == 200, response.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        assert f"<strong>{column}</strong>" in body(response)
        assert ended(queries) == "COMMIT"
        assert left_as_found()

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_it_is_deleted_from_its_delete_page_and_the_deletion_commits(
        self, client, admin_user, vendor, column, literal
    ):
        healthy = a_schedule("healthy")
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})

        assert response.status_code == 302
        assert ended(queries) == "COMMIT"
        assert left_as_found()
        assert held(pk, "id") is None
        assert held(healthy.pk, "id") is not None

    @pytest.mark.parametrize(("vendor", "column", "literal"), UNREADABLE_ROWS)
    def test_disable_pauses_it_beside_a_row_that_reads_and_both_commit(
        self, client, admin_user, vendor, column, literal
    ):
        healthy = a_schedule("healthy")
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.post(
                reverse(CHANGELIST),
                {
                    "action": "disable_selected",
                    "_selected_action": [str(healthy.pk), str(pk)],
                },
            )

        assert response.status_code == 302
        assert ended(queries) == "COMMIT"
        assert left_as_found()
        assert paused(healthy.pk)
        assert paused(pk)

    @pytest.mark.parametrize(
        ("vendor", "column", "literal", "given", "saved"), SHOWN_AS_STORED
    )
    def test_a_row_that_keeps_its_form_is_repaired_by_saving_it(
        self, client, admin_user, vendor, column, literal, given, saved
    ):
        row = shown_as_stored(vendor, column, literal)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            opened = client.get(reverse(CHANGE, args=[row.pk]))

        assert opened.status_code == 200, opened.get("Location")
        assert CHANGE_FORM in [t.name for t in opened.templates]
        assert inputs(opened, column) == literal.strip("'")
        assert any("cannot be read" in m for m in messages_of(opened))
        assert ended(queries) == "COMMIT"

        with CaptureQueriesContext(connection) as queries:
            response = client.post(
                reverse(CHANGE, args=[row.pk]), resubmission(opened, **given)
            )

        assert response.status_code == 302, errors_of(response)
        assert ended(queries) == "COMMIT"
        assert left_as_found()
        assert getattr(OxSchedule.objects.get(pk=row.pk), column) == saved


class TestASaveTheWriteRefuses:
    def test_the_form_comes_back_and_what_the_request_wrote_is_undone(
        self, client, admin_user, monkeypatch
    ):
        row = a_schedule("nightly")
        beside = a_schedule("beside")
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE, args=[row.pk]))
        submission = resubmission(
            opened,
            name="renamed",
            every_seconds="30",
            **in_the_form(timezone.now() + timedelta(minutes=30)),
        )
        # An hour passes between the form's check and the write, and the
        # request writes something of its own in it.
        the_write_comes_later(
            monkeypatch,
            first=lambda: OxSchedule.objects.filter(pk=beside.pk).update(
                name="written-by-the-request"
            ),
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.post(reverse(CHANGE, args=[row.pk]), submission)

        assert response.status_code == 200
        assert CHANGE_FORM in [t.name for t in response.templates]
        assert errors_of(response) == THE_FORM_SAYS
        assert inputs(response, "name") == "renamed"
        # The request's transaction is not lost with the save: the block
        # Django runs a submission in is what was rolled back, and the form
        # is drawn in the transaction that is left.
        assert ended(queries) == "COMMIT"
        assert left_as_found()
        after = OxSchedule.objects.get(pk=row.pk)
        assert (after.name, after.every_seconds) == ("nightly", 60)
        assert OxSchedule.objects.get(pk=beside.pk).name == "beside"
        assert not LogEntry.objects.exists()

    @pytest.mark.parametrize(("vendor", "column", "literal"), NOT_REPLACED_BY_THE_FORM)
    def test_a_value_the_form_does_not_replace_is_an_error_on_the_form(
        self, client, admin_user, monkeypatch, vendor, column, literal
    ):
        only_on(vendor)
        row = a_schedule("nightly")
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE, args=[row.pk]))
        submission = resubmission(opened, name="renamed")
        another_session_meanwhile(
            monkeypatch, lambda: set_column(row.pk, column, literal)
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.post(reverse(CHANGE, args=[row.pk]), submission)

        assert response.status_code == 200
        assert CHANGE_FORM in [t.name for t in response.templates]
        (said,) = errors_of(response)["__all__"]
        assert re.fullmatch(
            r"Nothing was saved\. This schedule holds a stored value django-ox "
            r"cannot use, which this form does not replace: "
            rf"{column} holds .+\. You can still disable or delete the schedule\.",
            said,
        ), said
        assert ended(queries) == "COMMIT"
        assert left_as_found()
        assert held(row.pk, "name") == "nightly"
        assert not LogEntry.objects.exists()


@pytest.fixture
def reading_a_snapshot():
    """
    This session at REPEATABLE READ, where a transaction goes on reading
    the rows as they were when it first read, and put back afterwards.
    Django's default for MySQL is READ COMMITTED; MySQL's own is this.
    """
    only_on("mysql")
    with connection.cursor() as cursor:
        cursor.execute("SELECT @@SESSION.transaction_isolation")
        (level,) = cursor.fetchone()
        cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    yield
    with connection.cursor() as cursor:
        cursor.execute(
            f"SET SESSION TRANSACTION ISOLATION LEVEL {level.replace('-', ' ')}"
        )


class TestARowDeletedBeforeTheSaveWhereTheRequestReadsASnapshot:
    def test_it_is_djangos_does_not_exist(
        self, client, admin_user, monkeypatch, reading_a_snapshot
    ):
        """
        The request's one transaction still shows the row another session
        deleted, so drawing the form again finds it again; the save's own
        read, which locks and is current, does not. That is said as a row
        that does not exist, not raised.
        """
        row = a_schedule("nightly")
        client.force_login(admin_user)
        opened = client.get(reverse(CHANGE, args=[row.pk]))
        submission = resubmission(opened, name="renamed")
        another_session_meanwhile(
            monkeypatch,
            lambda: OxSchedule.objects.filter(pk=row.pk).delete(),
        )

        with CaptureQueriesContext(connection) as queries:
            response = client.post(reverse(CHANGE, args=[row.pk]), submission)

        assert response.status_code == 302
        assert response["Location"] == reverse("admin:index")
        assert ended(queries) == "COMMIT"
        assert left_as_found()
        shown = client.get(response["Location"])
        assert any("doesn\u2019t exist" in m for m in messages_of(shown))
        assert not OxSchedule.objects.filter(pk=row.pk).exists()
        assert not LogEntry.objects.exists()
