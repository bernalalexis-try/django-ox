"""
Two extra admin sites beside the default one, each with a project's own
subclass of OxScheduleAdmin that lays the schedule form out itself: `sections`
in `fieldsets`, `listed` in `fields`. Both name the start time and the two
record times by their model fields, as the model does.
"""

from django.contrib import admin
from django.urls import path

from django_ox.admin import OxScheduleAdmin
from django_ox.models import OxSchedule


class InSections(OxScheduleAdmin):
    fieldsets = (
        (None, {"fields": ("name", "task_key", "enabled")}),
        (
            "Timing",
            {
                "fields": (
                    "trigger",
                    "cron",
                    ("every_seconds", "phase_seconds"),
                    ("start_time", "end_time"),
                    "starting_deadline_seconds",
                )
            },
        ),
        ("Arguments", {"fields": ("arguments",)}),
        ("Record", {"fields": ("created_at", "updated_at")}),
    )


class Listed(OxScheduleAdmin):
    fields = (
        "name",
        "task_key",
        "trigger",
        "cron",
        ("every_seconds", "phase_seconds"),
        "arguments",
        "enabled",
        ("start_time", "end_time"),
        "starting_deadline_seconds",
        ("created_at", "updated_at"),
    )


sections_site = admin.AdminSite(name="sections")
sections_site.register(OxSchedule, InSections)

listed_site = admin.AdminSite(name="listed")
listed_site.register(OxSchedule, Listed)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("sections/", sections_site.urls),
    path("listed/", listed_site.urls),
]
