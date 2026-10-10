"""
A project's own subclass of the schedule admin that lays the form out itself.

The schedule form writes ten fields and shows three values it does not
write: the start time and the two record times. A subclass that sets `fields`
or `fieldsets` can name those three as the model does, `start_time`,
`created_at` and `updated_at`. Each is an editable field of the model, so
left to Django each would be an input: required on the add form, and on a
save handed to django_ox.stored, which writes none of them and refuses the
submission. Named in a layout, each is drawn read-only instead, and the form
saves what it always saved.
"""

from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.urls import reverse
from django.utils import timezone

from django_ox.admin import OxScheduleAdmin
from django_ox.models import OxSchedule
from django_ox.registry import ScheduleKind, register
from django_ox.stored import create_schedule

from . import tasks

pytestmark = pytest.mark.django_db

#: The fields the form writes.
WRITTEN = {
    "name",
    "task_key",
    "trigger",
    "cron",
    "every_seconds",
    "phase_seconds",
    "arguments",
    "enabled",
    "end_time",
    "starting_deadline_seconds",
}
#: The three it shows and does not write, by their model fields.
SHOWN = {"start_time", "created_at", "updated_at"}

#: The admin sites of tests/urls_schedule_layouts.py: the form laid out in
#: `fieldsets`, and in `fields`.
LAYOUTS = ["sections", "listed"]


@pytest.fixture(autouse=True)
def _registry(monkeypatch):
    monkeypatch.setattr("django_ox.registry._registry", {})
    monkeypatch.setattr("django_ox.registry._discovered", True)
    register(ScheduleKind(key="report", task=tasks.add))


@pytest.fixture(autouse=True)
def _sites(settings):
    settings.ROOT_URLCONF = "tests.urls_schedule_layouts"


def a_schedule():
    return create_schedule(
        name="nightly",
        task_key="report",
        trigger="cron",
        cron="0 2 * * *",
        start_time=timezone.now() - timedelta(days=1),
    )


def drawn(response):
    """The form a page drew: the names of its inputs and of its read-only values."""
    adminform = response.context_data["adminform"]
    return set(adminform.form.fields), set(adminform.readonly_fields)


def as_drawn(response):
    """What a browser posts back from a drawn form: every input, with its value."""
    bound = response.context_data["adminform"].form
    data = {}
    for name, field in bound.fields.items():
        value = bound[name].value()
        if hasattr(field.widget, "decompress"):
            for index, part in enumerate(field.widget.decompress(value)):
                data[f"{name}_{index}"] = "" if part is None else str(part)
        elif value is True:
            data[name] = "on"
        elif value is not False:
            data[name] = "" if value is None else str(value)
    return data


@pytest.mark.parametrize("layout", LAYOUTS)
class TestALayoutThatNamesTheValuesTheFormDoesNotWrite:
    def test_the_change_form_draws_them_read_only(self, admin_client, layout):
        row = a_schedule()

        response = admin_client.get(
            reverse(f"{layout}:django_ox_oxschedule_change", args=[row.pk])
        )

        assert response.status_code == 200
        inputs, read_only = drawn(response)
        assert inputs == WRITTEN
        assert read_only >= SHOWN
        page = response.content.decode()
        for name in SHOWN:
            assert f'name="{name}' not in page

    def test_the_change_form_saves_as_it_was_drawn(self, admin_client, layout):
        row = a_schedule()
        url = reverse(f"{layout}:django_ox_oxschedule_change", args=[row.pk])
        submission = as_drawn(admin_client.get(url))
        submission["cron"] = "5 4 * * *"

        response = admin_client.post(url, submission)

        assert response.status_code == 302, response.context_data["errors"]
        saved = OxSchedule.objects.get(pk=row.pk)
        assert saved.cron == "5 4 * * *"
        assert saved.created_at == row.created_at

    def test_the_add_form_asks_for_none_of_them(self, admin_client, layout):
        url = reverse(f"{layout}:django_ox_oxschedule_add")
        page = admin_client.get(url)
        inputs, read_only = drawn(page)
        assert inputs == WRITTEN
        assert read_only >= SHOWN
        before = timezone.now()

        response = admin_client.post(
            url,
            {
                "name": "nightly",
                "task_key": "report",
                "trigger": "cron",
                "cron": "0 2 * * *",
                "arguments": "{}",
                "phase_seconds": 0,
                "enabled": "on",
            },
        )

        assert response.status_code == 302, response.context_data["errors"]
        added = OxSchedule.objects.get(name="nightly")
        # Written by django_ox.stored, as on the package's own form.
        assert added.start_time >= before
        assert added.boundary_for


class TestThePackagesOwnLayout:
    def test_names_the_three_once_each_through_its_methods(self, admin_client):
        row = a_schedule()

        response = admin_client.get(
            reverse("admin:django_ox_oxschedule_change", args=[row.pk])
        )

        inputs, read_only = drawn(response)
        assert inputs == WRITTEN
        assert read_only == {"starts", "created", "updated"}

    def test_a_subclass_that_declares_no_layout_gets_the_same(self, rf, admin_user):
        class Plain(OxScheduleAdmin):
            pass

        request = rf.get("/")
        request.user = admin_user

        assert Plain(OxSchedule, site).get_readonly_fields(request) == (
            "starts",
            "created",
            "updated",
        )

    def test_a_layout_that_names_one_of_them_gets_that_one(self, rf, admin_user):
        class StartOnly(OxScheduleAdmin):
            fields = ("name", "task_key", "trigger", "cron", "start_time")

        request = rf.get("/")
        request.user = admin_user

        assert StartOnly(OxSchedule, site).get_readonly_fields(request) == (
            "starts",
            "created",
            "updated",
            "start_time",
        )
