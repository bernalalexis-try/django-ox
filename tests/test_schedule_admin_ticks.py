"""
What the schedule admin's Last tick column costs.

The changelist shows each listed schedule's newest tick, and a deployment
that has run for a year has a tick log of millions of rows behind a page of
a hundred schedules. The column is worth showing only if the page does not
read that history to fill it: each schedule's newest tick has to be the end
of its own entries in the index on (schedule_name, scheduled_for), reached
directly, in one statement for the page.

So the statement the page sends is taken as it was sent and the database is
asked how it will run it, on a tick log large enough for the answer to
matter. The same read serves the change form's warning and the warnings
after "Enable" and "Run once", and is held to the same shape there.
"""

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

import django_ox.admin
from django_ox.models import OxScheduleTick

from .test_schedule_admin_unreadable import (
    CHANGE,
    CHANGELIST,
    SCHEDULES,
    TICKS,
    _registry,  # noqa: F401 (a fixture)
    a_schedule,
    a_tick,
    body,
    by_sql,
    clock,  # noqa: F401 (a fixture)
)

#: Ticks a schedule in the history below: enough that reading a schedule's
#: every tick and seeking to its newest are different plans.
PER_SCHEDULE = 2000

FIRST_TICK = "2026-01-01 00:00:00"


@pytest.fixture
def admin_user(db):
    return User.objects.create_superuser("root", "root@example.com", "pw")


def a_history(schedules):
    """
    PER_SCHEDULE ticks for each of these schedules, a minute apart, written
    by SQL in one statement. PostgreSQL's planner is then told what the
    table holds, inside this test's transaction; MySQL and SQLite choose
    this plan by the statement's shape.
    """
    columns = "(schedule_name, scheduled_for, task_id, created_at)"
    if connection.vendor == "postgresql":
        keys = ", ".join(f"('db:{row.pk}')" for row in schedules)
        by_sql(
            f"INSERT INTO {TICKS} {columns} "  # noqa: S608
            f"SELECT k.name, TIMESTAMPTZ '{FIRST_TICK}+00' + n * INTERVAL '1 minute', "
            f"NULL, now() FROM (VALUES {keys}) AS k(name), "
            f"generate_series(1, {PER_SCHEDULE}) AS n"
        )
        by_sql(f"ANALYZE {TICKS}")
        return
    names = " UNION ALL ".join(f"SELECT 'db:{row.pk}' AS name" for row in schedules)
    # The numbers 1 to PER_SCHEDULE, as a product of the ten digits.
    digit = " UNION ALL ".join(f"SELECT {d} AS d" for d in range(10))
    places = len(str(PER_SCHEDULE - 1))
    number = " + ".join(f"p{i}.d * {10**i}" for i in range(places)) + " + 1"
    digits = ", ".join(f"({digit}) AS p{i}" for i in range(places))
    at = (
        f"TIMESTAMPADD(MINUTE, {number}, '{FIRST_TICK}')"
        if connection.vendor == "mysql"
        else f"datetime('{FIRST_TICK}', '+' || ({number}) || ' minutes')"
    )
    created = "NOW(6)" if connection.vendor == "mysql" else f"'{FIRST_TICK}'"
    by_sql(
        f"INSERT INTO {TICKS} {columns} "  # noqa: S608
        f"SELECT k.name, {at}, NULL, {created} FROM ({names}) AS k, {digits} "
        f"WHERE {number} <= {PER_SCHEDULE}"
    )


def newest_as_shown(schedule):
    """The schedule's newest tick as the column shows one, by Django's own read."""
    newest = (
        OxScheduleTick.objects.filter(schedule_name=f"db:{schedule.pk}")
        .order_by("-scheduled_for")
        .values_list("scheduled_for", flat=True)
        .first()
    )
    return f"{newest:%Y-%m-%d %H:%M}"


def tick_statements(queries):
    return [
        q["sql"]
        for q in queries.captured_queries
        if q["sql"].lstrip().upper().startswith("SELECT") and TICKS in q["sql"]
    ]


