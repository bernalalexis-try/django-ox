"""
Who may delete a stored schedule from the admin.

A registry entry can ask for a permission of its own, and then holding the
admin's permissions on the schedule table is not enough to change or delete
a schedule of that entry. The pages ask it, so that a person is not offered
a deletion they may not make. But a page's answer is about the row as the
page read it, and it is the page's to give: a row can be given a guarded
task key between the check and the deletion, and a project's own subclass
can decide the page differently.

So the deletion does not rest on the page. Each way the admin deletes hands
the person to django_ox.stored, which asks the entry's permission of the
row as the database holds it, under the row's lock, and deletes nothing it
refuses: `delete_schedule` for a row, and for a selection the same asked of
every row before any is deleted. There are four ways: the delete page of a
row that reads, the delete page of one that does not, which the recovery
page's Delete leads to, and "Delete selected" over rows of either kind.

Each is driven through the admin as a person drives it, by someone who
holds every permission on the schedule table and not the entry's.
"""

import pytest
from django.contrib.admin.models import LogEntry
from django.contrib.admin.sites import site
from django.contrib.auth.models import Permission, User
from django.core.exceptions import PermissionDenied
from django.db import connection
from django.urls import reverse

from django_ox.admin import OxScheduleAdmin
from django_ox.models import OxSchedule

from .test_schedule_admin_unreadable import (
    CHANGELIST,
    DELETE,
    EVERY_PERMISSION,
    ONE_PER_ENGINE,
    _registry,  # noqa: F401 (a fixture)
    a_schedule,
    clock,  # noqa: F401 (a fixture)
    set_column,
    staff,
)
from .test_schedule_admin_unreadable_actions import everything_held, held, marker


def a_row(name, *, reads, task_key):
    """A stored schedule of this entry, with or without a value that does not read."""
    row = a_schedule(name, task_key=task_key)
    if not reads:
        column, literal = ONE_PER_ENGINE[connection.vendor]
        set_column(row.pk, column, literal)
    return row.pk


def from_its_delete_page(client, pk):
    return client.post(reverse(DELETE, args=[pk]), {"post": "yes"})


def by_delete_selected(client, pk):
    return client.post(
        reverse(CHANGELIST),
        {"action": "delete_selected", "_selected_action": [str(pk)], "post": "yes"},
    )


#: (how the row is deleted, whether the row reads). The delete page of a row
#: that reads is Django's own, ending in delete_model; that of a row that
#: does not is the one the recovery page's Delete leads to; "Delete
#: selected" ends in delete_queryset for both.
WAYS = [
    pytest.param(from_its_delete_page, True, id="delete-page-of-a-row-that-reads"),
    pytest.param(
        from_its_delete_page, False, id="delete-page-of-a-row-that-does-not-read"
    ),
    pytest.param(by_delete_selected, True, id="delete-selected-a-row-that-reads"),
    pytest.param(
        by_delete_selected, False, id="delete-selected-a-row-that-does-not-read"
    ),
]


def assert_nothing_was_deleted(response, pk, before, told):
    assert response.status_code == 403
    assert held(pk, "id") is not None, "the row was deleted"
    assert everything_held(pk) == before
    assert marker() == told, "the workers were told of a deletion not made"
    assert not LogEntry.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(("delete", "reads"), WAYS)
