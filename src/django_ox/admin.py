"""
Django admin for the task table.

Loaded by the admin's autodiscover, so it is only imported when
django.contrib.admin is installed; a project without the admin never sees
this module and needs nothing from it. The list shows what a worker would
see; the detail page is read-only, with every attempt's traceback; and two
actions call django_ox.actions.retry_many and discard_many on the selected
rows, one conditional UPDATE per thousand rows in one transaction, and
report counts.

Every page here reads the database its rows are written to, and both
get_queryset methods below say so. A ModelAdmin builds its queryset from the
default manager, which follows db_for_read. Under a router that sends reads
to a replica these pages would answer from a database no worker writes, and
the change form submits what it rendered.

The queue overview is a separate page linked from the task list. It calls
metrics.collect() once per visit, on the same alias; the change list itself
runs none of those aggregate queries.
"""

from __future__ import annotations

import logging
import threading
from copy import copy
from datetime import UTC, date, datetime, time
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from django import forms
from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.admin.utils import (
    NestedObjects,
    display_for_value,
    flatten_fieldsets,
    model_ngettext,
    quote,
)
from django.contrib.admin.views.main import ChangeList
from django.contrib.admin.widgets import AdminSplitDateTime
from django.core.exceptions import NON_FIELD_ERRORS, PermissionDenied, ValidationError
from django.db import router, transaction
from django.db.models import CharField, DateTimeField, Model, QuerySet, TextField
from django.http import HttpRequest, HttpResponse
from django.template.response import TemplateResponse
from django.urls import NoReverseMatch, path, reverse
from django.utils import formats, timezone
from django.utils.decorators import method_decorator
from django.utils.html import format_html, format_html_join
from django.utils.module_loading import import_string
from django.utils.text import capfirst
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_safe

from . import actions, metrics, registry, stored
from ._stored_read import (
    TickRead,
    UnreadableRow,
    is_unreadable_value,
    newest_ticks,
    raw,
    read_schedules,
)
from .compat import DEFAULT_TASK_BACKEND_ALIAS
from .exceptions import StoredValueUnreadable
from .models import OxSchedule, OxTask
from .schedules import STORED_KEY_PREFIX

logger = logging.getLogger("django_ox")

# What the schedule actions report, all of it in this block. `count` is how
# many of the selected rows a line is about, and `rows` names them, each by
# its name and its key.

#: "Enable selected schedules", of the rows it left disabled because they do
#: not validate.
_COULD_NOT_ENABLE = (
    "Left {count} schedule(s) disabled. Correct them before enabling them: {rows}."
)
#: The same, of the rows it left alone because a stored value in them does
#: not read. Not "disabled": what such a row's `enabled` holds may be the
#: value that does not read.
_UNREADABLE_NOT_ENABLED = (
    "Left {count} schedule(s) unchanged because they hold a stored value django-ox "
    "cannot use: {rows}. Open each schedule for its repair options before enabling it."
)
#: "Run selected schedules once now", of the rows that read and cannot be
#: built into a task.
_CANNOT_RUN = "Skipped {count} schedule(s) that cannot run as written: {rows}."
#: The same, of the rows a stored value in which does not read.
_UNREADABLE_NOT_RUN = (
    "Skipped {count} schedule(s) holding a stored value django-ox cannot use: {rows}. "
    "Open each schedule for its repair options."
)
#: Any of the three, of the rows the person may not change.
_NO_PERMISSION = (
    "Skipped {count} schedule(s) you do not have permission to change. If a task_key "
    "cannot be read or isn't registered here, only an active superuser can change its "
    "schedule."
)
#: Any of the three, of the rows deleted between the selection and the action.
_NO_LONGER_THERE = "Skipped {count} schedule(s) that no longer exist."
#: Enabling or running schedules whose newest tick does not read. `keys` is
#: what to hand ox_prune to remove those ticks.
_TICKS_UNREADABLE_AFTER = (
    "Unreadable tick history may block automatic dispatch for {count} of these "
    "schedule(s). Ask an operator to stop the workers that dispatch these schedules. "
    "Use ox_prune --purge-unreadable-ticks with the target options listed below, "
    "adding --dry-run first. Check the listed rows and the tasks those ticks recorded "
    "before running without --dry-run, then start the workers again. Removing history "
    "can repeat a tick or cause re-anchoring that skips the next tick. Target options "
    '(up to 10 schedules shown; "and N more" is not command syntax): {keys}.'
)
#: The most rows, or keys, a line names before it counts the rest.
_NAMED_AT_MOST = 10
#: The rest. `named` is the ones the line names and `more` how many it does not.
_AND_MORE = "{named} and {more} more"
#: The confirmation page of "Delete selected", of the selected rows a stored
#: value in which does not read.
_DELETING_UNREADABLE_ROWS = (
    "{count} of the selected schedule(s) hold a stored value django-ox cannot use: "
    "{rows}. Deleting them removes the rows by primary key."
)

# What the schedule pages say about a stored value that cannot be read, and
# about a time the requested zone cannot show, all of it in this block.

#: A changelist cell standing in for a schedule that did not read. `fields`
#: names the fields that did not, or is _UNKNOWN_FIELD.
_UNREADABLE_VALUE = "Stored values django-ox cannot use: {fields}"
#: The same, when the read could not say which field it was.
_UNKNOWN_FIELD = "unknown field"
#: A cell for one field that did not read.
_UNREADABLE_CELL = "Unreadable stored value"
#: The title of the page that stands in for such a schedule's change form.
_UNREADABLE_TITLE = "Stored schedule needs recovery"
#: The Enabled cell when the column holds something other than true or false.
_INVALID_ENABLED = "Invalid enabled value"
#: The Last tick cell when the newest tick did not read.
_UNREADABLE_TICKS = "Unreadable tick history"
#: The Last tick cell when the schedule has no tick at all.
_NO_TICKS = "never"
#: A time the requested zone cannot show, shown in UTC instead. `when` is the
#: time, formatted, and `zone` the zone it could not be shown in.
_SHOWN_IN_UTC = "{when} UTC (outside the dates {zone} can show)"
#: The label of an editable time shown and read in UTC for the same reason.
_EDITED_IN_UTC_LABEL = "{label} (UTC)"
#: Its help text. `zone` as above.
_EDITED_IN_UTC_HELP = (
    "This time is outside the dates {zone} can show, so it is shown and entered in UTC."
)
#: The change form of a readable schedule whose newest tick did not read.
#: `key` is the name the tick log knows the schedule by.
_TICKS_UNREADABLE_WARNING = (
    "This schedule's tick history holds a value that cannot be read, which may block "
    "automatic dispatch. Ask an operator to stop the workers that dispatch this "
    "schedule. Run ox_prune --purge-unreadable-ticks --schedule-key {key} with "
    "--dry-run first. Check the listed rows and the tasks those ticks recorded before "
    "running without --dry-run, then start the workers again. Removing history can "
    "repeat a tick or cause re-anchoring that skips the next tick."
)
#: The delete confirmation of a schedule that did not read. `reason` says why.
_DELETING_UNREADABLE = (
    "This schedule holds a stored value django-ox cannot use: {reason}. Deleting it "
    "removes the row by primary key."
)
#: The change form of a schedule that holds a value that does not read and
#: that saving the form replaces. `reason` names each such field and says why.
_SAVING_REPLACES = (
    "This schedule holds a stored value that cannot be read ({reason}). Saving "
    "this form replaces it and sets the start time to now."
)
#: The form's error when the save was refused because the row holds a value
#: that does not read and that the form does not replace. `reason` as above.
_SAVE_LEFT_UNREADABLE = (
    "Nothing was saved. This schedule holds a stored value django-ox cannot use, which "
    "this form does not replace: {reason}. You can still disable or delete the "
    "schedule."
)
#: The form's error for a refusal about a field the form does not carry.
#: `field` is the field's name and `message` what was said of it.
_REFUSED_OFF_THE_FORM = "{field}: {message}"
#: Logged the first time the admin meets a schedule that did not read.
_ROW_UNREADABLE_LOG = (
    "The admin found a stored value django-ox cannot use in schedule %s: %s"
)
#: Logged after that, at most once a ROW_REPORT_INTERVAL.
_ROW_STILL_UNREADABLE_LOG = (
    "The admin still finds a stored value django-ox cannot use in schedule %s (%d more "
    "times since the last report): %s"
)
#: Logged when a schedule's newest tick did not read. The schedule's tick
#: key, then why.
_TICK_UNREADABLE_LOG = (
    "The admin cannot read the newest tick of schedule %s: %s. The Last tick column "
    'shows "Unreadable tick history".'
)
#: Logged after that, at most once a ROW_REPORT_INTERVAL.
_TICK_STILL_UNREADABLE_LOG = (
    "The admin still cannot read the newest tick of schedule %s (%d more times "
    "since the last report): %s"
)
#: Logged when a time is shown in UTC because the requested zone cannot show it.
_DATETIME_FALLBACK_LOG = (
    "The admin showed %s in UTC: the time is outside the dates %s can show."
)

#: The fields the schedule form shows read-only, through `starts`, `created`
#: and `updated`. The form writes none of them, so a layout that names one
#: by its field gets it read-only too (`OxScheduleAdmin.get_readonly_fields`).
_NEVER_INPUTS = ("start_time", "created_at", "updated_at")

if TYPE_CHECKING:
    _ModelAdmin = admin.ModelAdmin[OxTask]
    _ScheduleAdmin = admin.ModelAdmin[OxSchedule]
    _ScheduleForm = forms.ModelForm[OxSchedule]
else:
    _ModelAdmin = admin.ModelAdmin
    _ScheduleAdmin = admin.ModelAdmin
    _ScheduleForm = forms.ModelForm

ERROR_TEMPLATE = (
    '<p><strong>Attempt {}: {}</strong></p><pre style="white-space: pre-wrap">{}</pre>'
)