def plan_of(statement):
    """How the database says it will run a statement, as sent: its plan's rows."""
    explain = {
        "postgresql": "EXPLAIN ",
        "mysql": "EXPLAIN FORMAT=TREE ",
        "sqlite": "EXPLAIN QUERY PLAN ",
    }[connection.vendor]
    with connection.cursor() as cursor:
        cursor.execute(explain + statement)
        return cursor.fetchall()


def assert_seeks_each_newest_tick(statement):
    """
    The statement reaches each schedule's newest tick from the end of that
    schedule's entries in the index, and reads no more of the tick log.

    Only what the plan does with the tick log is looked at. What it does
    with the schedules themselves, sorting a page of them by name for one,
    is another matter.
    """
    # Not a maximum taken over each schedule's history.
    assert "GROUP BY" not in statement.upper(), statement
    assert "MAX(" not in statement.upper(), statement
    # Each of the two lookups stops at the first entry it comes to.
    assert statement.upper().count(" LIMIT 1)") == 2, statement
    if getattr(connection, "mysql_is_mariadb", False):
        pytest.skip(
            "MariaDB's EXPLAIN has no form that says which way a lookup reads "
            "the index, so the plan is not checked there"
        )
    rows = plan_of(statement)
    plan = "\n".join(" ".join(str(value) for value in row) for row in rows)
    if connection.vendor == "postgresql":
        # Each lookup walks the index backward, under a limit of one.
        lines = plan.splitlines()
        scans = [i for i, line in enumerate(lines) if f" on {TICKS}" in line]
        assert scans, plan
        for i in scans:
            assert "Scan Backward using ox_tick_uniq" in lines[i], plan
            assert lines[i - 1].lstrip().startswith("->  Limit"), plan
    elif connection.vendor == "mysql":
        # Each lookup reads the index in reverse from the key's last entry,
        # which MySQL has written in two ways over its 8.x releases. A sort
        # of the lookup's rows is every tick of the key read and sorted.
        lookups = [line for line in plan.splitlines() if " on U0 " in line]
        assert lookups, plan
        for line in lookups:
            assert "Covering index lookup on U0 using ox_tick_uniq" in line, plan
            assert "(reverse)" in line or "iterate backwards" in line, plan
        assert "Sort: U0." not in plan, plan
    else:
        # Each lookup searches the index by the key, and nothing in the
        # lookup sorts what it finds: the index's own order is the order
        # asked for, so the first entry it comes to is the one its limit
        # takes. A plan's row names the row it is a step of.
        searches = [row for row in rows if " U0 " in f"{row[3]} "]
        assert searches, plan
        for _step, lookup, _unused, detail in searches:
            assert "SEARCH U0 USING COVERING INDEX" in detail, plan
            assert "(schedule_name=?)" in detail, plan
            sorts = [r for r in rows if r[1] == lookup and "TEMP B-TREE" in r[3]]
            assert not sorts, plan


@pytest.mark.django_db
class TestTheLastTickColumnOnALongHistory:
    def test_the_page_seeks_each_schedules_newest_tick(self, client, admin_user):
        schedules = [a_schedule(f"s{i}") for i in range(5)]
        quiet = a_schedule("quiet")
        a_history(schedules)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        page = body(response)
        assert page.count(f">{newest_as_shown(schedules[0])}<") == len(schedules)
        assert page.count(">never<") == 1
        assert f">{quiet.name}</a>" in page
        # One statement for the page, whatever it lists.
        (statement,) = tick_statements(queries)
        assert_seeks_each_newest_tick(statement)

    def test_so_does_the_change_forms_warning(self, client, admin_user):
        schedules = [a_schedule(f"s{i}") for i in range(5)]
        a_history(schedules)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.get(reverse(CHANGE, args=[schedules[2].pk]))

        assert response.status_code == 200
        (statement,) = tick_statements(queries)
        assert_seeks_each_newest_tick(statement)

    @pytest.mark.parametrize("action", ["enable_selected", "run_once_now"])
    def test_so_do_the_warnings_after_an_action(self, client, admin_user, action):
        schedules = [a_schedule(f"s{i}", enabled=False) for i in range(5)]
        a_history(schedules)
        client.force_login(admin_user)

        with CaptureQueriesContext(connection) as queries:
            response = client.post(
                reverse(CHANGELIST),
                {
                    "action": action,
                    "_selected_action": [str(row.pk) for row in schedules],
                },
            )

        assert response.status_code == 302
        # The page the action was chosen on, and then the schedules it
        # acted on: one statement each.
        statements = tick_statements(queries)
        assert len(statements) == 2
        for statement in statements:
            assert_seeks_each_newest_tick(statement)


