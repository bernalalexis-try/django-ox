"""
The schedule admin and a stored schedule whose task key is not registered
in this process.

A stored schedule names its registry entry by task key, and the entry is
where a permission of its own is declared. A row whose key this process
does not register names no entry here: the entry was taken out of the code,
or it is in newer code than this process runs, as it is for a time during a
rolling deploy. The permission the row requires is then not known, which is
not the same as there being none, so only an active superuser may change or
delete it. Anyone else who may see the schedule table still sees the row.

The admin asks django_ox.stored that question for every page and every
action, and django_ox.stored asks it again at the write. Each way the admin
changes or deletes a row is driven here as a person drives it: by someone
who holds every permission on the schedule table, by the same person
holding the permission the entry asked for while it was registered as well,
and by a superuser.
"""

import re

import pytest
from django.contrib.admin.models import CHANGE as CHANGED
from django.contrib.admin.models import DELETION, LogEntry
from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse

from django_ox import registry
from django_ox.models import OxTask
from django_ox.registry import ScheduleKind, register

from . import tasks
from .test_schedule_admin_unreadable import (
    CHANGE,
    CHANGELIST,
    DELETE,
    EVERY_PERMISSION,
    ONE_PER_ENGINE,
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
    NO_PERMISSION,
    _dispatched,  # noqa: F401 (a fixture)
    act,
    cannot_run,
    everything_held,
    held,
    marker,
    not_valid,
    paused,
    went_back_to_the_list,
)

pytestmark = pytest.mark.django_db

#: The row's key when it was written: one whose entry asked for a permission
#: of its own, and one anyone could schedule.
ONCE_REGISTERED = ["restricted", "nightly"]

#: What the person holds: every permission on the schedule table, and, in
#: the second case, the permission the guarded entry asked for as well.
HOLDING = [
    pytest.param(EVERY_PERMISSION, id="every-model-permission"),
    pytest.param(
        (*EVERY_PERMISSION, "view_user"), id="and-the-entrys-former-permission"
    ),
]


@pytest.fixture(autouse=True)
def _another_entry(_registry):  # noqa: F811 (the fixture, by its name)
    register(ScheduleKind(key="nightly", task=tasks.add))


def an_orphan(key, name="orphan", *, reads=True):
    """
    A schedule written while `key` was registered, whose entry is then gone
    from this process: its primary key. With `reads` false it also holds a
    value that does not read, so that its pages are the recovery ones.
    """
    row = a_schedule(name, task_key=key)
    registry._registry.pop(key)
    if not reads:
        column, literal = ONE_PER_ENGINE[connection.vendor]
        set_column(row.pk, column, literal)
    return row.pk


def selecting(action, pk, **more):
    return {"action": action, "_selected_action": [str(pk)], **more}


