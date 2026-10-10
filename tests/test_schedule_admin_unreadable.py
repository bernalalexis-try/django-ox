"""
The schedule admin when a stored value does not read.

A stored schedule written around django_ox.stored, by SQL, a fixture or a
data migration, can hold a value its column accepts and Django cannot read
back: PostgreSQL keeps year 10000 and 'infinity', SQLite keeps a date that
does not exist and text that is not UTF-8, MySQL keeps zero dates and an
`enabled` of 2. On SQLite some values read as something else instead: a
start of 'banana' reads as no start at all, an `enabled` of 2 as False. The
same goes for the tick log, whose newest tick the changelist shows.

One such row must not cost the person the page. The changelist lists every
other row, and lists this one in its place, saying which field did not
read; its change, history and delete pages say what is wrong and how to
repair it, rather than raising or reporting a row that exists as gone; and
a tick that does not read is shown as such, never as "never". Where every
value that does not read can be shown as it is stored, in a field the change
form writes back, the row keeps its form and its other pages, and those rows
are in tests/test_schedule_admin_save.py.

Times written through the API can fail a different way: an instant the
database holds can lie outside the years the requested zone can show, and
those are shown, and edited, in UTC.

Every value below is written by SQL, the only way it can be written, in the
form the engine that keeps it keeps it. A test for another engine's value
skips and says why.
"""

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission, User
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import django_ox.admin
from django_ox.models import OxSchedule, OxScheduleChange, OxScheduleTick, OxTask
from django_ox.registry import ScheduleKind, register
from django_ox.stored import create_schedule

from . import tasks

SCHEDULES = "django_ox_oxschedule"
TICKS = "django_ox_oxscheduletick"

CHANGELIST = "admin:django_ox_oxschedule_changelist"
CHANGE = "admin:django_ox_oxschedule_change"
DELETE = "admin:django_ox_oxschedule_delete"
HISTORY = "admin:django_ox_oxschedule_history"
UNREADABLE_PAGE = "admin/django_ox/oxschedule/unreadable.html"


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.add))
    register(
        ScheduleKind(key="restricted", task=tasks.add, permission="auth.view_user")
    )


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """
    The clock the admin's reports are kept by, starting afresh. It reports a
    row it keeps meeting once a minute, per process, and a rolled-back test
    can leave the next one a row with the same key.
    """
    now = [1000.0]
    if hasattr(django_ox.admin, "_Reports"):
        reports = django_ox.admin._Reports(clock=lambda: now[0])
        monkeypatch.setattr(django_ox.admin, "_reports", reports)
    return now


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


def staff(*codenames, username="staff"):
    user = User.objects.create_user(
        username, f"{username}@example.com", "pw", is_staff=True
    )
    for codename in codenames:
        user.user_permissions.add(Permission.objects.get(codename=codename))
    return User.objects.get(pk=user.pk)


EVERY_PERMISSION = (
    "view_oxschedule",
    "add_oxschedule",
    "change_oxschedule",
    "delete_oxschedule",
)