@pytest.mark.django_db
class TestTheLastTickColumnShowsWhatNewestFirstAloneWouldShow:
    """
    The page's lookup is ordered by the schedule's key and then by the tick.
    Under the lookup's equality on the key that finds the tick that ordering
    by the tick alone finds, which is how `newest_as_shown` reads it.
    """

    def test_for_each_schedule_whatever_the_others_hold(self, client, admin_user):
        at = timezone.now().replace(second=0, microsecond=0) - timedelta(days=1)
        own = [a_schedule(f"own-{i}") for i in range(3)]
        shared = [a_schedule(f"shared-{i}") for i in range(3)]
        never = a_schedule("never-ran")
        # Several ticks each, the newest written neither first nor last, and
        # a different one for each schedule.
        for i, schedule in enumerate(own):
            for minutes in (40, 5 + i, 90, 20):
                a_tick(schedule, at - timedelta(minutes=minutes))
        # The same instants under three keys.
        for minutes in (30, 0, 60):
            for schedule in shared:
                a_tick(schedule, at - timedelta(minutes=minutes))
        # A newer tick under a key that the last schedule's is the start of.
        OxScheduleTick.objects.create(
            schedule_name=f"db:{never.pk}0",
            scheduled_for=at + timedelta(hours=1),
            created_at=at,
        )
        client.force_login(admin_user)

        response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        cells = {
            str(row): django_ox.admin.OxScheduleAdmin.last_tick(None, row)
            for row in response.context["cl"].result_list
        }
        expected = {s.name: newest_as_shown(s) for s in (*own, *shared)}
        expected[never.name] = "never"
        assert cells == expected
        assert len({cells[s.name] for s in own}) == len(own)
        assert {cells[s.name] for s in shared} == {f"{at:%Y-%m-%d %H:%M}"}


@pytest.mark.django_db
class TestAScheduleDeletedWhileThePageIsRead:
    def test_is_shown_as_the_page_read_it_with_no_last_tick(
        self, client, admin_user, monkeypatch
    ):
        # The page reads its rows and then, in a statement of its own, their
        # newest ticks, each looked up from the schedule's row. Another
        # session can delete a schedule in between, and then there is no
        # row to look its tick up from.
        kept, goes = a_schedule("kept"), a_schedule("goes")
        at = timezone.now().replace(microsecond=0) - timedelta(hours=1)
        a_tick(kept, at)
        a_tick(goes, at)
        read_schedules = django_ox.admin.read_schedules

        def read_and_then_it_goes(*args, **kwargs):
            rows = read_schedules(*args, **kwargs)
            by_sql(f"DELETE FROM {SCHEDULES} WHERE id = {int(goes.pk)}")  # noqa: S608
            return rows

        monkeypatch.setattr(django_ox.admin, "read_schedules", read_and_then_it_goes)
        client.force_login(admin_user)

        response = client.get(reverse(CHANGELIST))

        assert response.status_code == 200
        cells = {
            str(row): django_ox.admin.OxScheduleAdmin.last_tick(None, row)
            for row in response.context["cl"].result_list
        }
        assert cells == {"goes": "never", "kept": f"{at:%Y-%m-%d %H:%M}"}
