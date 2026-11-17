"""Make the table of the usage records, and add three fields of a task.

The fields of a task are:

- the time before which the task does not start again after a failed attempt;
- the marker that a slot took the email of the batch of the task;
- the marker of an attempt whose end has no record.

The queue order reads the usage of each user from the table; see forcephot.queue.charge_usage.
Migration 0012 fills the table from the tasks that the database has.

This migration only changes the schema. MariaDB does not undo a change of the schema when a
later operation of the same migration fails. A data operation in this migration could thus fail
after the table is there, and a second run of the migration would then stop at the table.
"""

import django.db.models.deletion
from django.conf import settings
from django.db import migrations
from django.db import models


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
