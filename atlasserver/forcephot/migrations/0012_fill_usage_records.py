"""Fill the table of the usage records from the tasks that the database has.

The queue order reads the usage of each user from this table; see forcephot.queue.charge_usage.
With an empty table, each user has no usage at the first start of the new task runner, and the
users with the most usage then get the same place as all other users.

This migration only writes rows, and all its writes are one transaction. If it fails, the table
stays empty, and a second run of the migration starts from the same condition.
"""

import datetime as dt

from django.db import migrations

# the same number as forcephot.queue.USAGE_RETENTION_DAYS. A migration keeps its own copy, because
# it must give the same result after a change of the application code.
USAGE_RETENTION_DAYS = 35


def fill_usage_records(apps, schema_editor):
    """Make one usage record for each user and day from the finished tasks of the last days.

    The result is approximate. The database does not have the rows of the tasks that a user
    deleted while they ran, the rows of the archived API tasks that finished more than 7 days ago,
    or the attempts that failed. The task runner writes exact records from its first start.

    A table that has records stays as it is. The reverse of this migration keeps the records. A
    second fill would then make a second record for a user and day, and the unique constraint of
    the table refuses that record.
    """
    alias = schema_editor.connection.alias
    task = apps.get_model("forcephot", "Task")
    usageday = apps.get_model("forcephot", "UsageDay")

    if usageday.objects.using(alias).exists():
        return

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
        ("forcephot", "0011_usage_day"),
    ]

    operations = [
        # The reverse operation does nothing: the reverse of migration 0011 deletes the table.
        migrations.RunPython(fill_usage_records, migrations.RunPython.noop),
    ]