def a_schedule(name, **over):
    fields = {
        "name": name,
        "task_key": "report",
        "trigger": "interval",
        "every_seconds": 60,
        "start_time": timezone.now() - timedelta(days=1),
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


def insert_tick(key, literal):
    created = {"postgresql": "now()", "mysql": "NOW(6)"}.get(
        connection.vendor, "'2026-10-05 00:00:00'"
    )
    by_sql(
        f"INSERT INTO {TICKS} (schedule_name, scheduled_for, task_id, created_at) "  # noqa: S608
        f"VALUES (%s, {literal}, NULL, {created})",
        [key],
    )


def a_tick(schedule, at):
    OxScheduleTick.objects.create(
        schedule_name=f"db:{schedule.pk}", scheduled_for=at, created_at=at
    )


def only_on(vendor, why=""):
    if connection.vendor != vendor:
        pytest.skip(f"a {vendor} value{why}; this run is on {connection.vendor}")


def body(response):
    return response.content.decode()


def messages_of(response):
    return [str(m) for m in response.context["messages"]]


#: (engine, column, SQL literal): a value each engine keeps that Django's own
#: read raises on, or reads as something else.
UNREADABLE_ROWS = [
    pytest.param(
        "sqlite", "end_time", "'2026-02-30 00:00:00'", id="sqlite-end-does-not-exist"
    ),
    pytest.param(
        "sqlite", "end_time", "'10000-01-01 00:00:00'", id="sqlite-end-year-10000"
    ),
    pytest.param("sqlite", "start_time", "'banana'", id="sqlite-start-banana"),
    pytest.param("sqlite", "every_seconds", "'abc'", id="sqlite-interval-text"),
    pytest.param("sqlite", "enabled", "2", id="sqlite-enabled-2"),
    pytest.param("sqlite", "enabled", "'abc'", id="sqlite-enabled-text"),
    pytest.param(
        "sqlite", "name", "CAST(X'62616480FF' AS TEXT)", id="sqlite-name-not-utf-8"
    ),
    pytest.param("sqlite", "name", "X'626c6f622d6e616d65'", id="sqlite-name-bytes"),
    pytest.param("sqlite", "task_key", "X'6f70656e2e74'", id="sqlite-task-key-bytes"),
    pytest.param(
        "postgresql",
        "end_time",
        "'10000-01-01 00:00:00+00'",
        id="postgresql-end-year-10000",
    ),
    pytest.param("postgresql", "end_time", "'infinity'", id="postgresql-end-infinity"),
    pytest.param(
        "postgresql", "start_time", "'-infinity'", id="postgresql-start-minus-infinity"
    ),
    pytest.param(
        "postgresql",
        "updated_at",
        "'10000-01-01 00:00:00+00'",
        id="postgresql-updated-year-10000",
    ),
    pytest.param(
        "mysql", "end_time", "'0000-00-00 00:00:00'", id="mysql-end-zero-date"
    ),
    pytest.param(
        "mysql", "start_time", "'2026-00-10 00:00:00'", id="mysql-start-zero-month"
    ),
    pytest.param("mysql", "enabled", "2", id="mysql-enabled-2"),
]

#: The values above that leave a row its change form. Each sits in a field
#: the form's save replaces, and Django's own read hands it over as it is
#: stored, so the form can show it and saving the form repairs it. Those rows
#: are in tests/test_schedule_admin_save.py.
KEEP_THEIR_FORM = {"sqlite-interval-text", "mysql-enabled-2"}

#: The rest, whose change, history and delete pages are the recovery ones.
RECOVERY_ROWS = [row for row in UNREADABLE_ROWS if row.id not in KEEP_THEIR_FORM]

#: The fields any change through django_ox.stored writes afresh. A row that
#: does not read in one of them is repaired by a change to anything.
WRITTEN_BY_ANY_CHANGE = {"start_time", "updated_at"}

#: One value per engine, for the tests that need only one.
ONE_PER_ENGINE = {
    "sqlite": ("end_time", "'2026-02-30 00:00:00'"),
    "postgresql": ("end_time", "'10000-01-01 00:00:00+00'"),
    "mysql": ("end_time", "'0000-00-00 00:00:00'"),
}


def an_unreadable_schedule(name="bad", vendor=None, column=None, literal=None, **over):
    """A schedule written by the API, then given a value that does not read."""
    if column is None:
        column, literal = ONE_PER_ENGINE[connection.vendor]
    elif vendor is not None:
        only_on(vendor)
    row = a_schedule(name, **over)
    set_column(row.pk, column, literal)
    return row.pk, column


def label(pk, column, name="bad"):
    return f"pk {pk}" if column == "name" else f"{name} (pk {pk})"


class TestAStoredValueThatDoesNotRead:
    @pytest.mark.parametrize(("vendor", "column", "literal"), UNREADABLE_ROWS)
    def test_the_changelist_lists_it_beside_the_rows_that_read(
        self, client, admin_user, vendor, column, literal
    ):
        a_schedule("healthy")
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        page = body(response)
        assert ">healthy</a>" in page, "the row that reads is not listed"
        assert label(pk, column) in page, "the row that does not read is not listed"
        assert f"Stored values django-ox cannot use: {column}" in page
        cl = response.context["cl"]
        assert cl.result_count == 2
        assert len(cl.result_list) == 2
        # Selectable by its key, so an action can still be applied to it.
        assert f'name="_selected_action" value="{pk}"' in page

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_its_change_page_says_what_did_not_read_and_how_to_repair_it(
        self, client, admin_user, vendor, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[pk]))
        # Not a server error, and not "does not exist" either: SQLite's
        # converter raises ValueError on a date that does not exist, and
        # Django's own lookup reads that as a row that is not there.
        assert response.status_code == 200, response.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        page = " ".join(body(response).split())
        assert f"<strong>{column}</strong>" in page
        if column in WRITTEN_BY_ANY_CHANGE:
            # No value of its own to pass: update_schedule does not take it.
            assert f"update_schedule(OxSchedule(pk={pk}), name=...)" in page
            assert f"writes {column} afresh" in page
            assert f"{column}=..." not in page
        else:
            assert f"update_schedule(OxSchedule(pk={pk}), {column}=...)" in page
        assert "adminform" not in response.context, "the edit form was offered"

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_a_post_to_its_change_page_writes_nothing(
        self, client, admin_user, vendor, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        response = client.post(
            reverse(CHANGE, args=[pk]),
            {
                "name": "renamed",
                "task_key": "report",
                "trigger": "interval",
                "every_seconds": 30,
                "phase_seconds": 0,
                "arguments": "{}",
                "enabled": "on",
            },
        )
        assert response.status_code == 200
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        assert not OxSchedule.objects.filter(name="renamed").exists()
        assert not LogEntry.objects.exists()

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_its_history_page_is_the_same_page(
        self, client, admin_user, vendor, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        response = client.get(reverse(HISTORY, args=[pk]))
        assert response.status_code == 200, response.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in response.templates]

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_it_can_be_deleted_from_its_delete_page(
        self, client, admin_user, vendor, column, literal
    ):
        healthy = a_schedule("healthy")
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        confirm = client.get(reverse(DELETE, args=[pk]))
        assert confirm.status_code == 200, confirm.get("Location")
        assert "admin/delete_confirmation.html" in [t.name for t in confirm.templates]
        assert label(pk, column) in body(confirm)
        assert any("django-ox cannot use" in m for m in messages_of(confirm))
        before = OxScheduleChange.objects.get(id=1).changed_at
        response = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})
        assert response.status_code == 302
        assert response["Location"] == reverse(CHANGELIST)
        assert not OxSchedule.objects.filter(pk=pk).exists()
        assert OxSchedule.objects.filter(pk=healthy.pk).exists()
        # The workers are told, as by any other deletion here.
        assert OxScheduleChange.objects.get(id=1).changed_at > before
        entry = LogEntry.objects.get()
        assert entry.action_flag == DELETION
        assert entry.object_id == str(pk)
        assert entry.object_repr == label(pk, column)


