"""Make the table of the usage records, and fill it from the tasks that the database has.

This migration also adds three fields of a task:

- the time before which the task does not start again after a failed attempt;
- the marker that a slot took the email of the batch of the task;
- the marker of an attempt whose end has no record.

The queue order reads the usage of each user from this table; see forcephot.queue.charge_usage.
With an empty table, each user has no usage at the first start of the new task runner, and the
users with the most usage then get the same place as all other users.
"""

import datetime as dt

import django.db.models.deletion
from django.conf import settings
from django.db import migrations
from django.db import models

# the same number as forcephot.queue.USAGE_RETENTION_DAYS. A migration keeps its own copy, because
# it must give the same result after a change of the application code.
USAGE_RETENTION_DAYS = 35


def fill_usage_records(apps, schema_editor):
    """Make one usage record for each user and day from the finished tasks of the last days.

    The result is approximate. The database does not have the rows of the tasks that a user
    deleted while they ran, the rows of the archived API tasks that finished more than 7 days ago,
    or the attempts that failed. The task runner writes exact records from its first start.
    """
    alias = schema_editor.connection.alias
    task = apps.get_model("forcephot", "Task")
    usageday = apps.get_model("forcephot", "UsageDay")

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=USAGE_RETENTION_DAYS)
    finished = (
        task.objects.using(alias)
        .filter(finishtimestamp__gt=cutoff, starttimestamp__isnull=False)
        .values_list("user_id", "starttimestamp", "finishtimestamp")
    )
    # (user id, day) to [seconds, tasks]
    totals: dict[tuple[int, dt.date], list[float]] = {}
    for userid, started, finishedat in finished.iterator():
        total = totals.setdefault((userid, finishedat.astimezone(dt.UTC).date()), [0.0, 0])
        total[0] += max(0.0, (finishedat - started).total_seconds())
        total[1] += 1

    usageday.objects.using(alias).bulk_create(
        [
            usageday(user_id=userid, day=day, seconds=seconds, tasks=int(tasks))
            for (userid, day), (seconds, tasks) in totals.items()
        ],
        batch_size=1000,
    )


class Migration(migrations.Migration):
    dependencies = [
        ("forcephot", "0010_high_priority_group"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="UsageDay",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("day", models.DateField()),
                ("seconds", models.FloatField(default=0.0)),
                ("tasks", models.IntegerField(default=0)),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="usagedays",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [models.Index(fields=["day"], name="usageday_day_idx")],
                "constraints": [models.UniqueConstraint(fields=("user", "day"), name="usageday_user_day_unique")],
            },
        ),
        # The reverse operation does nothing: the reverse of CreateModel deletes the table.
        migrations.RunPython(fill_usage_records, migrations.RunPython.noop),
        migrations.AddField(
            model_name="task",
            name="retry_after",
            field=models.DateTimeField(blank=True, default=None, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="task",
            name="email_sent",
            field=models.BooleanField(default=False, editable=False),
        ),
        migrations.AddField(
            model_name="task",
            name="attempt_open",
            field=models.BooleanField(default=False, editable=False),
        ),
    ]