@pytest.mark.parametrize("key", ONCE_REGISTERED)
@pytest.mark.parametrize("permissions", HOLDING)
class TestSomeoneWhoIsNotASuperuser:
    @pytest.fixture
    def orphan(self, key):
        return an_orphan(key)

    @pytest.fixture
    def person(self, client, permissions):
        user = staff(*permissions)
        client.force_login(user)
        return user

    @pytest.fixture
    def untouched(self, orphan):
        """Checks, once the test has acted, that nothing was done to the row."""
        before, told = everything_held(orphan), marker()

        def check():
            assert held(orphan, "id") is not None, "the row was deleted"
            assert everything_held(orphan) == before
            assert marker() == told, "the workers were told of a change not made"
            assert not LogEntry.objects.exists()
            assert not OxTask.objects.exists()

        return check

    def test_sees_the_row_in_the_changelist(self, client, person, orphan, untouched):
        response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        assert ">orphan</a>" in body(response)
        assert f'name="_selected_action" value="{orphan}"' in body(response)
        untouched()

    def test_is_shown_its_change_form_with_nothing_to_save_it_by(
        self, client, person, orphan, untouched
    ):
        response = client.get(reverse(CHANGE, args=[orphan]))

        assert response.status_code == 200
        assert response.context["has_change_permission"] is False
        assert response.context["has_delete_permission"] is False
        page = body(response)
        assert 'name="_save"' not in page
        assert 'name="_continue"' not in page
        assert "deletelink" not in page
        untouched()

    def test_cannot_save_it_by_submitting_the_form_all_the_same(
        self, client, person, orphan, untouched
    ):
        response = client.post(
            reverse(CHANGE, args=[orphan]),
            {
                "name": "renamed",
                "task_key": "report",
                "trigger": "interval",
                "every_seconds": 30,
                "phase_seconds": 0,
                "arguments": "{}",
            },
        )

        assert response.status_code == 403
        untouched()

    @pytest.mark.parametrize(
        ("action", "done"),
        [
            ("disable_selected", "Disabled 0 schedule(s)."),
            ("enable_selected", "Enabled 0 schedule(s)."),
            ("run_once_now", "Enqueued 0 task(s)."),
        ],
    )
    def test_is_refused_by_each_action(
        self, client, person, orphan, untouched, action, done
    ):
        response = act(client, action, [orphan])

        assert went_back_to_the_list(response)
        assert messages_of(response) == [done, NO_PERMISSION]
        untouched()

    def test_an_action_still_acts_on_the_rows_beside_it(self, client, person, orphan):
        beside = a_schedule("beside-it")
        before = everything_held(orphan)

        response = act(client, "disable_selected", [beside.pk, orphan])

        assert messages_of(response) == ["Disabled 1 schedule(s).", NO_PERMISSION]
        assert paused(beside.pk)
        assert everything_held(orphan) == before

    def test_is_refused_by_its_delete_page(self, client, person, orphan, untouched):
        asked = client.get(reverse(DELETE, args=[orphan]))
        refused = client.post(reverse(DELETE, args=[orphan]), {"post": "yes"})

        assert asked.status_code == 403
        assert refused.status_code == 403
        untouched()

    def test_is_refused_by_delete_selected(self, client, person, orphan, untouched):
        asked = client.post(reverse(CHANGELIST), selecting("delete_selected", orphan))
        refused = client.post(
            reverse(CHANGELIST), selecting("delete_selected", orphan, post="yes")
        )

        assert asked.status_code == 200
        assert asked.context["perms_lacking"] == {"ox schedule"}
        assert 'value="yes"' not in body(asked), "the deletion can be confirmed"
        assert refused.status_code == 403
        untouched()

    def test_delete_selected_keeps_the_rows_selected_with_it_too(
        self, client, person, orphan
    ):
        beside = a_schedule("beside-it")
        before, told = everything_held(orphan), marker()

        refused = client.post(
            reverse(CHANGELIST),
            {
                "action": "delete_selected",
                "_selected_action": [str(beside.pk), str(orphan)],
                "post": "yes",
            },
        )

        assert refused.status_code == 403
        assert held(beside.pk, "id") is not None
        assert everything_held(orphan) == before
        assert marker() == told
        assert not LogEntry.objects.exists()


@pytest.mark.parametrize("key", ONCE_REGISTERED)
@pytest.mark.parametrize("permissions", HOLDING)
class TestTheRecoveryPageOfOneThatAlsoDoesNotRead:
    """
    A row whose task key is not registered and that holds a value that does
    not read has the recovery page for its change page, whose two buttons
    are Disable and Delete.
    """

    def test_someone_who_is_not_a_superuser_is_offered_neither_and_refused_both(
        self, client, key, permissions
    ):
        pk = an_orphan(key, reads=False)
        before, told = everything_held(pk), marker()
        client.force_login(staff(*permissions))

        page = client.get(reverse(CHANGE, args=[pk]))
        # What each button would send, sent without the button.
        disabled = act(client, "disable_selected", [pk], index="0")
        deleted = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})

        assert page.status_code == 200
        assert UNREADABLE_PAGE in [t.name for t in page.templates]
        assert not page.context["can_disable"]
        assert not page.context["can_delete"]
        assert 'value="disable_selected"' not in body(page)
        assert "deletelink" not in body(page)
        assert messages_of(disabled) == ["Disabled 0 schedule(s).", NO_PERMISSION]
        assert deleted.status_code == 403
        assert everything_held(pk) == before
        assert marker() == told
        assert not LogEntry.objects.exists()