class TestAPageAskedForByAFieldOfTheRow:
    """
    Django's change and delete pages take `_to_field`, the field a row is
    looked up by, and always allow the primary key for it: it is what a
    link from a related object's form, and a popup, send. The row behind it
    is the same row, and its pages have to be the same pages.
    """

    BY_ITS_KEY = {"_to_field": "id"}

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_the_change_page_is_the_recovery_page(
        self, client, admin_user, vendor, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[pk]), self.BY_ITS_KEY)
        assert response.status_code == 200, response.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        assert f"<strong>{column}</strong>" in body(response)
        assert "adminform" not in response.context, "the edit form was offered"

    @pytest.mark.parametrize(
        ("vendor", "column", "literal"),
        [row for row in UNREADABLE_ROWS if row.id in KEEP_THEIR_FORM],
    )
    def test_a_row_that_keeps_its_form_is_still_said_not_to_read(
        self, client, admin_user, vendor, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[pk]), self.BY_ITS_KEY)
        assert response.status_code == 200, response.get("Location")
        assert "adminform" in response.context
        (said,) = [m for m in messages_of(response) if "cannot be read" in m]
        assert column in said
        assert "Saving this form replaces it" in said

    @pytest.mark.parametrize(("vendor", "column", "literal"), RECOVERY_ROWS)
    def test_the_delete_page_deletes_it(
        self, client, admin_user, vendor, column, literal
    ):
        healthy = a_schedule("healthy")
        pk, column = an_unreadable_schedule("bad", vendor, column, literal)
        client.force_login(admin_user)
        by_its_key = f"{reverse(DELETE, args=[pk])}?_to_field=id"
        confirm = client.get(by_its_key)
        assert confirm.status_code == 200, confirm.get("Location")
        assert "admin/delete_confirmation.html" in [t.name for t in confirm.templates]
        assert label(pk, column) in body(confirm)
        assert any("django-ox cannot use" in m for m in messages_of(confirm))
        response = client.post(by_its_key, {"post": "yes"})
        assert response.status_code == 302
        assert not OxSchedule.objects.filter(pk=pk).exists()
        assert OxSchedule.objects.filter(pk=healthy.pk).exists()

    def test_a_row_that_reads_has_its_ordinary_form(self, client, admin_user):
        row = a_schedule("healthy")
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[row.pk]), self.BY_ITS_KEY)
        assert response.status_code == 200
        assert response.context["original"] == row
        assert "admin/change_form.html" in [t.name for t in response.templates]

    def test_a_key_with_no_row_is_djangos_does_not_exist(self, client, admin_user):
        client.force_login(admin_user)
        for key in (987654, "nope"):
            response = client.get(
                reverse(CHANGE, args=[key]), self.BY_ITS_KEY, follow=True
            )
            assert response.redirect_chain[0][1] == 302
            assert any("doesn\u2019t exist" in m for m in messages_of(response))


#: The MySQL dates above. With USE_TZ off the driver hands each over as text.
ZERO_DATES = [
    pytest.param("end_time", "'0000-00-00 00:00:00'", id="end-zero-date"),
    pytest.param("start_time", "'2026-00-10 00:00:00'", id="start-zero-month"),
]


class TestAZeroDateThatDjangoHandsOverAsText:
    """
    With USE_TZ off, Django's own read of a MySQL zero date raises nothing.
    The driver cannot make a datetime of it and hands over the text, and no
    converter of Django's looks at it. The row does not read all the same,
    and a form has no way to draw text where a date and time belongs, so its
    pages are the recovery ones, as they are with USE_TZ on.
    """

    @pytest.fixture(autouse=True)
    def _use_tz_off(self, settings):
        only_on("mysql", ", and only its driver hands one over as text")
        settings.USE_TZ = False

    @pytest.mark.parametrize(("column", "literal"), ZERO_DATES)
    def test_djangos_own_read_raises_nothing(self, admin_user, column, literal):
        pk, column = an_unreadable_schedule("bad", "mysql", column, literal)
        read = OxSchedule.objects.values_list(column, flat=True).get(pk=pk)
        assert isinstance(read, str)

    @pytest.mark.parametrize(("column", "literal"), ZERO_DATES)
    @pytest.mark.parametrize(
        "page",
        [pytest.param(CHANGE, id="change"), pytest.param(HISTORY, id="history")],
    )
    def test_its_change_and_history_pages_are_the_recovery_page(
        self, client, admin_user, page, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", "mysql", column, literal)
        client.force_login(admin_user)
        response = client.get(reverse(page, args=[pk]))
        assert response.status_code == 200, response.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        assert f"<strong>{column}</strong>" in body(response)
        assert "adminform" not in response.context, "the edit form was offered"

    @pytest.mark.parametrize(("column", "literal"), ZERO_DATES)
    def test_it_can_be_deleted_from_its_delete_page(
        self, client, admin_user, column, literal
    ):
        pk, column = an_unreadable_schedule("bad", "mysql", column, literal)
        client.force_login(admin_user)
        confirm = client.get(reverse(DELETE, args=[pk]))
        assert confirm.status_code == 200
        assert label(pk, column) in body(confirm)
        assert any("django-ox cannot use" in m for m in messages_of(confirm))
        response = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})
        assert response.status_code == 302
        assert not OxSchedule.objects.filter(pk=pk).exists()


class TestAnAbsentRowIsStillAbsent:
    def test_its_change_page_is_djangos_does_not_exist(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[987654]), follow=True)
        assert response.redirect_chain[0][1] == 302
        assert any("doesn\u2019t exist" in m for m in messages_of(response))

    def test_its_delete_page_is_djangos_does_not_exist(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse(DELETE, args=[987654]), follow=True)
        assert response.redirect_chain[0][1] == 302
        assert any("doesn\u2019t exist" in m for m in messages_of(response))

    def test_a_key_that_is_not_a_number_is_djangos_does_not_exist(
        self, client, admin_user
    ):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=["nope"]), follow=True)
        assert response.redirect_chain[0][1] == 302


