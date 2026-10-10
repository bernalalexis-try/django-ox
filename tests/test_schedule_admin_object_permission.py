"""
The schedule admin and a permission backend that decides by the row.

A person who holds every permission on the schedule table but not the one a
registry entry names is asked of the row itself, by a backend that grants it
on some schedules and not others. The admin hands that backend the row in
full, from the write alias, and so does the write behind it: a pause, a
delete and "Delete selected" go by the name the database holds, however the
reads of the table are routed and whatever the name is made of.
"""

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.urls import reverse

from django_ox.models import OxSchedule
from django_ox.registry import ScheduleKind, register

from . import tasks
from .test_replica_reads import PRIMARY, REPLICA, _ReplicaReadRouter
from .test_schedule_admin_unreadable import (
    CHANGE,
    CHANGELIST,
    EVERY_PERMISSION,
    _registry,  # noqa: F401 (a fixture)
    a_schedule,
    clock,  # noqa: F401 (a fixture)
    only_on,
    set_column,
    staff,
)
from .test_schedule_admin_unreadable_actions import _dispatched, act  # noqa: F401

PERMISSION = "auth.view_user"

#: Text that is not UTF-8, as SQLite keeps it.
NOT_UTF_8 = "CAST(X'62616480FF' AS TEXT)"


class NameBackend:
    """Grants the entry's permission on a schedule named after the person."""

    def authenticate(self, request, **credentials):
        return None

    def has_perm(self, user_obj, perm, obj=None):
        if obj is None or perm != PERMISSION:
            return False
        name = obj.name
        return isinstance(name, str) and name.startswith(f"{user_obj.username}-")

    def get_user(self, user_id):
        return User.objects.filter(pk=user_id).first()


@pytest.fixture(autouse=True)
def _guarded(_registry, settings):  # noqa: F811 (the fixture, by its name)
    register(ScheduleKind(key="guarded", task=tasks.add, permission=PERMISSION))
    settings.AUTHENTICATION_BACKENDS = [
        "django.contrib.auth.backends.ModelBackend",
        f"{__name__}.NameBackend",
    ]


@pytest.fixture
def person(client):
    user = staff(*EVERY_PERMISSION, username="objuser")
    client.force_login(user)
    return user


def guarded(name):
    return a_schedule(name, task_key="guarded").pk


def enabled(pk, alias="default"):
    return OxSchedule.objects.using(alias).get(pk=pk).enabled


@pytest.mark.django_db(databases=[REPLICA, PRIMARY])
class TestTheNameTheDatabaseHolds:
    @pytest.fixture(autouse=True)
    def _router(self, settings):
        settings.DATABASE_ROUTERS = [_ReplicaReadRouter()]

    def lagging(self, on_primary, on_replica):
        pk = guarded(on_primary)
        copy = OxSchedule.objects.using(PRIMARY).get(pk=pk)
        copy.name = on_replica
        copy._state.adding = True
        copy.save(using=REPLICA, force_insert=True)
        return pk

    def test_disable_selected_follows_the_primarys_name(self, client, person):
        refused = self.lagging("other-team", "objuser-mine")
        granted = self.lagging("objuser-mine", "other-team")

        act(client, "disable_selected", [refused, granted])

        assert enabled(refused, PRIMARY) is True
        assert enabled(granted, PRIMARY) is False

    def test_delete_selected_follows_the_primarys_name(self, client, person):
        refused = self.lagging("other-team", "objuser-mine")
        granted = self.lagging("objuser-mine", "other-team")

        act(client, "delete_selected", [granted], post="yes")
        act(client, "delete_selected", [refused], post="yes")

        remaining = set(OxSchedule.objects.using(PRIMARY).values_list("pk", flat=True))
        assert remaining == {refused}

    def test_the_change_page_follows_the_primarys_name(self, client, person):
        refused = self.lagging("other-team", "objuser-mine")
        granted = self.lagging("objuser-mine", "other-team")

        # The person may see both and save one: the other is a page to read.
        page = client.get(reverse(CHANGE, args=[granted]))
        assert page.status_code == 200
        assert 'name="_save"' in page.content.decode()
        page = client.get(reverse(CHANGE, args=[refused]))
        assert page.status_code == 200
        assert 'name="_save"' not in page.content.decode()


@pytest.mark.django_db
class TestAnAdminListThatHoldsANameThatDoesNotRead:
    def test_the_changelist_and_the_actions_answer(self, client, person):
        only_on("sqlite", ": SQLite keeps text that is not UTF-8")
        mine = guarded("objuser-mine")
        odd = guarded("objuser-odd")
        set_column(odd, "name", NOT_UTF_8)

        assert client.get(reverse(CHANGELIST)).status_code == 200
        act(client, "disable_selected", [mine, odd])

        # The row whose name does not read is not granted, and is not made up.
        assert enabled(mine) is False
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT enabled FROM django_ox_oxschedule WHERE id = %s", [odd]
            )
            assert cursor.fetchone() == (1,)
