"""
An admin site beside the default one, with a project's own subclass of
OxScheduleAdmin whose queryset carries annotations and a scope.

`shout` and `rank` are computed from the name. `rank` is also the ordering,
so the annotation decides which rows are on a page. Schedules whose name
starts with `hidden` are outside the scope this admin is allowed to see.
"""

from django.contrib import admin
from django.db.models import Q
from django.db.models.functions import Length, Upper
from django.urls import path

from django_ox.admin import OxScheduleAdmin
from django_ox.models import OxSchedule


class Annotated(OxScheduleAdmin):
    list_per_page = 2

    def get_queryset(self, request):
        # Django orders the queryset before it is annotated, so the
        # annotation cannot be named in it yet.
        request.annotating = True
        try:
            base = super().get_queryset(request)
        finally:
            request.annotating = False
        return base.filter(~Q(name__startswith="hidden")).annotate(
            shout=Upper("name"), rank=Length("name")
        )

    def get_ordering(self, request):
        if getattr(request, "annotating", False):
            return ()
        return ("-rank", "name")


annotated_site = admin.AdminSite(name="annotated")
annotated_site.register(OxSchedule, Annotated)

urlpatterns = [
    path("admin/", admin.site.urls),
    path("annotated/", annotated_site.urls),
]