@pytest.mark.django_db
class TestWhoMaySeeAndActOnAnUnreadableRow:
    def _page(self, client, user, pk):
        client.force_login(user)
        return client.get(reverse(CHANGE, args=[pk]))

    def test_without_view_or_change_permission_it_is_refused(self, client):
        pk, _ = an_unreadable_schedule()
        response = self._page(client, staff("delete_oxschedule"), pk)
        assert response.status_code == 403

    def test_a_viewer_sees_it_and_is_offered_neither_action(self, client):
        pk, _ = an_unreadable_schedule()
        response = self._page(client, staff("view_oxschedule"), pk)
        assert response.status_code == 200
        assert not response.context["can_disable"]
        assert not response.context["can_delete"]
        page = body(response)
        assert 'value="disable_selected"' not in page
        assert "deletelink" not in page
        assert client.get(reverse(DELETE, args=[pk])).status_code == 403

    def test_a_viewer_cannot_submit_to_it(self, client):
        pk, _ = an_unreadable_schedule()
        client.force_login(staff("view_oxschedule"))
        response = client.post(reverse(CHANGE, args=[pk]), {"name": "renamed"})
        assert response.status_code == 403

    def test_disable_and_delete_are_offered_to_those_allowed_them(self, client):
        pk, _ = an_unreadable_schedule()
        response = self._page(client, staff(*EVERY_PERMISSION), pk)
        assert response.context["can_disable"]
        assert response.context["can_delete"]
        page = body(response)
        # Disable is the changelist's own action, applied to this row by key.
        assert re.search(r'<form method="post" action="[^"]*/oxschedule/">', page), (
            "Disable does not post to the changelist"
        )
        assert 'name="action" value="disable_selected"' in page
        assert 'name="index" value="0"' in page
        assert f'name="_selected_action" value="{pk}"' in page
        assert reverse(DELETE, args=[pk]) in page

    def test_a_registry_permission_still_applies(self, client):
        pk, _ = an_unreadable_schedule(task_key="restricted")
        user = staff(*EVERY_PERMISSION)
        response = self._page(client, user, pk)
        assert response.status_code == 200
        assert not response.context["can_disable"]
        assert not response.context["can_delete"]
        assert client.get(reverse(DELETE, args=[pk])).status_code == 403
        assert (
            client.post(reverse(DELETE, args=[pk]), {"post": "yes"}).status_code == 403
        )
        assert OxSchedule.objects.filter(pk=pk).exists()
        user.user_permissions.add(Permission.objects.get(codename="view_user"))
        response = self._page(client, User.objects.get(pk=user.pk), pk)
        assert response.context["can_disable"]
        assert response.context["can_delete"]

    def test_an_unreadable_task_key_leaves_it_to_a_superuser(self, client):
        only_on("sqlite", ": text that is not UTF-8")
        row = a_schedule("bad")
        set_column(row.pk, "task_key", "CAST(X'72657080FF' AS TEXT)")
        user = staff(*EVERY_PERMISSION, "view_user")
        response = self._page(client, user, row.pk)
        assert response.status_code == 200
        assert "<strong>task_key</strong>" in body(response)
        assert not response.context["can_delete"]
        assert (
            client.post(reverse(DELETE, args=[row.pk]), {"post": "yes"}).status_code
            == 403
        )
        root = User.objects.create_superuser("root", "root@example.com", "pw")
        response = self._page(client, root, row.pk)
        assert response.context["can_delete"]
        assert (
            client.post(reverse(DELETE, args=[row.pk]), {"post": "yes"}).status_code
            == 302
        )
        assert not OxSchedule.objects.filter(pk=row.pk).exists()


@pytest.mark.django_db
class TestAKeyOfAThousandOrMore:
    """
    With USE_THOUSAND_SEPARATOR on, Django writes an integer into a template
    with the locale's separators, and 1,234 names no row. The page names the
    row by its primary key in the calls it prints and in its Disable button,
    so there the key is written as a key.
    """

    PK = 1234

    @pytest.fixture
    def column(self, settings):
        settings.USE_THOUSAND_SEPARATOR = True
        pk, column = an_unreadable_schedule()
        by_sql(f"UPDATE {SCHEDULES} SET id = {self.PK} WHERE id = {int(pk)}")  # noqa: S608
        return column

    def test_the_calls_it_prints_name_the_row(self, client, admin_user, column):
        client.force_login(admin_user)
        page = body(client.get(reverse(CHANGE, args=[self.PK])))
        assert f"update_schedule(OxSchedule(pk={self.PK}), {column}=...)" in page
        assert f"update_schedule(OxSchedule(pk={self.PK}), enabled=False)" in page
        assert "1,234" not in page

    def test_its_disable_button_pauses_the_row(self, client, admin_user, column):
        client.force_login(admin_user)
        page = body(client.get(reverse(CHANGE, args=[self.PK])))
        target, form = re.search(
            r'<form method="post" action="([^"]*)">(.*?)</form>', page, re.DOTALL
        ).groups()
        drawn = dict(
            re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form)
        )
        assert drawn["_selected_action"] == str(self.PK)
        response = client.post(target, drawn)
        assert response.status_code == 302
        row = OxSchedule.objects.filter(pk=self.PK)
        assert row.values_list("enabled", flat=True).get() is False


@pytest.mark.django_db
class TestFindingTheRowsStillWorks:
    """
    The page's rows are chosen by Django's own search, filters, ordering and
    pagination, and only then read; a row that does not read keeps its
    place and its count.
    """

    @pytest.fixture
    def three(self):
        a_schedule("alpha")
        a_schedule("bravo")
        pk, column = an_unreadable_schedule("charlie")
        return pk, column

    def test_search(self, client, admin_user, three):
        pk, column = three
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST), {"q": "charl"})
        assert response.context["cl"].result_count == 1
        assert label(pk, column, "charlie") in body(response)

    def test_filter(self, client, admin_user, three):
        pk, column = three
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST), {"enabled__exact": "1"})
        assert response.context["cl"].result_count == 3
        assert label(pk, column, "charlie") in body(response)

    def test_pages_and_order(self, client, admin_user, three, monkeypatch):
        pk, column = three
        monkeypatch.setattr(site._registry[OxSchedule], "list_per_page", 2)
        client.force_login(admin_user)
        first = client.get(reverse(CHANGELIST))
        second = client.get(reverse(CHANGELIST), {"p": "2"})
        assert first.context["cl"].result_count == 3
        assert [str(r) for r in first.context["cl"].result_list] == ["alpha", "bravo"]
        assert [str(r) for r in second.context["cl"].result_list] == [
            label(pk, column, "charlie")
        ]
        # Name, descending: the first column after the selection box.
        backwards = client.get(reverse(CHANGELIST), {"o": "-1"})
        assert [str(r) for r in backwards.context["cl"].result_list] == [
            label(pk, column, "charlie"),
            "bravo",
        ]


