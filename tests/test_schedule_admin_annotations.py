"""
A project's own subclass of the schedule admin whose get_queryset annotates.

The schedule pages are drawn from instances the package reconstructs, row by
row, so that a stored value that does not read cannot take a page with it.
An annotation put on the queryset therefore does not reach those instances,
on the changelist or the change form. It still does what a queryset
annotation does before the rows are read: it can order the rows and so
choose which of them are on a page. And the scope the subclass gives the
queryset holds for viewing and for every change made from the admin.
"""

import json

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from django_ox.models import OxSchedule
from django_ox.registry import ScheduleKind, register

from . import tasks
from .test_schedule_admin_unreadable import a_schedule

pytestmark = pytest.mark.django_db

CHANGELIST = "annotated:django_ox_oxschedule_changelist"
CHANGE = "annotated:django_ox_oxschedule_change"
DELETE = "annotated:django_ox_oxschedule_delete"
HISTORY = "annotated:django_ox_oxschedule_history"
ANNOTATIONS = ("shout", "rank")


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.add))


@pytest.fixture(autouse=True)
def _site(settings):
    settings.ROOT_URLCONF = "tests.urls_schedule_annotated"


@pytest.fixture
def client(client, db):
    client.force_login(User.objects.create_superuser("root", "r@example.com", "pw"))
    return client


def enabled(pk):
    return OxSchedule.objects.get(pk=pk).enabled


def act(client, action, pks):
    return client.post(
        reverse(CHANGELIST),
        {"action": action, "_selected_action": [str(pk) for pk in pks]},
        follow=True,
    )


class TestTheAnnotationsAreNotOnTheInstances:
    def test_not_on_the_rows_of_the_changelist(self, client):
        a_schedule("alpha")
        a_schedule("beta")

        response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        rows = list(response.context["cl"].result_list)
        assert len(rows) == 2
        for row in rows:
            for name in ANNOTATIONS:
                assert not hasattr(row, name)

    def test_not_on_the_instance_of_the_change_form(self, client):
        pk = a_schedule("alpha").pk

        response = client.get(reverse(CHANGE, args=[pk]))

        assert response.status_code == 200
        instance = response.context["adminform"].form.instance
        assert instance.name == "alpha"
        for name in ANNOTATIONS:
            assert not hasattr(instance, name)
            assert not hasattr(response.context["original"], name)

    def test_the_change_form_still_saves(self, client):
        pk = a_schedule("alpha").pk
        url = reverse(CHANGE, args=[pk])
        form = client.get(url).context["adminform"].form

        response = client.post(
            url,
            {
                "name": "alpha-renamed",
                "task_key": form.instance.task_key,
                "trigger": form.instance.trigger,
                "every_seconds": form.instance.every_seconds,
                "phase_seconds": form.instance.phase_seconds,
                "arguments": json.dumps(form.instance.arguments),
                "enabled": "on",
            },
        )

        assert response.status_code == 302
        assert OxSchedule.objects.get(pk=pk).name == "alpha-renamed"


class TestAnAnnotationThatOrdersTheRows:
    def test_still_chooses_the_page(self, client):
        # Longest name first, two to a page. By name alone the order would
        # be a, bb, ccc, dddd, and the first page would be a and bb.
        for name in ("a", "bb", "ccc", "dddd"):
            a_schedule(name)

        first = client.get(reverse(CHANGELIST))
        second = client.get(reverse(CHANGELIST) + "?p=2")

        assert [r.name for r in first.context["cl"].result_list] == ["dddd", "ccc"]
        assert [r.name for r in second.context["cl"].result_list] == ["bb", "a"]
        for row in first.context["cl"].result_list:
            for name in ANNOTATIONS:
                assert not hasattr(row, name)


class TestTheScopeOfTheQuerysetStillHolds:
    def test_a_row_outside_it_is_not_listed_or_counted(self, client):
        a_schedule("visible")
        a_schedule("hidden-one")

        response = client.get(reverse(CHANGELIST))

        names = [r.name for r in response.context["cl"].result_list]
        assert names == ["visible"]
        assert response.context["cl"].result_count == 1

    @pytest.mark.parametrize("page", [CHANGE, DELETE, HISTORY])
    def test_a_row_outside_it_has_no_page(self, client, page):
        pk = a_schedule("hidden-one").pk

        response = client.get(reverse(page, args=[pk]))

        assert response.status_code == 302
        assert response.url == reverse("annotated:index")

    def test_a_row_outside_it_cannot_be_saved(self, client):
        row = a_schedule("hidden-one")

        response = client.post(
            reverse(CHANGE, args=[row.pk]),
            {
                "name": "taken",
                "task_key": "report",
                "trigger": "interval",
                "every_seconds": 5,
                "phase_seconds": 0,
                "arguments": "{}",
                "enabled": "on",
            },
        )

        assert response.status_code == 302
        saved = OxSchedule.objects.get(pk=row.pk)
        assert (saved.name, saved.every_seconds) == ("hidden-one", 60)

    def test_a_row_outside_it_cannot_be_deleted(self, client):
        pk = a_schedule("hidden-one").pk

        response = client.post(reverse(DELETE, args=[pk]), {"post": "yes"})

        assert response.status_code == 302
        assert OxSchedule.objects.filter(pk=pk).exists()

    def test_the_actions_leave_a_row_outside_it_alone(self, client):
        inside = a_schedule("visible").pk
        outside = a_schedule("hidden-one").pk

        act(client, "disable_selected", [inside, outside])
        assert enabled(inside) is False
        assert enabled(outside) is True

        act(client, "enable_selected", [inside, outside])
        assert enabled(inside) is True

        OxSchedule.objects.filter(pk=outside).update(enabled=False)
        act(client, "enable_selected", [outside])
        assert enabled(outside) is False

    def test_run_once_now_leaves_a_row_outside_it_alone(self, client):
        from django_ox.models import OxTask

        outside = a_schedule("hidden-one").pk
        before = OxTask.objects.count()

        act(client, "run_once_now", [outside])

        assert OxTask.objects.count() == before

    def test_delete_selected_leaves_a_row_outside_it_alone(self, client):
        inside = a_schedule("visible").pk
        outside = a_schedule("hidden-one").pk

        act(client, "delete_selected", [inside, outside])
        client.post(
            reverse(CHANGELIST),
            {
                "action": "delete_selected",
                "_selected_action": [str(inside), str(outside)],
                "post": "yes",
            },
        )

        remaining = set(OxSchedule.objects.values_list("pk", flat=True))
        assert remaining == {outside}
