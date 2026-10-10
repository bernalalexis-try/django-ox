"""
The schedule admin and a stored schedule whose count of boundary writes is
the most its column can hold.

Such a row reads, as far as Django's own read goes. What it cannot take is
a write that moves its boundary, since that adds one to the count:
PostgreSQL and MySQL refuse the write, and SQLite keeps the sum as a float.
Disabling a row whose boundary is stale is such a write. So "Disable
selected schedules" answered a server error for a selection that held one,
on PostgreSQL and MySQL, and on SQLite left the row a count that no longer
read. Only SQL writes such a count, and no page here can lower it.

django_ox meets the row as one that does not read, by its count. The
changelist lists it in its place, its change page is the recovery page,
Disable pauses it and leaves the count alone, Delete removes it, and Enable
and a manual run leave it as it is and say which row it was. The rows
selected beside it are acted on as ever.
"""

import re

import pytest
from django.contrib.admin.models import CHANGE as CHANGED
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import User
from django.db import connection, connections
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from django_ox.models import OxTask

from .test_schedule_admin_unreadable import (
    CHANGE,
    CHANGELIST,
    DELETE,
    EVERY_PERMISSION,
    HISTORY,
    UNREADABLE_PAGE,
    _registry,  # noqa: F401 (a fixture)
    a_schedule,
    body,
    clock,  # noqa: F401 (a fixture)
    messages_of,
    set_column,
    staff,
)
from .test_schedule_admin_unreadable_actions import (
    CONFIRMATION,
    _dispatched,  # noqa: F401 (a fixture)
    act,
    deleting_unread,
    everything_held,
    held,
    marker,
    not_enabled,
    not_run,
    paused,
    went_back_to_the_list,
)
from .test_stored_read import largest_count

COUNT = "boundary_generation"

#: The row with its boundary stale, which a pause would move if it could,
#: and with it current.
BOUNDARY = pytest.mark.parametrize("stale", [True, False], ids=["stale", "current"])


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


def a_full_schedule(name="full", *, stale=True, **over):
    """
    A schedule written by the API whose count SQL then set to the most its
    column holds: its primary key. Stale, it was retimed by SQL first, a
    route that moves no boundary.
    """
    row = a_schedule(name, **over)
    if stale:
        set_column(row.pk, "every_seconds", "120")
    set_column(row.pk, COUNT, str(largest_count()))
    return row.pk


def full_still(pk):
    """The count is as SQL left it: the same number, and a whole number still."""
    kind = f"typeof({COUNT})" if connection.vendor == "sqlite" else "NULL"
    statement = f"SELECT {COUNT}, {kind} FROM django_ox_oxschedule WHERE id = %s"  # noqa: S608
    with connection.cursor() as cursor:
        cursor.execute(statement, [pk])
        found = cursor.fetchone()
    whole = "integer" if connection.vendor == "sqlite" else None
    return found == (largest_count(), whole)


def why():
    return (
        f"holds {largest_count()}, the most its column can hold, so the count "
        "cannot be raised"
    )