#: (engine, SQL literal): a tick each engine keeps that Django's read of the
#: newest tick raises on or misreads, and that sorts newest.
UNREADABLE_NEWEST_TICKS = [
    pytest.param("postgresql", "'10000-01-01 00:00:00+00'", id="postgresql-year-10000"),
    pytest.param("postgresql", "'infinity'", id="postgresql-infinity"),
    pytest.param("sqlite", "'banana'", id="sqlite-banana"),
    pytest.param("sqlite", "'9999-02-30 00:00:00'", id="sqlite-does-not-exist"),
    pytest.param("mysql", "'9999-00-00 00:00:00'", id="mysql-zero-month"),
]

#: The same for a tick that sorts oldest, which is the newest when it is the
#: schedule's only one.
UNREADABLE_ONLY_TICKS = [
    pytest.param("postgresql", "'-infinity'", id="postgresql-minus-infinity"),
    pytest.param("sqlite", "'0000-02-30 00:00:00'", id="sqlite-year-zero"),
    pytest.param("mysql", "'0000-00-00 00:00:00'", id="mysql-zero-date"),
]


@pytest.mark.django_db
class TestATickThatDoesNotRead:
    def _page(self, client, admin_user):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        return response

    def _cells(self, response):
        return {
            str(row): django_ox.admin.OxScheduleAdmin.last_tick(None, row)
            for row in response.context["cl"].result_list
        }

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_NEWEST_TICKS)
    def test_is_shown_as_unreadable_and_the_other_rows_as_they_are(
        self, client, admin_user, vendor, literal
    ):
        only_on(vendor)
        broken = a_schedule("broken")
        ran = a_schedule("ran")
        a_schedule("quiet")
        at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        a_tick(broken, at)
        a_tick(ran, at)
        insert_tick(f"db:{broken.pk}", literal)
        response = self._page(client, admin_user)
        page = body(response)
        assert ">Unreadable tick history<" in page
        assert f"{at:%Y-%m-%d %H:%M}" in page
        assert ">never<" in page
        cells = self._cells(response)
        assert cells == {
            "broken": "Unreadable tick history",
            "quiet": "never",
            "ran": f"{at:%Y-%m-%d %H:%M}",
        }

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_ONLY_TICKS)
    def test_an_only_tick_that_does_not_read_is_not_never(
        self, client, admin_user, vendor, literal
    ):
        only_on(vendor)
        broken = a_schedule("broken")
        insert_tick(f"db:{broken.pk}", literal)
        cells = self._cells(self._page(client, admin_user))
        assert cells == {"broken": "Unreadable tick history"}

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_ONLY_TICKS)
    def test_an_old_tick_that_does_not_read_is_not_the_newest(
        self, client, admin_user, vendor, literal
    ):
        only_on(vendor)
        ran = a_schedule("ran")
        at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        insert_tick(f"db:{ran.pk}", literal)
        a_tick(ran, at)
        cells = self._cells(self._page(client, admin_user))
        assert cells == {"ran": f"{at:%Y-%m-%d %H:%M}"}

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_NEWEST_TICKS)
    def test_the_schedule_can_still_be_opened_and_deleted(
        self, client, admin_user, vendor, literal
    ):
        only_on(vendor)
        broken = a_schedule("broken")
        insert_tick(f"db:{broken.pk}", literal)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[broken.pk]))
        assert response.status_code == 200, response.get("Location")
        assert "adminform" in response.context, "the edit form was not offered"
        (warning,) = [m for m in messages_of(response) if "tick history" in m]
        assert f"--schedule-key db:{broken.pk} with --dry-run first." in warning
        confirm = client.get(reverse(DELETE, args=[broken.pk]))
        assert confirm.status_code == 200
        deleted = client.post(reverse(DELETE, args=[broken.pk]), {"post": "yes"})
        assert deleted.status_code == 302
        assert not OxSchedule.objects.filter(pk=broken.pk).exists()

    @pytest.mark.parametrize(("vendor", "literal"), UNREADABLE_NEWEST_TICKS)
    def test_the_actions_still_reach_the_schedule(
        self, client, admin_user, vendor, literal
    ):
        only_on(vendor)
        broken = a_schedule("broken")
        insert_tick(f"db:{broken.pk}", literal)
        client.force_login(admin_user)

        def act(action, **more):
            return client.post(
                reverse(CHANGELIST),
                {"action": action, "_selected_action": [str(broken.pk)], **more},
            )

        assert act("disable_selected").status_code == 302
        assert not OxSchedule.objects.get(pk=broken.pk).enabled
        assert act("enable_selected").status_code == 302
        assert OxSchedule.objects.get(pk=broken.pk).enabled
        assert act("run_once_now").status_code == 302
        assert OxTask.objects.count() == 1
        assert act("delete_selected").status_code == 200
        assert act("delete_selected", post="yes").status_code == 302
        assert not OxSchedule.objects.filter(pk=broken.pk).exists()

    def test_a_readable_tick_draws_no_warning_on_the_change_form(
        self, client, admin_user
    ):
        ran = a_schedule("ran")
        a_tick(ran, timezone.now() - timedelta(hours=1))
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[ran.pk]))
        assert not any("tick history" in m for m in messages_of(response))