@pytest.mark.parametrize("key", ONCE_REGISTERED)
class TestAnActiveSuperuser:
    @pytest.fixture(autouse=True)
    def root(self, client):
        user = User.objects.create_superuser("root", "root@example.com", "pw")
        client.force_login(user)
        return user

    def test_disables_it(self, client, key):
        pk = an_orphan(key)
        told = marker()

        response = act(client, "disable_selected", [pk])

        assert messages_of(response) == ["Disabled 1 schedule(s)."]
        assert paused(pk)
        assert marker() > told

    def test_is_let_through_to_enable_and_run_it_which_its_key_then_refuses(
        self, client, key
    ):
        """
        Neither action is refused a superuser for want of a permission.
        What stops both is the row itself: a schedule whose task key is not
        registered does not validate and cannot be built, whoever asks.
        """
        pk = an_orphan(key)
        act(client, "disable_selected", [pk])

        enabled = act(client, "enable_selected", [pk])
        ran = act(client, "run_once_now", [pk])

        named = f"orphan (pk {pk})"
        assert messages_of(enabled) == ["Enabled 0 schedule(s).", not_valid(named)]
        assert messages_of(ran) == ["Enqueued 0 task(s).", cannot_run(named)]
        assert NO_PERMISSION not in messages_of(enabled) + messages_of(ran)
        assert paused(pk)
        assert not OxTask.objects.exists()

    def test_has_a_form_that_saves_it_onto_a_key_that_is_registered(self, client, key):
        pk = an_orphan(key)

        page = client.get(reverse(CHANGE, args=[pk]))
        saved = client.post(
            reverse(CHANGE, args=[pk]),
            {
                "name": "orphan",
                "task_key": "report",
                "trigger": "interval",
                "every_seconds": 60,
                "phase_seconds": 0,
                "arguments": "{}",
                "enabled": "on",
            },
        )

        assert page.status_code == 200
        assert page.context["has_change_permission"] is True
        assert 'name="_save"' in body(page)
        assert saved.status_code == 302, saved.context and saved.context["errors"]
        assert held(pk, "task_key") is not None
        entry = LogEntry.objects.get()
        assert (entry.action_flag, entry.object_id) == (CHANGED, str(pk))

    def test_deletes_it_from_its_delete_page(self, client, key):
        pk = an_orphan(key)
        told = marker()

        asked = client.get(reverse(DELETE, args=[pk]))
        response = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})

        assert asked.status_code == 200
        assert response.status_code == 302
        assert held(pk, "id") is None
        assert marker() > told
        assert LogEntry.objects.get().action_flag == DELETION

    def test_deletes_it_by_delete_selected(self, client, key):
        pk = an_orphan(key)
        beside = a_schedule("beside-it")

        response = act(client, "delete_selected", [beside.pk, pk], post="yes")

        assert messages_of(response) == ["Successfully deleted 2 ox schedules."]
        assert held(pk, "id") is None
        assert held(beside.pk, "id") is None

    def test_uses_both_buttons_of_its_recovery_page(self, client, key):
        pk = an_orphan(key, reads=False)

        page = client.get(reverse(CHANGE, args=[pk]))

        assert UNREADABLE_PAGE in [t.name for t in page.templates]
        assert page.context["can_disable"]
        assert page.context["can_delete"]
        # What the page's own Disable form submits, to where it submits it.
        form = re.search(
            r'<form method="post" action="([^"]*)">(.*?)</form>', body(page), re.S
        )
        sent = dict(
            re.findall(r'type="hidden" name="([^"]+)" value="([^"]*)"', form[2])
        )
        sent.pop("csrfmiddlewaretoken", None)
        disabled = client.post(form[1], sent, follow=True)
        assert messages_of(disabled) == ["Disabled 1 schedule(s)."]
        assert paused(pk)
        assert reverse(DELETE, args=[pk]) in body(page)
        deleted = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})
        assert deleted.status_code == 302
        assert held(pk, "id") is None


class TestASuperuserWhoIsNotActive:
    """
    Django does not let such a user in at all. The password is refused at
    the login form, and a session the user already had stops being theirs:
    the model backend hands back no user for it, so every admin page
    answers as it answers someone who is not logged in.
    """

    def test_cannot_log_in_and_no_page_or_action_is_theirs(self, client):
        retired = User.objects.create_superuser("retired", "r@example.com", "pw")
        retired.is_active = False
        retired.save()
        pk = an_orphan("restricted")
        before, told = everything_held(pk), marker()

        assert client.login(username="retired", password="pw") is False
        # A session from before the account was switched off.
        client.force_login(retired)
        answers = [
            client.get(reverse(CHANGELIST)),
            client.get(reverse(CHANGE, args=[pk])),
            client.post(reverse(CHANGELIST), selecting("disable_selected", pk)),
            client.post(reverse(DELETE, args=[pk]), {"post": "yes"}),
            client.post(
                reverse(CHANGELIST), selecting("delete_selected", pk, post="yes")
            ),
        ]

        for response in answers:
            assert response.status_code == 302
            assert response["Location"].startswith(reverse("admin:login"))
        assert everything_held(pk) == before
        assert marker() == told
        assert not LogEntry.objects.exists()
