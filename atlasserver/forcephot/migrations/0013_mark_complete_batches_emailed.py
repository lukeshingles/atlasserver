"""Mark the finished tasks of each complete batch as emailed.

Migration 0011 added Task.email_sent and gave each existing row False, also the rows of a batch
that had its email already. The deployment applies migrations before it restarts the task runner,
so a runner of the code before 0011 could also finish a batch after that migration and leave its
rows False. The maintenance of the task runner sends the email of each batch that has a finished
task with email_sent False and no unfinished task (taskrunner.main.send_pending_batch_emails).
Without this migration, it would send a second email for each such old batch.

A batch that has an unfinished task now did not get its email, because a batch gets its email
when its last task finishes. Its rows stay as they are.

The data does not show if a complete batch from before the field got its email. A batch whose last
task its user cancelled got none, and it looks the same as a batch that got its email. Thus this
migration marks only the tasks that finished before the field was reliable: the time of migration
0011 plus RESTART_MARGIN. After that time, a runner that writes the field ran, and a complete batch
with email_sent False got no email. Its rows stay as they are, and the maintenance then sends the
email that the batch did not get.

The writes are in chunks and not in one transaction: the table is large, and a chunk that is
written stays correct if a later chunk fails. A second run finds the same rows less the rows
that the first run wrote.
"""

import datetime as dt

from django.db import connection as default_connection
from django.db import migrations
from django.db.migrations.recorder import MigrationRecorder

# the number of rows that one UPDATE writes
CHUNK_ROWS = 5000

# The time after migration 0011 from which a runner that writes email_sent ran. The README
# restarts the task runner directly after `migrate`. A long margin prefers a lost email to a
# second email: a complete batch before the reliable time gets no email from the maintenance.
RESTART_MARGIN = dt.timedelta(days=1)


def mark_complete_batches(apps, schema_editor):
    """Set email_sent on each task that finished before the reliable time and whose batch has no unfinished task.

    A batch is the tasks of one user with send_email and one submission time, as in
    taskrunner.main.send_email_if_needed. The reliable time is the time of migration 0011 plus
    RESTART_MARGIN; see the docstring of the module.
    """
    connection = schema_editor.connection if schema_editor is not None else default_connection
    alias = connection.alias
    task = apps.get_model("forcephot", "Task")

    applied = (
        MigrationRecorder(connection)
        .migration_qs.filter(app="forcephot", name="0011_usage_day")
        .values_list("applied", flat=True)
        .first()
    )
    # 0013 depends on 0011, so the record exists. Without it, the migration marks each task that
    # finished before now, which sends no second email.
    reliable = applied + RESTART_MARGIN if applied is not None else dt.datetime.now(dt.UTC)

    openbatches = set(
        task.objects.using(alias)
        .filter(finishtimestamp__isnull=True, send_email=True)
        .values_list("user_id", "timestamp")
        .distinct()
    )

    lastpk = 0
    while True:
        rows = list(
            task.objects.using(alias)
            .filter(pk__gt=lastpk, finishtimestamp__lt=reliable, email_sent=False)
            .order_by("pk")
            .values_list("pk", "user_id", "timestamp")[:CHUNK_ROWS]
        )
        if not rows:
            return

        lastpk = rows[-1][0]
        complete = [pk for pk, userid, timestamp in rows if (userid, timestamp) not in openbatches]
        if complete:
            task.objects.using(alias).filter(pk__in=complete).update(email_sent=True)


class Migration(migrations.Migration):
    # see the docstring of the module for why the chunks are not one transaction
    atomic = False

    dependencies = [
        ("forcephot", "0012_fill_usage_records"),
    ]

    operations = [
        migrations.RunPython(mark_complete_batches, migrations.RunPython.noop, elidable=True),
    ]