def _format_age(seconds: float | None) -> str:
    """A compact duration for the overview's operator-facing table."""
    if seconds is None:
        return "—"
    remaining = max(0, int(seconds))
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, seconds = divmod(remaining, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or parts:
        parts.append(f"{hours}h")
    if minutes or parts:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


@admin.register(OxTask)
class OxTaskAdmin(_ModelAdmin):
    list_display = (
        "id",
        "task_path",
        "queue_name",
        "status",
        "attempts",
        "enqueued_at",
        "finished_at",
    )
    list_filter = ("status", "queue_name")
    search_fields = ("id", "task_path")
    ordering = ("-enqueued_at",)
    date_hierarchy = "enqueued_at"
    actions = ("retry_selected", "discard_selected")
    readonly_fields = (
        "id",
        "task_path",
        "args",
        "kwargs",
        "queue_name",
        "priority",
        "takes_context",
        "backend_name",
        "status",
        "run_after",
        "attempts",
        "max_attempts",
        "return_value",
        "worker_ids",
        "enqueued_at",
        "started_at",
        "last_attempted_at",
        "finished_at",
        "locked_by",
        "locked_at",
        "lease_expires_at",
        "lease_epoch",
        "attempt_errors",
    )
    fieldsets = (
        (None, {"fields": ("id", "task_path", "args", "kwargs", "status")}),
        (
            "Queue",
            {
                "fields": (
                    "queue_name",
                    "priority",
                    "backend_name",
                    "takes_context",
                    "run_after",
                )
            },
        ),
        (
            "Attempts",
            {
                "fields": (
                    "attempts",
                    "max_attempts",
                    "worker_ids",
                    "return_value",
                    "attempt_errors",
                )
            },
        ),
        (
            "Timing",
            {
                "fields": (
                    "enqueued_at",
                    "started_at",
                    "last_attempted_at",
                    "finished_at",
                )
            },
        ),
        (
            "Lease",
            {
                "fields": (
                    "locked_by",
                    "locked_at",
                    "lease_expires_at",
                    "lease_epoch",
                )
            },
        ),
    )

    # Rows are written by workers, by django_ox.actions and by
    # django_ox._waiting. The admin can read them and run the two actions; it
    # cannot add, edit or delete one, because a hand-edited status would
    # bypass the lease and a delete could take a row from under a running
    # worker. ox_prune deletes.
    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(
        self, request: HttpRequest, obj: OxTask | None = None
    ) -> bool:
        return False

    def has_delete_permission(
        self, request: HttpRequest, obj: OxTask | None = None
    ) -> bool:
        return False

    def get_queryset(self, request: HttpRequest) -> QuerySet[OxTask]:
        """
        The alias the task rows are written to, for every page here.

        A changelist, a detail page and the queryset an action is handed all
        start here, so this one line decides the database all of them read.
        Left to `db_for_read`, a router that splits reads sends this page to
        a replica: a task enqueued a moment ago is not in the list, and the
        detail page for one that exists reports it as deleted.
        """
        return super().get_queryset(request).using(router.db_for_write(self.model))

    def get_urls(self) -> list[Any]:
        urls = super().get_urls()
        overview = self.admin_site.admin_view(self.overview)
        return [
            path("overview/", overview, name="django_ox_oxtask_overview"),
            *urls,
        ]

    @method_decorator(require_safe)
    def overview(self, request: HttpRequest) -> HttpResponse:
        """The per-queue readings, collected once from the write alias."""
        if not self.has_view_permission(request):
            raise PermissionDenied

        alias = router.db_for_write(self.model)
        families = {
            family.name: family.samples for family in metrics.collect(using=alias)
        }
        statuses: dict[str, dict[str, int]] = {}
        for labels, value in families["django_ox_tasks"]:
            statuses.setdefault(labels["queue"], {})[labels["status"]] = int(value)

        def by_queue(name: str) -> dict[str, float]:
            return {labels["queue"]: value for labels, value in families[name]}

        eligible = by_queue("django_ox_ready_tasks")
        oldest = by_queue("django_ox_oldest_ready_age_seconds")
        claims = by_queue("django_ox_last_claim_age_seconds")
        throughput = by_queue("django_ox_throughput_per_minute")
        failure = by_queue("django_ox_failure_rate")
        rows = []
        for queue_name, counts in sorted(statuses.items()):
            finished = queue_name in failure
            rows.append(
                {
                    "queue_name": queue_name,
                    "ready": counts.get("ready", 0),
                    "eligible_ready": int(eligible.get(queue_name, 0)),
                    "running": counts.get("running", 0),
                    "waiting": counts.get("waiting", 0),
                    "failed": counts.get("failed", 0),
                    "successful": counts.get("successful", 0),
                    "lost": counts.get("lost", 0),
                    "discarded": counts.get("discarded", 0),
                    "oldest_age": _format_age(oldest.get(queue_name)),
                    "throughput": (
                        f"{throughput[queue_name]:.2f}" if finished else "—"
                    ),
                    "failure_rate": f"{failure[queue_name]:.2%}" if finished else "—",
                    "last_claim_age": (
                        _format_age(claims[queue_name])
                        if queue_name in claims
                        else "never"
                    ),
                }
            )
        context = {
            **self.admin_site.each_context(request),
            "title": "Queue overview",
            "opts": self.opts,
            "rows": rows,
            "as_of": timezone.now(),
        }
        return TemplateResponse(
            request, "admin/django_ox/oxtask/queue_overview.html", context
        )

    @admin.display(description="Attempt errors")
    def attempt_errors(self, obj: OxTask) -> str:
        if not obj.errors:
            return "No errors recorded."
        return format_html_join(
            "",
            ERROR_TEMPLATE,
            (
                (index, error["exception_class_path"], error["traceback"])
                for index, error in enumerate(obj.errors, start=1)
            ),
        )

    def _apply(
        self,
        request: HttpRequest,
        queryset: QuerySet[OxTask],
        action: Any,
        verb: str,
    ) -> None:
        done, skipped = action(queryset)
        self.message_user(request, f"{verb} {done} task(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                format_html(
                    "Skipped {} task(s) whose status did not allow it.", skipped
                ),
                messages.WARNING,
            )

    @admin.action(description="Retry selected tasks", permissions=("retry_or_discard",))
    def retry_selected(self, request: HttpRequest, queryset: QuerySet[OxTask]) -> None:
        self._apply(request, queryset, actions.retry_many, "Retried")

    @admin.action(
        description="Discard selected tasks", permissions=("retry_or_discard",)
    )
    def discard_selected(
        self, request: HttpRequest, queryset: QuerySet[OxTask]
    ) -> None:
        self._apply(request, queryset, actions.discard_many, "Discarded")

    def has_retry_or_discard_permission(self, request: HttpRequest) -> bool:
        # The model's change permission, without the change form: the
        # actions are the only writes the admin offers.
        opts = self.opts
        return request.user.has_perm(f"{opts.app_label}.change_{opts.model_name}")


class _Reports:
    """
    What the schedule pages log about a value they keep meeting: in full the
    first time, then at most every stored.ROW_REPORT_INTERVAL seconds with
    how many times went unsaid, the way a worker reports a row it keeps
    skipping. Every page view of a broken row would otherwise log it again.

    One per process, shared by its request threads, so the bookkeeping is
    behind a lock. `clock` is time.monotonic outside tests.
    """

    def __init__(self, clock: Any = None) -> None:
        self._lock = threading.Lock()
        self._report = (
            stored._RowReport() if clock is None else stored._RowReport(clock)
        )

    def due(self, key: Any) -> tuple[bool, bool, int]:
        with self._lock:
            return self._report.due(key)


_reports = _Reports()


def _current_zone_name() -> str:
    return stored._printable(timezone.get_current_timezone_name())


def _report_datetime_fallback(field: str) -> None:
    zone = _current_zone_name()
    due, _, _ = _reports.due(("datetime", field, zone))
    if due:
        logger.warning(
            _DATETIME_FALLBACK_LOG,
            field,
            zone,
            extra={
                "event": "schedule_admin_datetime_fallback",
                "field": field,
                "zone": zone,
            },
        )


def _shows_in_current_zone(value: datetime) -> bool:
    """
    Whether the active time zone can show this time.

    A time is stored as an instant, and the database only had to hold it in
    UTC. Moved into a zone east of UTC, a time late on 31 December 9999 is
    in year 10000, and one west of it early on 1 January of year 1 is in
    year 0; datetime holds neither and raises. Which zone that is depends on
    TIME_ZONE and on whatever zone the request activated, so a row that
    showed yesterday can fail today, and no check at the write can rule it
    out.
    """
    if not settings.USE_TZ or timezone.is_naive(value):
        return True
    try:
        timezone.localtime(value)
    except OverflowError:
        return False
    return True


def _shown_time(value: datetime | None, field: str, empty: str) -> str:
    """
    A stored time as the admin shows a date and time field, or, where the
    active zone cannot show it, the same instant in UTC, labelled as such.
    `empty` is what the admin shows for no value.
    """
    if value is None:
        return empty
    if _shows_in_current_zone(value):
        if settings.USE_TZ and timezone.is_aware(value):
            value = timezone.localtime(value)
        return formats.localize(value)
    _report_datetime_fallback(field)
    return _SHOWN_IN_UTC.format(
        when=formats.localize(value.astimezone(UTC)), zone=_current_zone_name()
    )


class _AdminSplitDateTimeInUTC(AdminSplitDateTime):
    """The admin's date and time inputs, filled in UTC."""

    def decompress(self, value: Any) -> tuple[date | None, time | None]:
        with timezone.override(UTC):
            return super().decompress(value)


class _SplitDateTimeInUTC(forms.SplitDateTimeField):
    """A date and time typed in UTC, whatever zone the request is in."""

    widget = _AdminSplitDateTimeInUTC

    def compress(self, data_list: Any) -> datetime | None:
        with timezone.override(UTC):
            return super().compress(data_list)