@pytest.mark.django_db
class TestItsPages:
    def test_the_changelist_lists_it_beside_the_rows_that_read(
        self, client, admin_user
    ):
        a_schedule("healthy")
        pk = a_full_schedule()
        client.force_login(admin_user)

        response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        page = body(response)
        assert ">healthy</a>" in page
        assert f"full (pk {pk})" in page
        assert f"Stored values django-ox cannot use: {COUNT}" in page
        assert f'name="_selected_action" value="{pk}"' in page

    @BOUNDARY
    def test_its_change_page_says_why_and_offers_disable_and_delete(
        self, client, admin_user, stale
    ):
        pk = a_full_schedule(stale=stale)
        client.force_login(admin_user)

        response = client.get(reverse(CHANGE, args=[pk]))

        assert response.status_code == 200
        assert UNREADABLE_PAGE in [t.name for t in response.templates]
        assert "adminform" not in response.context, "the edit form was offered"
        assert response.context["problems"] == [(COUNT, why())]
        # The count is django_ox.stored's own to write, and it has no room
        # to be written again: nothing to pass update_schedule repairs it.
        assert response.context["stuck"] == [COUNT]
        assert response.context["example"] is None
        page = body(response)
        assert f"<strong>{COUNT}</strong>: {why()}" in page
        assert "To repair it, call" not in page
        assert (
            "You can pause or delete the schedule, subject to permissions."
            in " ".join(page.split())
        )
        assert response.context["can_disable"]
        assert response.context["can_delete"]
        assert 'name="action" value="disable_selected"' in page
        assert reverse(DELETE, args=[pk]) in page

    def test_its_history_page_is_the_same_page(self, client, admin_user):
        pk = a_full_schedule()
        client.force_login(admin_user)

        response = client.get(reverse(HISTORY, args=[pk]))

        assert response.status_code == 200
        assert UNREADABLE_PAGE in [t.name for t in response.templates]

    def test_a_post_to_its_change_page_writes_nothing(self, client, admin_user):
        pk = a_full_schedule()
        before, told = everything_held(pk), marker()
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
        assert everything_held(pk) == before
        assert marker() == told
        assert not LogEntry.objects.exists()


@pytest.mark.django_db
class TestDisablingIt:
    @BOUNDARY
    def test_the_recovery_pages_button_pauses_it_and_leaves_the_count(
        self, client, admin_user, stale
    ):
        pk = a_full_schedule(stale=stale)
        before, told = everything_held(pk), marker()
        client.force_login(admin_user)
        page = body(client.get(reverse(CHANGE, args=[pk])))
        # What the page's own form submits, to where it submits it.
        form = re.search(
            r'<form method="post" action="([^"]*)">(.*?)</form>', page, re.S
        )
        assert form is not None, "the page offers no Disable"
        sent = dict(
            re.findall(r'type="hidden" name="([^"]+)" value="([^"]*)"', form[2])
        )
        sent.pop("csrfmiddlewaretoken", None)

        response = client.post(form[1], sent, follow=True)

        assert went_back_to_the_list(response)
        assert messages_of(response) == ["Disabled 1 schedule(s)."]
        assert paused(pk)
        assert full_still(pk)
        assert marker() > told, "the workers were not told"
        after = everything_held(pk)
        written = {"enabled", "updated_at"} | (set() if stale else {"boundary_for"})
        assert {c for c in before if before[c] != after[c]} == written
        entry = LogEntry.objects.get()
        assert (entry.action_flag, entry.object_id) == (CHANGED, str(pk))

    @BOUNDARY
    def test_the_action_pauses_it_and_every_row_selected_with_it(
        self, client, admin_user, stale
    ):
        first = a_schedule("first")
        pk = a_full_schedule(stale=stale)
        last = a_schedule("last")
        client.force_login(admin_user)

        response = act(client, "disable_selected", [first.pk, pk, last.pk])

        assert response.status_code == 200
        assert went_back_to_the_list(response)
        assert messages_of(response) == ["Disabled 3 schedule(s)."]
        assert paused(first.pk)
        assert paused(pk)
        assert paused(last.pk)
        assert full_still(pk)

    def test_no_write_of_the_pause_names_the_count(self, client, admin_user):
        pk = a_full_schedule()
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            act(client, "disable_selected", [pk])

        writes = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"].lstrip().upper().startswith("UPDATE")
            and "django_ox_oxschedule" in q["sql"]
            and "django_ox_oxschedulechange" not in q["sql"]
        ]
        assert writes, "nothing was written"
        assert not [sql for sql in writes if COUNT in sql]
        assert paused(pk)

    def test_someone_who_may_change_the_table_pauses_it_too(self, client):
        # Its task key reads, and names an entry anyone may schedule.
        pk = a_full_schedule()
        client.force_login(staff(*EVERY_PERMISSION))

        response = act(client, "disable_selected", [pk])

        assert messages_of(response) == ["Disabled 1 schedule(s)."]
        assert paused(pk)
        assert full_still(pk)