@pytest.mark.django_db
class TestAnEnabledValueThatIsNeitherTrueNorFalse:
    @pytest.mark.parametrize(
        ("vendor", "literal"),
        [
            pytest.param("mysql", "2", id="mysql-2"),
            pytest.param("sqlite", "2", id="sqlite-2"),
            pytest.param("sqlite", "'abc'", id="sqlite-text"),
        ],
    )
    def test_is_marked_rather_than_drawn_as_a_boolean(
        self, client, admin_user, vendor, literal
    ):
        a_schedule("healthy")
        an_unreadable_schedule("bad", vendor, "enabled", literal)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        page = body(response)
        assert '<td class="field-is_enabled">Invalid enabled value</td>' in page
        assert 'img/icon-yes.svg" alt="True">' in page, "the healthy row lost its icon"
        assert "icon-no.svg" not in page, "the value was drawn as False"


@pytest.mark.django_db
class TestAReadableRowThatDoesNotValidate:
    """A row that reads and does not validate keeps the ordinary form."""

    def test_keeps_its_change_form_and_its_place_on_the_page(self, client, admin_user):
        row = a_schedule("broken")
        quoted = '"trigger"' if connection.vendor != "mysql" else "`trigger`"
        by_sql(
            f"UPDATE {SCHEDULES} SET {quoted} = 'cron', cron = 'banana', "  # noqa: S608
            f"every_seconds = NULL WHERE id = {row.pk}"
        )
        client.force_login(admin_user)
        listed = client.get(reverse(CHANGELIST))
        assert ">banana<" in body(listed)
        response = client.get(reverse(CHANGE, args=[row.pk]))
        assert response.status_code == 200
        assert "adminform" in response.context


def schedule_statements(queries):
    """
    What each statement a request sent about the schedules and their ticks
    was for, in the order sent: a count of the list, the newest ticks of a
    page, a page's rows, or the keys those rows are then read by.
    """
    kinds = []
    for query in queries.captured_queries:
        sql = query["sql"]
        if not sql.lstrip().upper().startswith("SELECT") or SCHEDULES not in sql:
            continue
        if "COUNT(" in sql.upper():
            kinds.append("count")
        elif TICKS in sql:
            kinds.append("newest ticks")
        elif "ox_stored_" in sql:
            kinds.append("rows")
        else:
            kinds.append("keys")
    return kinds


def savepoints(queries):
    return [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK TO"))
    ]


#: The changelist of schedules that all read: Django's two counts of the
#: list, the page's keys, chosen as Django chooses the page, its rows, and
#: every listed schedule's newest tick in one statement.
A_HEALTHY_PAGE = ["count", "count", "keys", "rows", "newest ticks"]

#: With the session and the person signed in, which Django reads first.
A_HEALTHY_PAGE_IN_ALL = 2 + len(A_HEALTHY_PAGE)


@pytest.mark.django_db
class TestTheHealthyPageCost:
    def _page(self, client):
        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        return response, queries

    def test_a_fixed_number_of_statements_whatever_the_rows(self, client, admin_user):
        client.force_login(admin_user)
        for i in range(3):
            a_tick(a_schedule(f"s{i:03d}"), timezone.now() - timedelta(hours=1))
        _, few_queries = self._page(client)
        for i in range(30, 90):
            a_tick(a_schedule(f"s{i:03d}"), timezone.now() - timedelta(hours=1))
        many, many_queries = self._page(client)
        assert len(many.context["cl"].result_list) == 63
        for queries in (few_queries, many_queries):
            assert schedule_statements(queries) == A_HEALTHY_PAGE
            assert len(queries.captured_queries) == A_HEALTHY_PAGE_IN_ALL
            # Inside this test's transaction, where a savepoint would show:
            # no read takes one.
            assert savepoints(queries) == []

    def test_the_change_form_reads_its_row_and_its_newest_tick(
        self, client, admin_user
    ):
        row = a_schedule("nightly")
        a_tick(row, timezone.now() - timedelta(hours=1))
        client.force_login(admin_user)
        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGE, args=[row.pk]))
        assert response.status_code == 200
        # Found through the admin's own queryset by its key, read, and then
        # its newest tick, for the warning the form carries when that does
        # not read.
        assert schedule_statements(queries) == ["keys", "rows", "newest ticks"]
        assert savepoints(queries) == []


@pytest.mark.django_db(databases=["default", "alt"])
class TestThePagesReadTheDatabaseTheSchedulesAreWrittenTo:
    """Under a router that sends reads to a replica, as get_queryset says."""

    class _ReplicaReads:
        def db_for_read(self, model, **hints):
            return "default" if model._meta.app_label == "django_ox" else None

        def db_for_write(self, model, **hints):
            return "alt" if model._meta.app_label == "django_ox" else None

        def allow_relation(self, obj1, obj2, **hints):
            return True

        def allow_migrate(self, db, app_label, **hints):
            return None

    def test_rows_and_ticks_come_from_the_written_database(
        self, client, admin_user, settings
    ):
        settings.DATABASE_ROUTERS = [self._ReplicaReads()]
        healthy = a_schedule("healthy")
        bad = a_schedule("bad")
        assert healthy._state.db == "alt"
        at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        OxScheduleTick.objects.using("alt").create(
            schedule_name=f"db:{healthy.pk}", scheduled_for=at, created_at=at
        )
        # alt is SQLite whatever the default database is.
        with connections["alt"].cursor() as cursor:
            cursor.execute(
                f"UPDATE {SCHEDULES} SET end_time = '2026-02-30 00:00:00' "  # noqa: S608
                f"WHERE id = {int(bad.pk)}"
            )
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        page = body(response)
        assert ">healthy</a>" in page
        assert f"bad (pk {bad.pk})" in page
        assert f"{at:%Y-%m-%d %H:%M}" in page
        recovery = client.get(reverse(CHANGE, args=[bad.pk]))
        assert recovery.status_code == 200, recovery.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in recovery.templates]