class _UnreadableSchedule:
    """
    A stored schedule that holds a value that does not read, in its place on
    a schedule page.

    Not an OxSchedule. Django renders a model instance's fields with its own
    formatting, which reads every value, and a value that does not read is
    what raised. Every column of the changelist is a method of the admin
    that knows this class, and this carries what Django's changelist needs
    to list a row, link it and select it: the model's options, the primary
    key and a label.
    """

    _meta = OxSchedule._meta

    def __init__(self, row: UnreadableRow) -> None:
        self.row = row
        self.pk = self.id = row.pk
        #: The schedule's newest tick, as the changelist read it for the page.
        self.ox_last_tick: TickRead | None = None

    def value(self, field: str) -> Any:
        """
        A field's value as it read, or None when it did not. OxSchedule has
        no relations, so each field's name is its attribute's name.
        """
        return None if field in self.row.unreadable else self.row.values.get(field)

    def serializable_value(self, field: str) -> Any:
        return self.value(field)

    def __str__(self) -> str:
        return stored._row_label(self.pk, self.value("name"))


def _report_unreadable_row(row: UnreadableRow) -> None:
    due, first, unsaid = _reports.due(("row", row.alias, row.pk))
    if not due:
        return
    name = row.values.get("name")
    label = stored._row_label(row.pk, name)
    reason = stored._printable(row.reason)
    message, args = (
        (_ROW_UNREADABLE_LOG, (label, reason))
        if first
        else (_ROW_STILL_UNREADABLE_LOG, (label, unsaid, reason))
    )
    logger.warning(
        message,
        *args,
        extra={
            "event": "schedule_admin_row_unreadable",
            "database": row.alias,
            "schedule": stored._printable(name) if isinstance(name, str) else None,
            "schedule_pk": row.pk,
            "fields": list(row.fields),
            "reason": reason,
        },
    )


def _report_unreadable_tick(alias: str, pk: Any, tick: TickRead) -> None:
    due, first, unsaid = _reports.due(("tick", alias, tick.key))
    if not due:
        return
    key = stored._printable(tick.key)
    reason = stored._printable(tick.reason)
    message, args = (
        (_TICK_UNREADABLE_LOG, (key, reason))
        if first
        else (_TICK_STILL_UNREADABLE_LOG, (key, unsaid, reason))
    )
    logger.warning(
        message,
        *args,
        extra={
            "event": "schedule_tick_unreadable",
            "surface": "admin",
            "phase": "latest",
            "database": alias,
            "schedule_key": key,
            "schedule_pk": pk,
            "tick_pk": tick.pk,
            "reason": reason,
        },
    )


def _newest_ticks(pks: list[Any], alias: str) -> dict[Any, TickRead]:
    """
    Each stored schedule's newest tick, by primary key, in one statement.

    Looked up a schedule at a time inside that statement, each from the end
    of its own entries in the tick log's index, so a page costs the same
    however much history its schedules have. A newest tick that does not
    read is that schedule's answer alone and is reported; the others still
    read, and it is never passed over for an older tick that does.

    A schedule deleted since its key was read has no row to look a tick up
    for, and is answered as one with no tick.
    """
    ticks = newest_ticks(pks, using=alias) if pks else {}
    found = {}
    for pk in pks:
        tick = ticks.get(pk)
        if tick is None:
            tick = TickRead(f"{STORED_KEY_PREFIX}{pk}")
        elif tick.unreadable:
            _report_unreadable_tick(alias, pk, tick)
        found[pk] = tick
    return found


class _ScheduleChangeList(ChangeList):
    """
    The schedule changelist, its page read row by row.

    Django lists a page by reading it as one queryset, and one value in one
    row that the driver or Django cannot convert ends that read: no row is
    listed, healthy or not. Here the page is chosen exactly as Django
    chooses it, by the same search, filters, ordering and pagination, with
    only the primary keys read that way. The rows are then read by
    django_ox._stored_read, which reads every row that reads and names, in
    each one that does not, the fields that did not. Such a row keeps its
    place on the page and in the count.

    The newest tick of every schedule on the page is then read in one
    statement, for the Last tick column. A newest tick that does not read
    is "Unreadable tick history", never "never" and never an older tick.
    """

    def get_results(self, request: HttpRequest) -> None:
        super().get_results(request)
        alias = stored.schedule_db_alias()
        pks = list(self.result_list.values_list("pk", flat=True))
        rows = read_schedules(pks, using=alias)
        ticks = _newest_ticks(list(rows), alias)
        page: list[OxSchedule | _UnreadableSchedule] = []
        for pk, row in rows.items():
            shown: OxSchedule | _UnreadableSchedule
            if isinstance(row, UnreadableRow):
                _report_unreadable_row(row)
                shown = _UnreadableSchedule(row)
            else:
                shown = row
            # Where the Last tick column finds it.
            cast("Any", shown).ox_last_tick = ticks[pk]
            page.append(shown)
        self.result_list = page


class _Unreadable(Exception):
    """Raised by get_object for a stored schedule that does not read."""

    def __init__(self, row: UnreadableRow) -> None:
        super().__init__(row.reason)
        self.row = row


def _instance(row: OxSchedule | UnreadableRow) -> OxSchedule:
    """
    A stored schedule as an instance to act on by its key: the row where it
    reads, and otherwise the fields of it that did, the rest deferred.
    """
    return row.deferred_instance() if isinstance(row, UnreadableRow) else row


def _label(row: OxSchedule | UnreadableRow) -> str:
    """A stored schedule as a report names it, whether or not its name read."""
    name = row.values.get("name") if isinstance(row, UnreadableRow) else row.name
    return stored._row_label(row.pk, name)


def _named(names: list[str], separator: str = ", ") -> str:
    """The first of `names`, and how many more there are."""
    named = separator.join(names[:_NAMED_AT_MOST])
    more = len(names) - _NAMED_AT_MOST
    return named if more <= 0 else _AND_MORE.format(named=named, more=more)


#: The fields a save of the change form writes whatever they held: the
#: form's own, and those any change through django_ox.stored writes afresh.
_REPLACED_BY_A_SAVE = stored.WRITABLE_FIELDS | stored._WRITTEN_HERE


def _shown_in_the_form(row: UnreadableRow) -> OxSchedule | None:
    """
    A stored schedule that holds a value that does not read, as an instance
    the change form can still be drawn from, or None where it cannot.

    The form shows a row's values and writes every one of its own fields
    back, so it can be offered for such a row on two conditions. Every value
    that did not read is in a field a save of the form replaces. And Django's
    own read hands each of those values over as the database holds it, so
    that what the form shows is what is stored: text in a whole-number
    column on SQLite, an `enabled` of 2 on MySQL. A read that raises has
    nothing to show. One that reads something else, as SQLite reads a start
    that is not a date as no start and an `enabled` of 2 as false, would
    show a value the row does not hold and write it back.

    A date and time that did not read is never shown this way, whatever
    Django's read hands over. The form draws one with widgets and formats
    that take a datetime and nothing else, and with USE_TZ off MySQL's
    driver hands a zero date over as the text it holds, with nothing
    raised: drawn from that, the page is a server error.

    The instance carries the row as it was found, for the form's warning.
    """
    fields = [
        model_field
        for model_field in OxSchedule._meta.concrete_fields
        if model_field.name in row.unreadable
    ]
    if not fields or any(f.name not in _REPLACED_BY_A_SAVE for f in fields):
        return None
    if any(isinstance(f, DateTimeField) for f in fields):
        return None
    # Each of those fields twice in the one statement: as Django's read
    # converts it, on the instance, and as the driver hands it over.
    held = {f"ox_held_{f.attname}": raw(f.attname) for f in fields}
    query = OxSchedule.objects.using(row.alias).filter(pk=row.pk).annotate(**held)
    try:
        found = list(query)
    except Exception as exc:
        if not is_unreadable_value(exc, using=row.alias):
            raise
        return None
    if not found:
        return None
    instance = found[0]
    for model_field in fields:
        read = getattr(instance, model_field.attname)
        as_held = instance.__dict__.pop(f"ox_held_{model_field.attname}")
        if type(read) is not type(as_held) or read != as_held:
            return None
        if isinstance(model_field, (CharField, TextField)) and not isinstance(
            read, str
        ):
            # Bytes SQLite kept in a text column: a text widget would draw
            # their repr, and a save would store that text in their place.
            return None
    cast("Any", instance).ox_unreadable = row
    return instance


class _SaveRefused(ValidationError):
    """
    Raised out of save_model when django_ox.stored refuses the write.

    Out of save_model, and so out of the transaction Django opens around a
    submitted form, which rolls back. changeform_view answers it by drawing
    the form again on `as_read`, the row as the form was built from it, with
    `refusals`: what the form is to say, by field, None for no one field.

    A ValidationError holding the same, so that code which calls save_model
    itself and catches the service layer's refusal catches this one.
    """

    def __init__(
        self, as_read: OxSchedule | None, refusals: dict[str | None, list[Any]]
    ) -> None:
        super().__init__(
            {field or NON_FIELD_ERRORS: errors for field, errors in refusals.items()}
        )
        self.as_read = as_read
        self.refusals = refusals


class _RowGone(OxSchedule.DoesNotExist):
    """
    Raised out of save_model when the row was deleted before the write.

    changeform_view answers it by running Django's view once more, which
    finds no row and says so. An OxSchedule.DoesNotExist still, to code that
    calls save_model itself.
    """


#: Where changeform_view leaves a refused save on the request, for the pass
#: of Django's view that draws the form again: the row as the form was built
#: from it, and what the form is to say.
_REFUSED_SAVE = "_ox_refused_save"


def _refused_save(
    request: HttpRequest,
) -> tuple[OxSchedule | None, dict[str | None, list[Any]]] | None:
    return cast(
        "tuple[OxSchedule | None, dict[str | None, list[Any]]] | None",
        getattr(request, _REFUSED_SAVE, None),
    )