@pytest.mark.django_db
class TestDeletingIt:
    @BOUNDARY
    def test_from_its_delete_page(self, client, admin_user, stale):
        healthy = a_schedule("healthy")
        pk = a_full_schedule(stale=stale)
        told = marker()
        client.force_login(admin_user)

        confirm = client.get(reverse(DELETE, args=[pk]))
        response = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})

        assert confirm.status_code == 200
        assert f"full (pk {pk})" in body(confirm)
        assert response.status_code == 302
        assert held(pk, "id") is None
        assert held(healthy.pk, "id") is not None
        assert marker() > told
        entry = LogEntry.objects.get()
        assert (entry.action_flag, entry.object_id) == (DELETION, str(pk))

    def test_by_delete_selected_beside_a_row_that_reads(self, client, admin_user):
        healthy = a_schedule("healthy")
        pk = a_full_schedule()
        client.force_login(admin_user)

        asked = act(client, "delete_selected", [healthy.pk, pk])
        assert CONFIRMATION in [t.name for t in asked.templates]
        assert deleting_unread(f"full (pk {pk})") in messages_of(asked)
        response = act(client, "delete_selected", [healthy.pk, pk], post="yes")

        assert messages_of(response) == ["Successfully deleted 2 ox schedules."]
        assert held(pk, "id") is None
        assert held(healthy.pk, "id") is None


@pytest.mark.django_db
class TestEnableAndAManualRunLeaveItAndNameIt:
    @BOUNDARY
    def test_enable(self, client, admin_user, stale):
        healthy = a_schedule("healthy", enabled=False)
        pk = a_full_schedule(stale=stale, enabled=False)
        before = everything_held(pk)
        client.force_login(admin_user)

        response = act(client, "enable_selected", [healthy.pk, pk])

        assert went_back_to_the_list(response)
        assert messages_of(response) == [
            "Enabled 1 schedule(s).",
            not_enabled(f"full (pk {pk})"),
        ]
        assert not paused(healthy.pk)
        assert everything_held(pk) == before

    @BOUNDARY
    def test_a_manual_run(self, client, admin_user, stale):
        healthy = a_schedule("healthy")
        pk = a_full_schedule(stale=stale)
        before = everything_held(pk)
        client.force_login(admin_user)

        response = act(client, "run_once_now", [healthy.pk, pk])

        assert went_back_to_the_list(response)
        assert messages_of(response) == [
            "Enqueued 1 task(s).",
            not_run(f"full (pk {pk})"),
        ]
        assert OxTask.objects.count() == 1
        assert everything_held(pk) == before


@pytest.mark.django_db(transaction=True)
class TestInsideARequestThatIsOneTransaction:
    """
    With ATOMIC_REQUESTS on, the database's refusal of the one row's write
    took the whole request back with it: no row of the selection was left
    paused.
    """

    @pytest.fixture(autouse=True)
    def atomic_requests(self, monkeypatch):
        monkeypatch.setitem(
            connections.settings["default"], "ATOMIC_REQUESTS", value=True
        )

    @BOUNDARY
    def test_disable_pauses_the_selection_and_the_request_commits(
        self, client, admin_user, stale
    ):
        first = a_schedule("first")
        pk = a_full_schedule(stale=stale)
        last = a_schedule("last")
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.post(
                reverse(CHANGELIST),
                {
                    "action": "disable_selected",
                    "_selected_action": [str(first.pk), str(pk), str(last.pk)],
                },
            )

        assert response.status_code == 302
        ends = [
            q["sql"]
            for q in queries.captured_queries
            if q["sql"] in ("COMMIT", "ROLLBACK")
        ]
        assert ends == ["COMMIT"]
        assert connection.get_autocommit()
        assert paused(first.pk)
        assert paused(pk)
        assert paused(last.pk)
        assert full_still(pk)