class TestDeletingARowOfAGuardedEntry:
    def test_without_the_entrys_permission_it_is_refused(self, client, delete, reads):
        pk = a_row("payroll", reads=reads, task_key="restricted")
        before, told = everything_held(pk), marker()
        client.force_login(staff(*EVERY_PERMISSION))

        response = delete(client, pk)

        assert_nothing_was_deleted(response, pk, before, told)

    def test_the_write_refuses_it_whatever_the_page_let_through(
        self, client, monkeypatch, delete, reads
    ):
        # The pages' own check stood aside, as a subclass that decides the
        # page differently would have it. What is left is the write.
        monkeypatch.setattr(
            OxScheduleAdmin, "_registry_permitted", lambda self, request, obj: True
        )
        pk = a_row("payroll", reads=reads, task_key="restricted")
        before, told = everything_held(pk), marker()
        client.force_login(staff(*EVERY_PERMISSION))

        response = delete(client, pk)

        assert_nothing_was_deleted(response, pk, before, told)

    def test_a_row_given_a_guarded_key_after_the_page_checked_it_is_refused(
        self, client, monkeypatch, delete, reads
    ):
        # Anyone may delete the row as the page reads it. By the time it is
        # deleted it names the guarded entry, and the write is asked by the
        # row it finds under the lock.
        pk = a_row("report", reads=reads, task_key="report")
        logged = OxScheduleAdmin.log_deletions
        done = []

        def log_deletions(self, request, queryset):
            # The last thing each of the four does before it deletes.
            if not done:
                done.append(True)
                set_column(pk, "task_key", "'restricted'")
            return logged(self, request, queryset)

        monkeypatch.setattr(OxScheduleAdmin, "log_deletions", log_deletions)
        told = marker()
        client.force_login(staff(*EVERY_PERMISSION))

        response = delete(client, pk)

        assert done, "the deletion was refused before it was reached"
        assert response.status_code == 403
        assert held(pk, "id") is not None, "the row was deleted"
        assert marker() == told
        assert not LogEntry.objects.exists()

    def test_with_the_entrys_permission_it_is_deleted(self, client, delete, reads):
        pk = a_row("payroll", reads=reads, task_key="restricted")
        told = marker()
        user = staff(*EVERY_PERMISSION)
        user.user_permissions.add(Permission.objects.get(codename="view_user"))
        client.force_login(User.objects.get(pk=user.pk))

        response = delete(client, pk)

        assert response.status_code == 302
        assert held(pk, "id") is None
        assert marker() > told, "the workers were not told"
        assert LogEntry.objects.count() == 1


@pytest.mark.django_db
class TestDeleteSelectedOverSeveralRows:
    def test_one_row_the_person_may_not_delete_takes_the_selection_back_with_it(
        self, client, monkeypatch
    ):
        # A row anyone may delete has the lower key and is asked first. It
        # is not deleted when the next one is refused: every row is asked
        # before any goes, and the workers are told of nothing.
        monkeypatch.setattr(
            OxScheduleAdmin, "_registry_permitted", lambda self, request, obj: True
        )
        first = a_row("anyone", reads=True, task_key="report")
        second = a_row("payroll", reads=True, task_key="restricted")
        told = marker()
        client.force_login(staff(*EVERY_PERMISSION))

        response = client.post(
            reverse(CHANGELIST),
            {
                "action": "delete_selected",
                "_selected_action": [str(first), str(second)],
                "post": "yes",
            },
        )

        assert response.status_code == 403
        assert held(first, "id") is not None, "the first row stayed deleted"
        assert held(second, "id") is not None
        assert marker() == told
        assert not LogEntry.objects.exists()

    def test_delete_queryset_called_directly_asks_for_the_person_too(self, rf):
        # A project's own action or view that ends in delete_queryset has
        # no page of this admin in front of it at all.
        model_admin = site._registry[OxSchedule]
        first = a_row("anyone", reads=True, task_key="report")
        second = a_row("payroll", reads=True, task_key="restricted")
        told = marker()
        request = rf.post("/")
        request.user = staff(*EVERY_PERMISSION)
        both = OxSchedule.objects.filter(pk__in=[first, second])

        with pytest.raises(PermissionDenied):
            model_admin.delete_queryset(request, both)

        assert held(first, "id") is not None
        assert held(second, "id") is not None
        assert marker() == told
        request.user = User.objects.create_superuser("root", "root@example.com", "pw")
        model_admin.delete_queryset(request, both)
        assert held(first, "id") is None
        assert held(second, "id") is None
        assert marker() > told