def _refusals(form: Any, exc: ValidationError) -> dict[str | None, list[Any]]:
    """
    What django_ox.stored refused, as errors the form can carry.

    A refusal about one of the form's fields stays on that field, as the
    field's own cleaning would have put it, code and params included. One
    about a field the form does not carry, the start time among them, has no
    input to sit beside, so it is said of the form with the field's name in
    front.
    """
    found = (
        exc.error_dict
        if hasattr(exc, "error_dict")
        else {NON_FIELD_ERRORS: exc.error_list}
    )
    errors: dict[str | None, list[Any]] = {}
    for name, refused in found.items():
        if name == NON_FIELD_ERRORS:
            errors.setdefault(None, []).extend(refused)
        elif name in form.fields:
            errors.setdefault(name, []).extend(refused)
        else:
            errors.setdefault(None, []).extend(
                ValidationError(
                    _REFUSED_OFF_THE_FORM.format(field=name, message=message)
                )
                for error in refused
                for message in error.messages
            )
    return errors


class OxScheduleForm(_ScheduleForm):
    """
    The change form for a stored schedule.

    `task_key` is a choice drawn from the registry rather than a text
    input. That is the difference between this admin and every other
    Django scheduling admin: the field cannot express a task the code did
    not expose, so holding the change permission here is not permission to
    run any importable callable.

    A ChoiceField validates membership on the server, so a hand-made POST
    naming an unexposed task is refused here and not merely absent from the
    rendered select. The model checks membership again in its own clean(),
    which is what covers the write paths that never build a form:
    objects.create(), a data migration, a fixture.

    The field is a choice rather than a text input because a text input
    would let anyone holding the change permission name any importable
    callable.

    The choices are every registered key, not the ones this user may
    schedule. Narrowing them per user would make the change form for an
    existing restricted row fail validation on the key the row already
    holds, and it would hide which keys exist rather than saying what is
    needed to use one. The refusal is a field error instead.
    """

    #: The person submitting, when there is one. OxScheduleAdmin.get_form
    #: binds it; a form built any other way has none, and then the check
    #: below is skipped exactly as it is for a write with no `user`.
    user: Any = None

    #: What django_ox.stored refused when this submission was saved, by
    #: field, None for no one field. OxScheduleAdmin.get_form binds it on
    #: the pass that draws the form again; on every other form it is None.
    refused: dict[str | None, list[Any]] | None = None

    class Meta:
        model = OxSchedule
        fields = (
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
        )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        #: The row as this form was built from it. Validation writes the
        #: submission onto `instance`, and a form drawn again after a
        #: refused save has to be drawn from the row, not from that.
        self.as_read = copy(self.instance) if self.instance.pk is not None else None
        keys = sorted(registry.kinds())
        self.fields["task_key"] = forms.ChoiceField(
            choices=[(key, key) for key in keys],
            label="Task",
            help_text=(
                "Tasks the code has exposed with @schedulable or "
                "SCHEDULABLE_TASKS. Nothing else can be scheduled."
                if keys
                else "No tasks are exposed yet. django-ox imports each "
                "installed app's `tasks` module, so @schedulable takes "
                "effect there; from any other module, name the task in "
                "OPTIONS['SCHEDULABLE_TASKS']."
            ),
        )
        self._end_time_in_utc_if_the_zone_cannot_show_it()

    def _end_time_in_utc_if_the_zone_cannot_show_it(self) -> None:
        """
        Show and read the end time in UTC when the active zone cannot show it.

        The admin's date and time inputs show a time in the active zone, and
        a stored end late on 31 December 9999 is in year 10000 east of UTC,
        which a datetime cannot hold. In that zone the inputs can be neither
        drawn nor compared with what is submitted, so the value most likely
        to need changing is the one the form cannot get past. In UTC the
        same instant fits, and the label says which zone the inputs are in.

        Which zone the inputs are read in is decided here, from the row as it
        is stored, on the request that shows the form and on the one that
        submits it, never from anything the submission carries. Submitted
        unchanged, the inputs read back as the same instant; emptied, the
        schedule has no end.
        """
        field = self.fields.get("end_time")
        stored_end = self.instance.end_time if self.instance.pk is not None else None
        if (
            not isinstance(field, forms.SplitDateTimeField)
            or stored_end is None
            or _shows_in_current_zone(stored_end)
        ):
            return
        _report_datetime_fallback("end_time")
        zone = _current_zone_name()
        self.fields["end_time"] = _SplitDateTimeInUTC(
            required=field.required,
            label=_EDITED_IN_UTC_LABEL.format(label=field.label),
            help_text=_EDITED_IN_UTC_HELP.format(zone=zone),
        )

    def _effective_start_time(self, cleaned: dict[str, Any]) -> Any:
        """
        The activation boundary this submission will end up with.

        `start_time` is not a form field. The service layer sets it: to now
        when a schedule is created, retimed or re-enabled, and to whatever
        the row already held otherwise. Django excludes a field the form
        does not carry from its own validation, so without this the pair is
        never compared on the way in.
        """
        if self.instance.pk is None:
            return timezone.now()
        if getattr(self.instance, "ox_unreadable", None) is not None:
            # A row that held a value that did not read: what it held is not
            # known, so the save gives it a boundary of now, as a retime does.
            return timezone.now()
        moved = any(
            cleaned.get(field) != getattr(self.instance, field)
            for field in stored.TIMING_FIELDS
        ) or (cleaned.get("enabled") and not self.instance.enabled)
        return timezone.now() if moved else self.instance.start_time

    def _check_the_registry_permission(self, cleaned: dict[str, Any]) -> None:
        """
        Put a registry entry's own permission in front of the person.

        django_ox.stored enforces it on every write path, form or no form,
        and that is where the guarantee lives; nothing unpermitted has ever
        been written. But the refusal arrived out of save_model as a bare
        403: no admin chrome, no link back, no field error, and every value
        the person had typed discarded. This form answers any other
        validation failure with the page and the values intact, so the
        refusal an advertised feature produces was the one it handled
        worst.

        Checked against a copy of the instance, so an object-level
        permission backend sees the row it would see at the write.
        """
        task_key = cleaned.get("task_key")
        if not task_key:
            return
        candidate = copy(self.instance)
        candidate.task_key = task_key
        try:
            stored.check_permission(candidate, self.user)
        except PermissionDenied as exc:
            self.add_error("task_key", str(exc))

    def clean(self) -> dict[str, Any]:
        cleaned = cast("dict[str, Any]", super().clean())
        self._check_the_registry_permission(cleaned)
        end_time = cleaned.get("end_time")
        if end_time is not None and end_time <= self._effective_start_time(cleaned):
            # As a field error rather than an exception out of the save. The
            # same rule runs again in validate_schedule, which is the
            # authority for every write path; this is what puts it in front
            # of the person filling the form in.
            self.add_error(
                "end_time",
                "The end time must be after the start time, which this "
                "submission sets to now.",
            )
        return cleaned

    def full_clean(self) -> None:
        """
        Add what django_ox.stored refused when this submission was saved.

        After the form's own validation, and so after the model's, which is
        what finds a name already taken. That validation runs again on the
        pass that draws the form, a moment later than the one the write
        refused, and may now find the same fault itself: an end the clock
        has passed, a name another session has since taken. A field it
        faults is left with what it says, which is the form's own wording
        for it. Every other refusal is added, so the form is never drawn as
        valid after a save that was refused.
        """
        super().full_clean()
        if not self.is_bound:
            return
        for field, errors in (self.refused or {}).items():
            if field is None or field not in self.errors:
                self.add_error(field, errors)