@pytest.mark.django_db(transaction=True)
class TestOutsideATransaction:
    """
    A request runs outside a transaction unless the project sets
    ATOMIC_REQUESTS, and that is how the changelist is read. Every other
    test here runs inside the transaction its test is wrapped in, and what
    may follow a read that failed is not decided the same way in the two.
    """

    def test_a_healthy_page_costs_what_it_does_inside_one(self, client, admin_user):
        for i in range(3):
            a_tick(a_schedule(f"s{i:03d}"), timezone.now() - timedelta(hours=1))
        client.force_login(admin_user)
        assert client.get(reverse(CHANGELIST)).status_code == 200
        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        assert schedule_statements(queries) == A_HEALTHY_PAGE
        assert len(queries.captured_queries) == A_HEALTHY_PAGE_IN_ALL

    def test_the_pages_answer_as_they_do_inside_one(self, client, admin_user):
        a_schedule("healthy")
        pk, column = an_unreadable_schedule()
        broken = a_schedule("broken")
        literal = {
            "postgresql": "'infinity'",
            "mysql": "'9999-00-00 00:00:00'",
        }.get(connection.vendor, "'9999-02-30 00:00:00'")
        insert_tick(f"db:{broken.pk}", literal)
        assert connection.get_autocommit()
        assert not connection.in_atomic_block
        client.force_login(admin_user)
        response = client.get(reverse(CHANGELIST))
        assert response.status_code == 200
        page = body(response)
        assert ">healthy</a>" in page
        assert f"Stored values django-ox cannot use: {column}" in page
        assert ">Unreadable tick history<" in page
        recovery = client.get(reverse(CHANGE, args=[pk]))
        assert recovery.status_code == 200, recovery.get("Location")
        assert UNREADABLE_PAGE in [t.name for t in recovery.templates]
        assert client.get(reverse(CHANGE, args=[broken.pk])).status_code == 200
        deleted = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})
        assert deleted.status_code == 302
        assert not OxSchedule.objects.filter(pk=pk).exists()
        # Nothing above left a transaction open behind it.
        assert connection.get_autocommit()
        assert not connection.in_atomic_block
        assert client.get(reverse(CHANGELIST)).status_code == 200


@pytest.mark.django_db
class TestWhatTheAdminLogs:
    def test_an_unreadable_row_is_logged_once_and_then_summarised(
        self, client, admin_user, caplog, clock
    ):
        pk, column = an_unreadable_schedule()
        client.force_login(admin_user)
        caplog.set_level("WARNING", logger="django_ox")

        def logged():
            return [
                r
                for r in caplog.records
                if getattr(r, "event", None) == "schedule_admin_row_unreadable"
            ]

        client.get(reverse(CHANGELIST))
        client.get(reverse(CHANGE, args=[pk]))
        client.get(reverse(CHANGELIST))
        (first,) = logged()
        assert first.schedule_pk == pk
        assert first.fields == [column]
        assert first.database == "default"
        assert column in first.reason
        clock[0] += 61
        client.get(reverse(CHANGELIST))
        assert len(logged()) == 2
        assert "2 more times since the last report" in logged()[1].getMessage()

    def test_an_unreadable_tick_is_logged_with_the_admin_as_its_surface(
        self, client, admin_user, caplog
    ):
        broken = a_schedule("broken")
        literal = {
            "postgresql": "'infinity'",
            "mysql": "'9999-00-00 00:00:00'",
        }.get(connection.vendor, "'banana'")
        insert_tick(f"db:{broken.pk}", literal)
        client.force_login(admin_user)
        caplog.set_level("WARNING", logger="django_ox")
        client.get(reverse(CHANGELIST))
        (record,) = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_tick_unreadable"
        ]
        assert record.surface == "admin"
        assert record.schedule_pk == broken.pk
        assert record.schedule_key == f"db:{broken.pk}"
        assert (
            record.tick_pk == OxScheduleTick.objects.values_list("pk", flat=True).get()
        )
        assert "scheduled_for" in record.reason


# -- times the requested zone cannot show ---------------------------------

YEAR_ONE = datetime(1, 1, 1, tzinfo=UTC)
LAST_HOUR = datetime(9999, 12, 31, 23, 0, tzinfo=UTC)


def inputs(response, name):
    """The value an input of the rendered form holds."""
    match = re.search(rf'name="{name}"[^>]*value="([^"]*)"', body(response))
    if match is None:
        match = re.search(rf'value="([^"]*)"[^>]*name="{name}"', body(response))
    return None if match is None else match.group(1)


def resubmission(response, **over):
    """The rendered change form submitted back as it was drawn."""
    form = response.context["adminform"].form
    data = {
        "name": form.instance.name,
        "task_key": form.instance.task_key,
        "trigger": form.instance.trigger,
        "cron": form.instance.cron,
        "every_seconds": form.instance.every_seconds or "",
        "phase_seconds": form.instance.phase_seconds,
        "arguments": json.dumps(form.instance.arguments),
        "starting_deadline_seconds": "",
        "end_time_0": inputs(response, "end_time_0") or "",
        "end_time_1": inputs(response, "end_time_1") or "",
    }
    if form.instance.enabled:
        data["enabled"] = "on"
    data.update(over)
    return data