@admin.register(OxSchedule)
class OxScheduleAdmin(_ScheduleAdmin):
    form = OxScheduleForm
    # Every column is a method rather than a field name, so that each value
    # is shown by code that knows a stored value may not read, and a time
    # the active zone cannot show is shown in UTC: Django's own rendering of
    # a field raises on either and takes the page with it.
    list_display = (
        "schedule_name",
        "task",
        "timing",
        "is_enabled",
        "starts",
        # Here because "Run selected schedules once now" ignores it, so an
        # operator choosing rows has to be able to see which of them have
        # already ended.
        "ends",
        "last_tick",
    )
    list_filter = ("enabled", "trigger")
    search_fields = ("name", "task_key")
    ordering = ("name",)
    # "Delete selected" first, where Django's own stands, which the one
    # below replaces on these pages.
    actions = (
        "delete_selected",
        "enable_selected",
        "disable_selected",
        "run_once_now",
    )
    # Methods for the same reason as the columns.
    readonly_fields = ("starts", "created", "updated")
    unreadable_template = "admin/django_ox/oxschedule/unreadable.html"

    #: Said on the pages where a person is about to write a row that
    #: nothing would dispatch. The condition cannot be a system check: a
    #: check runs without importing application code, so it can see
    #: SCHEDULABLE_TASKS in settings but never an @schedulable decorator,
    #: and it would need a database read to know whether any row exists.
    #: Here the registry is populated and the person is standing in front
    #: of the mistake.
    NO_SOURCE_WARNING = (
        "No backend sets OPTIONS['SCHEDULE_SOURCE'] to "
        "django_ox.stored.DatabaseScheduleSource, so schedules saved here "
        "are stored and never dispatched."
    )

    def _warn_if_nothing_dispatches(self, request: HttpRequest) -> None:
        # On the rendered page only. An action POST is handled by
        # changelist_view and then redirects to it, so warning on both
        # would say the same thing twice on the page that follows.
        if request.method == "GET" and self._stored_backend() is None:
            self.message_user(request, self.NO_SOURCE_WARNING, messages.WARNING)

    def changelist_view(
        self, request: HttpRequest, extra_context: dict[str, Any] | None = None
    ) -> Any:
        self._warn_if_nothing_dispatches(request)
        return super().changelist_view(request, extra_context)

    def add_view(
        self,
        request: HttpRequest,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> Any:
        self._warn_if_nothing_dispatches(request)
        return super().add_view(request, form_url, extra_context)

    def get_queryset(self, request: HttpRequest) -> QuerySet[OxSchedule]:
        """
        The alias the schedules are written to, for every page here.

        The same pin `OxTaskAdmin` makes, and here a write depends on it.
        The change form is built from this queryset and submits every field,
        so a form rendered from a replica writes the replica's values back
        over the primary. `stored.update_schedule` takes the row lock and
        writes only the submitted fields to stop a caller holding a stale
        copy doing exactly that, and a stale form walks straight through it.
        Nothing raises and nothing is logged; the newer values are gone.

        Django's own `ModelAdmin.get_object` reads this queryset too. So does
        the count `delete_selected` takes before it deletes. Unpinned, the
        redirect after an add lands on "doesn't exist" for a row that was
        just written, and the delete action removes nothing and says nothing.

        It carries nothing but the rows. The changelist reads the tick log
        for its own page, and every other page that starts here, the change
        form and both deletions among them, has no use for it: reading a
        schedule's ticks to find the schedule would make a tick that does
        not read stop the schedule from being opened, paused or deleted.
        django_ox.E008 refuses a router that writes ticks anywhere but the
        schedules' database, so the changelist reads them on this alias.
        """
        return super().get_queryset(request).using(stored.schedule_db_alias())

    def get_changelist(self, request: HttpRequest, **kwargs: Any) -> type[ChangeList]:
        return _ScheduleChangeList

    def get_readonly_fields(
        self, request: HttpRequest, obj: OxSchedule | None = None
    ) -> tuple[str, ...]:
        """
        The read-only values, and with them whichever of the start and the
        two record times a layout names by its field.

        This form shows those three through `starts`, `created` and
        `updated`. A project's subclass can lay the form out itself, in
        `fields` or `fieldsets`, and name them there as the model does.
        Each is an editable field of the model, so one named in a layout
        and not read-only would be drawn as an input, required on the add
        form, and handed to django_ox.stored on a save, which writes none
        of them and refuses the whole submission. Named there, it is drawn
        read-only instead, by Django's own rendering of the field.

        Only for a layout the class declares. One built in `get_fieldsets`
        is not looked at: asking for it would ask for these fields again.
        """
        declared = tuple(super().get_readonly_fields(request, obj))
        layout = self.fieldsets or [(None, {"fields": self.fields or ()})]
        named = flatten_fieldsets(layout)
        added = tuple(
            name for name in _NEVER_INPUTS if name in named and name not in declared
        )
        return (*declared, *added)

    def get_object(
        self, request: HttpRequest, object_id: str, from_field: str | None = None
    ) -> OxSchedule | None:
        """
        The schedule a change, delete or history page is for, read so that
        a stored value that does not read is told apart from a row that is
        not there.

        Django's own lookup raises on such a value, or, where the converter
        raises ValueError, catches it and answers that the row does not
        exist, which sends the person away from a row that does. Here the
        row is found through this admin's queryset, by its key alone, and
        read by django_ox._stored_read. A row that does not read raises
        _Unreadable, which each of those pages answers with the recovery
        page; a key with no row is None, and Django's own "does not exist".

        A page asked for by another field of the row (`_to_field`, which a
        link from a related object's form and a popup carry, and which Django
        always allows for the primary key) is found the same way: the row's
        key is looked up by that field, and nothing else of the row is read
        to find it.

        One kind of row that does not read is still returned: one whose
        every such value the change form can show as it is stored and
        replaces when it is saved (`_shown_in_the_form`). Its pages are the
        ordinary ones, and saving its form is how it is repaired. Only for
        someone who may save it. For anyone else the form is a page of
        values to read, each drawn by Django's own rendering of its field,
        which raises on an `enabled` that is neither true nor false.
        """
        refused = _refused_save(request)
        if refused is not None:
            # The pass that draws the form again after a refused save: on the
            # row as the first pass read it, which may no longer read.
            return refused[0]
        try:
            if from_field is None:
                pk = self.opts.pk.to_python(object_id)
            else:
                # As Django looks a row up by another field, for its key alone.
                field = cast("Any", self.opts.get_field(from_field))
                named = {field.name: field.to_python(object_id)}
                rows = self.get_queryset(request).filter(**named)
                pk = rows.values_list("pk", flat=True).get()
        except (OxSchedule.DoesNotExist, ValidationError, ValueError):
            return None
        alias = stored.schedule_db_alias()
        found = read_schedules(
            self.get_queryset(request).filter(pk=pk), using=alias
        ).get(pk)
        if isinstance(found, UnreadableRow):
            _report_unreadable_row(found)
            shown = _shown_in_the_form(found)
            if shown is None or not self.has_change_permission(request, shown):
                raise _Unreadable(found)
            return shown
        return found

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> Any:
        """
        Django's add and change views, with a save that django_ox.stored
        refuses answered by the form.

        The form validates a submission before the write, and the write
        validates it again under the row's lock, against the row as it then
        stands: a name taken in between, a boundary the write moves to a
        later moment than the form reckoned with, a stored value that stopped
        reading. Left to rise out of save_model, such a refusal is a server
        error, and everything typed is lost. Caught there and passed over,
        it is a save reported as made that was not.

        So save_model raises it as _SaveRefused, out of the transaction
        Django runs a submitted form in, which rolls back and takes whatever
        the request had written with it. Django's view then runs once more
        on the same submission with the refusal bound to the form, where it
        is one more validation error: the form is drawn again with it,
        nothing is saved and nothing is logged. A row deleted in between is
        left to that second pass too, which finds no row and says so. Where
        it still finds one, and its write again does not, Django's "does not
        exist" is answered here.
        """
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except _SaveRefused as refused:
            # What the second pass needs of it, and not the exception, which
            # would keep the first pass's frames with the request.
            setattr(request, _REFUSED_SAVE, (refused.as_read, refused.refusals))
        except _RowGone:
            pass
        try:
            return super().changeform_view(request, object_id, form_url, extra_context)
        except _RowGone:
            # The second pass found the row its write then did not. It read
            # it in a transaction that began before the row was deleted and
            # still shows it, which is what a request's one transaction does
            # at an isolation level that reads a snapshot. The write's read is
            # the one that locks and is current, and it has said twice that
            # the row is gone, so the person is told what Django tells them.
            gone = cast("Any", self)._get_obj_does_not_exist_redirect
            return gone(request, self.opts, str(object_id))

    def change_view(
        self,
        request: HttpRequest,
        object_id: str,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            response = super().change_view(request, object_id, form_url, extra_context)
        except _Unreadable as exc:
            return self._unreadable_view(request, exc.row)
        self._warn_if_a_value_does_not_read(request, response)
        self._warn_if_the_ticks_do_not_read(request, response)
        return response

    def delete_view(
        self,
        request: HttpRequest,
        object_id: str,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            return super().delete_view(request, object_id, extra_context)
        except _Unreadable as exc:
            return self._delete_unreadable(request, exc.row, extra_context)

    def history_view(
        self,
        request: HttpRequest,
        object_id: str,
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        try:
            return super().history_view(request, object_id, extra_context)
        except _Unreadable as exc:
            return self._unreadable_view(request, exc.row)

    @staticmethod
    def _schedule_on_the_form(
        request: HttpRequest, response: HttpResponse
    ) -> OxSchedule | None:
        """The schedule a change form was drawn for, on the request that shows it."""
        if request.method != "GET" or not isinstance(response, TemplateResponse):
            return None
        schedule = (response.context_data or {}).get("original")
        return schedule if isinstance(schedule, OxSchedule) else None

    def _warn_if_a_value_does_not_read(
        self, request: HttpRequest, response: HttpResponse
    ) -> None:
        """
        Say on the change form of a schedule that holds a value that does
        not read which value it is, and what saving the form does to it.

        The form is offered for such a row only where saving it replaces the
        value (`_shown_in_the_form`), so the save is the repair. It also
        starts the schedule afresh from that moment, as a retime does, since
        what the row held before is not known.
        """
        schedule = self._schedule_on_the_form(request, response)
        row = getattr(schedule, "ox_unreadable", None)
        if row is not None:
            self.message_user(
                request,
                _SAVING_REPLACES.format(reason=stored._printable(row.reason)),
                messages.WARNING,
            )

    def _warn_if_the_ticks_do_not_read(
        self, request: HttpRequest, response: HttpResponse
    ) -> None:
        """
        Say on a readable schedule's change form that its tick history does
        not read.

        The schedule itself can be edited, paused and deleted as ever. What
        the person cannot see from the form is that a worker reads the
        newest tick before it dispatches, and a tick that does not read
        holds the schedule back however it is edited.
        """
        schedule = self._schedule_on_the_form(request, response)
        if schedule is None:
            return
        alias = stored.schedule_db_alias()
        tick = _newest_ticks([schedule.pk], alias)[schedule.pk]
        if tick.unreadable:
            self.message_user(
                request,
                _TICKS_UNREADABLE_WARNING.format(key=tick.key),
                messages.WARNING,
            )

    def _unreadable_view(
        self, request: HttpRequest, row: UnreadableRow
    ) -> HttpResponse:
        """
        The page for a stored schedule that does not read, in place of its
        change form and its history.

        It says which fields did not read and why, and how to repair the
        row through django_ox.stored, which can replace a value it cannot
        read. The form is not offered: it is drawn from the row's values and
        submits every one of them back. Disable and Delete are, to the
        people allowed to use them, because both act on the row by its key.

        How it is repaired depends on which fields those are. A field a
        caller may write is repaired by passing it a new value. The start
        time, the time of the last change and the digest of the timing are
        written afresh by any change, and take no value of their own. The
        time the row was created and the count of boundary writes are
        neither, so a row that does not read in one of those can be paused
        and deleted and not repaired.
        """
        instance = row.deferred_instance()
        # As Django's change view decides it: a submission needs the change
        # permission, a look at the page that or the view permission.
        allowed = (
            self.has_change_permission(request, instance)
            if request.method == "POST"
            else self.has_view_or_change_permission(request, instance)
        )
        if not allowed:
            raise PermissionDenied
        shown = _UnreadableSchedule(row)
        problems = list(row.unreadable.items()) or [(_UNKNOWN_FIELD, row.reason)]
        can_disable = "disable_selected" in self.get_actions(
            request
        ) and self.has_change_permission(request, instance)
        replaced = [name for name in row.fields if name in stored.WRITABLE_FIELDS]
        rewritten = [name for name in row.fields if name in stored._WRITTEN_HERE]
        stuck = [name for name in row.fields if name not in _REPLACED_BY_A_SAVE]
        # What to hand update_schedule: a value for each field it takes, or,
        # where a change of any kind is the repair, the one field a row
        # always has a value for.
        example = ", ".join(f"{name}=..." for name in replaced) or "name=..."
        context = {
            **self.admin_site.each_context(request),
            "title": _UNREADABLE_TITLE,
            "subtitle": str(shown),
            "opts": self.opts,
            "schedule": shown,
            "name": shown.value("name"),
            "task_key": shown.value("task_key"),
            "problems": problems,
            "replaced": replaced,
            "rewritten": rewritten,
            "stuck": stuck,
            "example": example if row.fields and not stuck else None,
            "can_disable": can_disable,
            "can_delete": self.has_delete_permission(request, instance),
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "preserved_filters": self.get_preserved_filters(request),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(request, self.unreadable_template, context)

    def _delete_unreadable(
        self,
        request: HttpRequest,
        row: UnreadableRow,
        extra_context: dict[str, Any] | None,
    ) -> HttpResponse:
        """
        Delete a stored schedule that does not read, by its key.

        Django's delete page, with the same permissions, the same check for
        related rows that would go with it or protect it, the same entry in
        the admin log and the same deletion through delete_model, which
        tells the workers. What differs is that nothing reads the row's
        values: it is named by its label, and deleted as an instance holding
        only the fields that read.
        """
        instance = row.deferred_instance()
        if not self.has_delete_permission(request, instance):
            raise PermissionDenied
        shown = _UnreadableSchedule(row)
        deleted, model_count, perms_needed, protected = self._deletion_summary(
            request, {instance: str(shown)}
        )
        if request.POST and not protected:
            if perms_needed:
                raise PermissionDenied
            with transaction.atomic(using=row.alias):
                self.log_deletions(request, cast("Any", [shown]))
                self.delete_model(request, instance)
            return self.response_delete(request, str(shown), row.pk)
        self.message_user(
            request,
            _DELETING_UNREADABLE.format(reason=stored._printable(row.reason)),
            messages.WARNING,
        )
        object_name = str(self.opts.verbose_name)
        # Django's own titles for this page, in Django's own translations.
        title = (
            gettext("Cannot delete %(name)s") % {"name": object_name}
            if perms_needed or protected
            else gettext("Delete")
        )
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "subtitle": None,
            "object_name": object_name,
            "object": shown,
            "escaped_object": display_for_value(
                str(shown), self.get_empty_value_display()
            ),
            "deleted_objects": deleted,
            "delete_confirmation_max_display": getattr(
                self, "delete_confirmation_max_display", None
            ),
            "model_count": dict(model_count).items(),
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": self.opts,
            "app_label": self.opts.app_label,
            "preserved_filters": self.get_preserved_filters(request),
            "is_popup": False,
            "to_field": None,
            **(extra_context or {}),
        }
        return self.render_delete_form(request, context)

    def _deletion_summary(
        self, request: HttpRequest, labels: dict[OxSchedule, str]
    ) -> tuple[list[Any], dict[str, int], set[str], list[Any]]:
        """
        What Django's get_deleted_objects reports for these rows, each named
        by its label in `labels`.

        Django names each object it lists by str(), which reads the row's
        name, and that may be the value that does not read. The rows that
        would be deleted with them, or that protect them, are collected
        exactly as Django collects them, and named as Django names them.
        Nothing here reads a schedule: each is collected by its key.
        """
        site = self.admin_site
        empty = self.get_empty_value_display()
        instances = list(labels)
        collector = NestedObjects(using=stored.schedule_db_alias(), origin=instances)
        collector.collect(instances)
        perms_needed: set[str] = set()

        def format_callback(obj: Model) -> Any:
            opts = obj._meta
            # A row collected with them, of whatever model, is named as
            # Django names it.
            shown = labels.get(cast("OxSchedule", obj)) or str(obj)
            no_edit_link = f"{capfirst(opts.verbose_name)}: {shown}"
            if not site.is_registered(type(obj)):
                return no_edit_link
            if not site.get_model_admin(type(obj)).has_delete_permission(request, obj):
                perms_needed.add(str(opts.verbose_name))
            try:
                url = reverse(
                    f"{site.name}:{opts.app_label}_{opts.model_name}_change",
                    None,
                    (quote(obj.pk),),
                )
            except NoReverseMatch:
                return no_edit_link
            return format_html(
                '{}: <a href="{}">{}</a>',
                capfirst(opts.verbose_name),
                url,
                display_for_value(shown, empty),
            )

        to_delete = collector.nested(format_callback)
        protected = [format_callback(obj) for obj in collector.protected]
        model_count = {
            str(cast("type[Model]", model)._meta.verbose_name_plural): len(objs)
            for model, objs in collector.model_objs.items()
        }
        return to_delete, model_count, perms_needed, protected

    def get_form(
        self,
        request: HttpRequest,
        obj: OxSchedule | None = None,
        change: bool = False,  # noqa: FBT001, FBT002 - ModelAdmin's own signature
        **kwargs: Any,
    ) -> Any:
        """
        Bind the requesting user onto the form class, and with it whatever
        django_ox.stored refused when this request's submission was saved.

        ModelAdmin hands the form class to the change view, which builds it
        with arguments of its own, so there is no other seam to pass either
        through. A subclass per request rather than a partial, because the
        admin also reads `base_fields` off what this returns.
        """
        form_class = super().get_form(request, obj, change=change, **kwargs)
        refused = _refused_save(request)
        bound = {
            "user": request.user,
            "refused": None if refused is None else refused[1],
        }
        return type(form_class.__name__, (form_class,), bound)

    @admin.display(description="Name", ordering="name")
    def schedule_name(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        return str(obj)

    @admin.display(description="Task key", ordering="task_key")
    def task(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        if isinstance(obj, _UnreadableSchedule):
            return obj.value("task_key") or _UNREADABLE_CELL
        return obj.task_key

    @admin.display(description="Timing")
    def timing(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        if isinstance(obj, _UnreadableSchedule):
            return _UNREADABLE_VALUE.format(
                fields=", ".join(obj.row.fields) or _UNKNOWN_FIELD
            )
        if obj.trigger == OxSchedule.Trigger.CRON:
            return obj.cron
        every = f"every {obj.every_seconds}s"
        return f"{every} +{obj.phase_seconds}s" if obj.phase_seconds else every

    @admin.display(description="Enabled", ordering="enabled")
    def is_enabled(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        # Django's icon for a boolean is looked up by value and raises on
        # anything but True, False and None, so only those reach it.
        if isinstance(obj, _UnreadableSchedule):
            if "enabled" in obj.row.unreadable:
                return _INVALID_ENABLED
            enabled = obj.value("enabled")
        else:
            enabled = obj.enabled
        if enabled is True or enabled is False:
            return str(
                display_for_value(enabled, self.get_empty_value_display(), boolean=True)
            )
        return _INVALID_ENABLED

    @admin.display(description="Start time", ordering="start_time")
    def starts(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        return self._time_cell(obj, "start_time")

    @admin.display(description="End time", ordering="end_time")
    def ends(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        return self._time_cell(obj, "end_time")

    @admin.display(description="Created at")
    def created(self, obj: OxSchedule) -> str:
        return self._time_cell(obj, "created_at")

    @admin.display(description="Updated at")
    def updated(self, obj: OxSchedule) -> str:
        return self._time_cell(obj, "updated_at")

    def _time_cell(self, obj: OxSchedule | _UnreadableSchedule, field: str) -> str:
        empty = self.get_empty_value_display()
        if isinstance(obj, _UnreadableSchedule):
            if field in obj.row.unreadable:
                return _UNREADABLE_CELL
            return _shown_time(obj.value(field), field, empty)
        return _shown_time(getattr(obj, field), field, empty)

    @admin.display(description="Last tick")
    def last_tick(self, obj: OxSchedule | _UnreadableSchedule) -> str:
        # The changelist reads the newest tick of every schedule on its page
        # in one statement and leaves it here. Anywhere else this reads the
        # one.
        tick = cast("TickRead | None", getattr(obj, "ox_last_tick", None))
        if tick is None:
            alias = stored.schedule_db_alias()
            tick = _newest_ticks([obj.pk], alias)[obj.pk]
        if tick.unreadable:
            return _UNREADABLE_TICKS
        if tick.at is None:
            return _NO_TICKS
        return f"{tick.at:%Y-%m-%d %H:%M}"

    def save_model(
        self,
        request: HttpRequest,
        obj: OxSchedule,
        form: Any,
        change: bool,  # noqa: FBT001 - ModelAdmin's own signature
    ) -> None:
        """
        Route through the service layer rather than calling save().

        The default implementation saves the instance directly, which would
        leave the activation boundary and the change marker untouched: a
        retimed schedule would keep a boundary set for its old timing, and
        no worker would learn the row had moved.

        What the service layer refuses is raised as _SaveRefused, which
        changeform_view answers with the form: see there for why it is
        raised and not handled here. Only for this module's form, which is
        the one that can carry the refusal; a project's own form class that
        is not one gets the service layer's exception as it was raised. A
        row deleted before the write is raised as _RowGone, whatever the
        form, and changeform_view answers that too.
        """
        if not isinstance(form, OxScheduleForm):
            self._write(request, obj, form, change=change)
            return
        try:
            self._write(request, obj, form, change=change)
        except ValidationError as exc:
            raise _SaveRefused(form.as_read, _refusals(form, exc)) from exc
        except StoredValueUnreadable as exc:
            # A value that does not read and that the submission did not
            # replace: every field of the form was submitted, so it is in
            # none of them, and is said of the form.
            message = _SAVE_LEFT_UNREADABLE.format(reason=stored._printable(exc.reason))
            refusal: dict[str | None, list[Any]] = {None: [ValidationError(message)]}
            raise _SaveRefused(form.as_read, refusal) from exc

    def _write(
        self, request: HttpRequest, obj: OxSchedule, form: Any, *, change: bool
    ) -> None:
        if change:
            # The values this request submitted, not every field off the
            # instance. A form is built from a row read at the start of the
            # request, so saving all of it would write back whatever else has
            # changed since, including a boundary someone else just moved.
            submitted = {
                field: form.cleaned_data[field]
                for field in form.fields
                if field in form.cleaned_data
            }
            try:
                stored.update_schedule(obj, user=request.user, **submitted)
            except OxSchedule.DoesNotExist as exc:
                # Deleted since the form read it: the same exception, of a
                # class changeform_view knows to answer.
                raise _RowGone(*exc.args) from exc
        else:
            created = stored.create_schedule(
                user=request.user,
                **{
                    field: getattr(obj, field)
                    for field in (
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
                    )
                },
            )
            # The service function builds and saves its own instance, and
            # the admin goes on using the one it holds. Without this its pk
            # stays None: "save and continue editing" redirects to a URL
            # containing None, and the log entry records the row's id as
            # the string "None", so its history is attached to nothing.
            obj.pk = created.pk
            # From the alias the row was written to, which is the one the
            # service function resolved and used. Unqualified this follows
            # db_for_read, and a replica that has not seen the row yet
            # raises DoesNotExist on a save that succeeded.
            obj.refresh_from_db(using=created._state.db)

    def has_change_permission(
        self, request: HttpRequest, obj: OxSchedule | None = None
    ) -> bool:
        """
        The model permission, plus the registry entry's own if it has one.

        Checked here because this is the hook Django gives an object to.
        The per-action hook does not take one, so an action's own check
        cannot see which task the selected rows name.
        """
        if not super().has_change_permission(request, obj):
            return False
        return self._registry_permitted(request, obj)

    def has_delete_permission(
        self, request: HttpRequest, obj: OxSchedule | None = None
    ) -> bool:
        """
        The registry entry's permission gates deletion too.

        Stopping a production schedule by deleting it is the same authority
        the permission exists to gate, so checking it only on change would
        leave the obvious way round.
        """
        if not super().has_delete_permission(request, obj):
            return False
        return self._registry_permitted(request, obj)

    def _registry_permitted(self, request: HttpRequest, obj: OxSchedule | None) -> bool:
        """
        Whether the registry lets this person change or delete this row.

        The question django_ox.stored puts to the same row at the write,
        put here by the same function, so that a page and the write behind
        it cannot answer differently: the permission of the entry the row
        names, and for a row whose task key did not read, which names no
        entry, a superuser alone. No rule of its own is kept here.
        """
        if obj is None:
            return True
        # A stored row whose task key did not read stands in with only the
        # fields that did, and its task key is one of the rest.
        unread = {"task_key"} & obj.get_deferred_fields()
        # A backend that decides by the row is handed the row in full, from
        # the write alias, as the write is: the instance here may hold only
        # some of it, and reading the rest would follow the routing of reads.
        whole = (
            partial(stored._whole_row, obj.pk, stored.schedule_db_alias(), locked=False)
            if obj.get_deferred_fields()
            else (lambda: obj)
        )
        try:
            stored._may_change(obj, unread, request.user, whole)
        except PermissionDenied:
            return False
        return True

    def delete_model(self, request: HttpRequest, obj: OxSchedule) -> None:
        """
        Delete through the service layer, for the person asking.

        has_delete_permission decides who is shown the page. The deletion
        itself is django_ox.stored's, which asks the registry entry's
        permission of the row as the database holds it under its lock, and
        tells the workers.
        """
        stored.delete_schedule(obj, user=request.user)

    def delete_queryset(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        """
        Delete these schedules through the service layer, each by its key,
        for the person asking.

        A queryset's own delete() reads every row it is about to delete
        whenever the project has a signal receiver or a relation that needs
        the rows, and one row holding a value that does not read ends that
        read for all of them. Here each row is read by
        django_ox._stored_read and handed over as an instance: the row
        where it reads, and otherwise the fields that did. django_ox.stored
        then deletes the selection together. It locks the rows by one read,
        asks the registry entry's permission of each as the database holds
        it under its lock, all of them before any is deleted, deletes them
        as Django deletes instances, with whatever would go with them and
        the same signals for each, and tells the workers once. What that
        costs does not grow with the selection.

        All of the rows or none: a row the person may not delete keeps the
        whole selection, and the workers are told of nothing.
        """
        alias = stored.schedule_db_alias()
        rows = read_schedules(queryset.using(alias), using=alias)
        stored._delete_schedules(
            [_instance(row) for row in rows.values()],
            user=request.user,
            origin=queryset,
        )

    def _selected(
        self, queryset: QuerySet[OxSchedule]
    ) -> tuple[str, dict[Any, OxSchedule | UnreadableRow]]:
        """
        The rows an action was handed, by key, each as it reads, and the
        alias they were read from.

        Their keys are read from the queryset, which is how Django chose
        them, and nothing else is: iterating it would read every selected
        row in one result, and one value in one row that does not read
        would end the action for all of them. Each row is then read by
        django_ox._stored_read, and one that does not read is reported as
        the changelist reports it.

        From the alias the schedules are written to, the way delete_queryset
        takes them. An action writes what it reads here, or enqueues from it,
        so on a replica that is behind it would act on rows as they were.
        """
        alias = stored.schedule_db_alias()
        rows = read_schedules(queryset.using(alias), using=alias)
        for row in rows.values():
            if isinstance(row, UnreadableRow):
                _report_unreadable_row(row)
        return alias, rows

    def has_delete_selected_permission(self, request: HttpRequest) -> bool:
        """
        Who is offered "Delete selected": whoever Django's own is offered to.

        A site can switch Django's action off for every model, and the one
        below replaces it on these pages only, so it goes where that goes.
        """
        offered = any(name == "delete_selected" for name, _ in self.admin_site.actions)
        return offered and self.has_delete_permission(request)

    @admin.action(
        # Django's own name for it, in Django's own translations.
        description=gettext_lazy("Delete selected %(verbose_name_plural)s"),
        permissions=("delete_selected",),
    )
    def delete_selected(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> HttpResponse | None:
        """
        Django's "Delete selected", for a selection that may hold a row that
        does not read.

        Django's own reads the selected rows whole, as one queryset: to
        collect what would be deleted with them, to count them, to log them
        and to list them on the confirmation page. Here the rows are read by
        django_ox._stored_read, each named by a label that reads nothing
        more, and the rest is Django's: the same permissions, asked of each
        row, the same check for rows that would go with them or protect
        them, the same confirmation page, the same entries in the admin log,
        written before the deletion, and delete_queryset, which tells the
        workers.
        """
        alias, rows = self._selected(queryset)
        # What the page lists and the log names: the row where it reads, a
        # stand-in that knows only its label where it does not.
        shown = [
            _UnreadableSchedule(row) if isinstance(row, UnreadableRow) else row
            for row in rows.values()
        ]
        deletable, model_count, perms_needed, protected = self._deletion_summary(
            request,
            {
                _instance(row): str(named)
                for row, named in zip(rows.values(), shown, strict=True)
            },
        )
        if request.POST.get("post") and not protected:
            if perms_needed:
                raise PermissionDenied
            if shown:
                with transaction.atomic(using=alias):
                    self.log_deletions(request, cast("Any", shown))
                    self.delete_queryset(request, queryset)
                self.message_user(
                    request,
                    gettext("Successfully deleted %(count)d %(items)s.")
                    % {
                        "count": len(shown),
                        "items": model_ngettext(self.opts, len(shown)),
                    },
                    messages.SUCCESS,
                )
            return None
        unreadable = [row for row in rows.values() if isinstance(row, UnreadableRow)]
        if unreadable:
            self.message_user(
                request,
                _DELETING_UNREADABLE_ROWS.format(
                    count=len(unreadable), rows=_named([_label(r) for r in unreadable])
                ),
                messages.WARNING,
            )
        objects_name = model_ngettext(self.opts, len(shown))
        title = (
            gettext("Cannot delete %(name)s") % {"name": objects_name}
            if perms_needed or protected
            else gettext("Delete multiple objects")
        )
        context = {
            **self.admin_site.each_context(request),
            "title": title,
            "subtitle": None,
            "objects_name": str(objects_name),
            "deletable_objects": [deletable],
            "delete_confirmation_max_display": getattr(
                self, "delete_confirmation_max_display", None
            ),
            "model_count": dict(model_count).items(),
            "queryset": shown,
            "perms_lacking": perms_needed,
            "protected": protected,
            "opts": self.opts,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "media": self.media,
        }
        request.current_app = self.admin_site.name
        # Django's own page, looked for where Django looks for it.
        page = "delete_selected_confirmation.html"
        app, model = self.opts.app_label, self.opts.model_name
        return TemplateResponse(
            request,
            self.delete_selected_confirmation_template
            or [f"admin/{app}/{model}/{page}", f"admin/{app}/{page}", f"admin/{page}"],
            context,
        )

    def _pause(self, request: HttpRequest, alias: str, row: UnreadableRow) -> None:
        """
        Pause a stored schedule that does not read, and enter it in the
        admin's log.

        By its key, which is how django_ox.stored pauses a row it cannot
        read. The entry is the one the change form leaves when a field is
        changed, under the row's label, so that a row paused this way has
        the same record a row edited in the form has.
        """
        enabled = OxSchedule._meta.get_field("enabled")
        changed = [{"changed": {"fields": [capfirst(str(enabled.verbose_name))]}}]
        with transaction.atomic(using=alias):
            stored.update_schedule(
                OxSchedule(pk=row.pk), user=request.user, enabled=False
            )
            self.log_change(request, cast("Any", _UnreadableSchedule(row)), changed)

    def _set_enabled(
        self,
        request: HttpRequest,
        queryset: QuerySet[OxSchedule],
        *,
        enabled: bool,
    ) -> None:
        """
        Enable or disable each selected schedule on its own.

        Disabling is `enabled=False` alone, which update_schedule writes
        without validating the rest of the row and without reading a value
        in it that does not read: it is how an operator stops a schedule
        that no longer validates, or one that cannot be read at all.

        Enabling validates the row in full. A row that does not validate,
        and one holding a value that does not read, are left as they are,
        counted and named, and the rest of the selection is still enabled.
        A row that does not read is refused here and not handed to
        update_schedule, which would take `enabled=True` for a new value of
        an `enabled` that did not read, or for the change that writes an
        unreadable start time afresh, and repair the row. That is a repair
        nobody asked for by choosing "Enable".
        """
        alias, rows = self._selected(queryset)
        changed: list[Any] = []
        invalid: list[str] = []
        unreadable: list[str] = []
        refused = gone = 0
        for pk, row in rows.items():
            if not self.has_change_permission(request, _instance(row)):
                refused += 1
                continue
            if enabled and isinstance(row, UnreadableRow):
                unreadable.append(_label(row))
                continue
            try:
                if isinstance(row, UnreadableRow):
                    self._pause(request, alias, row)
                else:
                    stored.update_schedule(
                        OxSchedule(pk=pk), user=request.user, enabled=enabled
                    )
            except ValidationError:
                invalid.append(_label(row))
            except StoredValueUnreadable:
                # It read a moment ago, and held a value that does not by the
                # time it was locked. Read once more, for the report.
                unreadable.append(_label(row))
                again = read_schedules([pk], using=alias).get(pk)
                if isinstance(again, UnreadableRow):
                    _report_unreadable_row(again)
            except PermissionDenied:
                refused += 1
            except OxSchedule.DoesNotExist:
                gone += 1
            else:
                changed.append(pk)
        verb = "Enabled" if enabled else "Disabled"
        self.message_user(
            request, f"{verb} {len(changed)} schedule(s).", messages.SUCCESS
        )
        if invalid:
            self.message_user(
                request,
                _COULD_NOT_ENABLE.format(count=len(invalid), rows=_named(invalid)),
                messages.WARNING,
            )
        if unreadable:
            self.message_user(
                request,
                _UNREADABLE_NOT_ENABLED.format(
                    count=len(unreadable), rows=_named(unreadable)
                ),
                messages.WARNING,
            )
        self._say_what_was_passed_over(request, refused, gone)
        if enabled:
            self._warn_of_unreadable_ticks(request, alias, changed)

    def _say_what_was_passed_over(
        self, request: HttpRequest, refused: int, gone: int
    ) -> None:
        """Report the selected rows an action left alone for no fault of theirs."""
        if refused:
            self.message_user(
                request, _NO_PERMISSION.format(count=refused), messages.WARNING
            )
        if gone:
            self.message_user(
                request, _NO_LONGER_THERE.format(count=gone), messages.WARNING
            )

    def _warn_of_unreadable_ticks(
        self, request: HttpRequest, alias: str, pks: list[Any]
    ) -> None:
        """
        Say which of the schedules just enabled or run have tick history
        that does not read.

        Neither action reads the tick log, so a tick that does not read
        stops neither. A worker does read it before it dispatches, and holds
        such a schedule back, so the person who has just enabled or run one
        is told that it may still not run on its own, and what removes the
        tick.
        """
        if not pks:
            return
        held_back = [
            tick.key for tick in _newest_ticks(pks, alias).values() if tick.unreadable
        ]
        if held_back:
            keys = _named([f"--schedule-key {key}" for key in held_back], " ")
            self.message_user(
                request,
                _TICKS_UNREADABLE_AFTER.format(count=len(held_back), keys=keys),
                messages.WARNING,
            )

    @admin.action(description="Enable selected schedules", permissions=("change",))
    def enable_selected(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        self._set_enabled(request, queryset, enabled=True)

    @admin.action(description="Disable selected schedules", permissions=("change",))
    def disable_selected(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        self._set_enabled(request, queryset, enabled=False)

    @admin.action(
        description="Run selected schedules once now", permissions=("change",)
    )
    def run_once_now(
        self, request: HttpRequest, queryset: QuerySet[OxSchedule]
    ) -> None:
        """
        Enqueue each selected schedule's task immediately.

        No tick row is written, because this is not a tick: the schedule's
        own ticks are unaffected and the next one still fires. A manual run
        does not consume a scheduled one.

        Built through the same path a dispatched tick takes, so a manual run
        carries the same arguments and goes to the same backend. Enqueueing
        the task directly passed the row's raw values where a tick passes
        the form's cleaned ones, and skipped the backend binding, so the
        two could differ in both what they carried and where they landed.

        A disabled schedule and one past its end time both run: this is the
        one way to run a paused schedule, and a run is not a tick, so
        neither bound applies to it. Both are reported, because the
        changelist shows paused and running rows together, an admin action
        has no confirmation step, and a success count alone leaves an
        operator who mis-selected a paused schedule with nothing to read.

        A row holding a value that does not read is not run. What it would
        run with is not known: on SQLite a start that is not a date reads as
        no start and an `enabled` of 2 as false, and a task built from
        either is not the one the row describes. It is counted and named
        apart from a row that reads and cannot be built, since what puts
        each right is different.
        """
        alias, options, source_class = self._stored_backend() or (
            DEFAULT_TASK_BACKEND_ALIAS,
            {},
            stored.DatabaseScheduleSource,
        )
        source = source_class(options, alias)
        if not callable(getattr(source, "_to_schedule", None)):
            # A source is only required to answer schedules(). One that
            # reaches the rows some other way has nothing for a manual run
            # to build a single row with, so the shipped reading is used
            # for that and the project's own alias is still the one the
            # task is enqueued on.
            source = stored.DatabaseScheduleSource(options, alias)
        now = timezone.now()
        ran: list[Any] = []
        skipped: list[str] = []
        unreadable: list[str] = []
        overridden = refused = 0
        # A manual run enqueues from the row's own values rather than
        # re-reading them, so the alias `_selected` reads is the alias the
        # run carries: read anywhere else, a schedule edited a moment ago
        # would run with the arguments a lagging replica still holds.
        schedules_alias, rows = self._selected(queryset)
        for pk, row in rows.items():
            if not self.has_change_permission(request, _instance(row)):
                # Counted separately from `skipped`, which reports a row
                # that cannot run as written: telling an operator their
                # payroll schedule is broken when they are simply not
                # allowed to run it sends them to fix the wrong thing.
                refused += 1
                continue
            if isinstance(row, UnreadableRow):
                unreadable.append(_label(row))
                continue
            try:
                built = source._to_schedule(row)
            except Exception:
                skipped.append(_label(row))
                continue
            built.task.enqueue(*built.args, **built.kwargs)
            ran.append(pk)
            # Counted after the enqueue, so a row that could not be built is
            # reported as skipped rather than as a run that overrode a bound.
            if not row.enabled or (row.end_time is not None and row.end_time < now):
                overridden += 1
        self.message_user(request, f"Enqueued {len(ran)} task(s).", messages.SUCCESS)
        if skipped:
            self.message_user(
                request,
                _CANNOT_RUN.format(count=len(skipped), rows=_named(skipped)),
                messages.WARNING,
            )
        if unreadable:
            self.message_user(
                request,
                _UNREADABLE_NOT_RUN.format(
                    count=len(unreadable), rows=_named(unreadable)
                ),
                messages.WARNING,
            )
        self._say_what_was_passed_over(request, refused, 0)
        if overridden:
            self.message_user(
                request,
                f"Ran {overridden} schedule(s) that were disabled or past "
                "their end time. A manual run ignores both.",
                messages.WARNING,
            )
        self._warn_of_unreadable_ticks(request, schedules_alias, ran)

    @staticmethod
    def _stored_backend() -> tuple[str, dict[str, Any], type[Any]] | None:
        """
        The backend whose workers dispatch the stored schedules, its
        options, and the class it reads them with, or None when no backend
        names one.

        Read from settings rather than from the instantiated backends, the
        same way the system checks read it: a check must not construct
        every backend in a project to answer a question about a dictionary.

        The test applied here is the loader's own: build the class the
        path names and ask whether it answers `schedules()`. Nothing else
        decides what the worker dispatches from, so nothing else may
        decide what this page says about it. Matched by name, and then by
        `issubclass` against the shipped source, a project whose source
        is written by composition rather than by inheritance -- which the
        loader documents as supported -- was told its schedules are
        stored and never dispatched while its worker was dispatching
        them, and its manual run went to the default alias.

        The cost of taking the loader's answer is that a source which
        answers `schedules()` without reading the rows counts too. That
        is the same class of source the worker would run, and the admin
        has nothing else to read it by; a false warning on a working
        deployment is the worse of the two.

        The class is returned with the options because a subclass that
        changes how a row becomes a schedule has to be the one the manual
        run builds with, or the admin runs something the project did not
        define.
        """
        for alias, config in settings.TASKS.items():
            options = config.get("OPTIONS") if isinstance(config, dict) else None
            if not isinstance(options, dict):
                continue
            path = options.get("SCHEDULE_SOURCE")
            if not isinstance(path, str):
                continue
            try:
                source_class = import_string(path)
            except ImportError:
                # The worker refuses this configuration outright. Here it
                # is one backend of several and the page still has to load,
                # so it is simply not the one.
                continue
            if not isinstance(source_class, type):
                continue
            try:
                source = source_class(options, str(alias))
            except Exception:  # noqa: S112 - the worker reports it, not this page
                # Same reasoning as the failed import: a source that cannot
                # be built is refused at worker startup and by the system
                # check, and the admin still has to render.
                continue
            if callable(getattr(source, "schedules", None)):
                return str(alias), options, source_class
        return None