@pytest.mark.django_db
class TestATimeTheRequestedZoneCannotShow:
    """
    Both rows are written through create_schedule, which checks that the
    database can hold the time. Neither check can know what zone the time
    will be shown in: a start at the first instant of year 1 is in year 0
    anywhere west of UTC, and an end in the last hour of 9999 is in year
    10000 anywhere east of it.
    """

    @pytest.fixture(autouse=True)
    def _with_time_zones(self, settings):
        if not settings.USE_TZ:
            pytest.skip(
                "with USE_TZ off a stored time has no zone and is shown as it "
                "is stored, so there is none that cannot show it"
            )

    @pytest.fixture
    def ancient(self, settings):
        settings.TIME_ZONE = "America/Chicago"
        return a_schedule("ancient", start_time=YEAR_ONE)

    @pytest.fixture
    def forever(self, settings):
        settings.TIME_ZONE = "Asia/Tokyo"
        return a_schedule("forever", end_time=LAST_HOUR)

    def test_the_changelist_shows_both_in_utc_and_says_so(
        self, client, admin_user, settings
    ):
        a_schedule("healthy")
        a_schedule("ancient", start_time=YEAR_ONE)
        a_schedule("forever", end_time=LAST_HOUR)
        client.force_login(admin_user)
        settings.TIME_ZONE = "America/Chicago"
        west = client.get(reverse(CHANGELIST))
        assert west.status_code == 200
        assert ">healthy</a>" in body(west)
        assert "UTC (outside the dates America/Chicago can show)" in body(west)
        settings.TIME_ZONE = "Asia/Tokyo"
        east = client.get(reverse(CHANGELIST))
        assert east.status_code == 200
        assert ">healthy</a>" in body(east)
        assert "UTC (outside the dates Asia/Tokyo can show)" in body(east)

    def test_a_start_in_year_one_west_of_utc(self, client, admin_user, ancient):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[ancient.pk]))
        assert response.status_code == 200
        assert "UTC (outside the dates America/Chicago can show)" in body(response)
        unchanged = client.post(
            reverse(CHANGE, args=[ancient.pk]), resubmission(response)
        )
        assert unchanged.status_code == 302
        ancient.refresh_from_db()
        assert ancient.start_time == YEAR_ONE

    def test_an_end_in_9999_east_of_utc_is_edited_in_utc(
        self, client, admin_user, forever
    ):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[forever.pk]))
        assert response.status_code == 200
        assert inputs(response, "end_time_0") == "9999-12-31"
        assert inputs(response, "end_time_1") == "23:00:00"
        field = response.context["adminform"].form.fields["end_time"]
        assert field.label == "End time (UTC)"
        assert "Asia/Tokyo" in str(field.help_text)

    def test_an_unchanged_submission_keeps_the_instant(
        self, client, admin_user, forever
    ):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[forever.pk]))
        boundary = forever.start_time
        saved = client.post(reverse(CHANGE, args=[forever.pk]), resubmission(response))
        assert saved.status_code == 302
        forever.refresh_from_db()
        assert forever.end_time == LAST_HOUR
        assert forever.start_time == boundary

    def test_a_typed_time_is_read_in_utc(self, client, admin_user, forever):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[forever.pk]))
        saved = client.post(
            reverse(CHANGE, args=[forever.pk]),
            resubmission(response, end_time_1="22:00:00"),
        )
        assert saved.status_code == 302
        forever.refresh_from_db()
        assert forever.end_time == datetime(9999, 12, 31, 22, 0, tzinfo=UTC)

    def test_clearing_the_end_time_works(self, client, admin_user, forever):
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[forever.pk]))
        saved = client.post(
            reverse(CHANGE, args=[forever.pk]),
            resubmission(response, end_time_0="", end_time_1=""),
        )
        assert saved.status_code == 302
        forever.refresh_from_db()
        assert forever.end_time is None

    def test_a_zone_changed_after_the_row_was_written(
        self, client, admin_user, settings
    ):
        settings.TIME_ZONE = "America/Chicago"
        row = a_schedule("forever", end_time=LAST_HOUR)
        client.force_login(admin_user)
        assert inputs(client.get(reverse(CHANGE, args=[row.pk])), "end_time_1") == (
            "17:00:00"
        ), "Chicago can show it, so it is shown in Chicago"
        settings.TIME_ZONE = "Asia/Tokyo"
        response = client.get(reverse(CHANGE, args=[row.pk]))
        assert response.status_code == 200
        assert inputs(response, "end_time_1") == "23:00:00"

    def test_a_zone_the_request_activates(self, client, admin_user, settings):
        settings.TIME_ZONE = "America/Chicago"
        row = a_schedule("forever", end_time=LAST_HOUR)
        client.force_login(admin_user)
        with timezone.override("Asia/Tokyo"):
            response = client.get(reverse(CHANGE, args=[row.pk]))
            assert response.status_code == 200
            assert inputs(response, "end_time_1") == "23:00:00"
            saved = client.post(reverse(CHANGE, args=[row.pk]), resubmission(response))
            assert saved.status_code == 302
            assert client.get(reverse(CHANGELIST)).status_code == 200
        row.refresh_from_db()
        assert row.end_time == LAST_HOUR

    def test_a_time_the_zone_can_show_is_shown_and_read_in_it(
        self, client, admin_user, settings
    ):
        settings.TIME_ZONE = "Asia/Tokyo"
        ends = datetime(2099, 1, 1, 3, 0, tzinfo=UTC)
        row = a_schedule("ordinary", end_time=ends)
        client.force_login(admin_user)
        response = client.get(reverse(CHANGE, args=[row.pk]))
        assert inputs(response, "end_time_1") == "12:00:00"
        assert response.context["adminform"].form.fields["end_time"].label == (
            "End time"
        )
        saved = client.post(reverse(CHANGE, args=[row.pk]), resubmission(response))
        assert saved.status_code == 302
        row.refresh_from_db()
        assert row.end_time == ends

    def test_the_fallback_is_logged_with_the_field_and_the_zone(
        self, client, admin_user, forever, caplog
    ):
        client.force_login(admin_user)
        caplog.set_level("WARNING", logger="django_ox")
        client.get(reverse(CHANGELIST))
        records = [
            r
            for r in caplog.records
            if getattr(r, "event", None) == "schedule_admin_datetime_fallback"
        ]
        assert [(r.field, r.zone) for r in records] == [("end_time", "Asia/Tokyo")]
