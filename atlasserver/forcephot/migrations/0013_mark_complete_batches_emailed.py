"""Mark the finished tasks of each complete batch as emailed.

Migration 0011 added Task.email_sent and gave each existing row False, also the rows of a batch
that had its email already. The deployment applies migrations before it restarts the task runner,
so a runner of the code before 0011 could also finish a batch after that migration and leave its
rows False. The maintenance of the task runner sends the email of each batch that has a finished
task with email_sent False and no unfinished task (taskrunner.main.send_pending_batch_emails).
Without this migration, it would send a second email for each such old batch.

A batch that has an unfinished task now did not get its email, because a batch gets its email
when its last task finishes. Its rows stay as they are.

The writes are in chunks and not in one transaction: the table is large, and a chunk that is
written stays correct if a later chunk fails. A second run finds the same rows less the rows
that the first run wrote.
"""

from django.db import migrations

# the number of rows that one UPDATE writes
CHUNK_ROWS = 5000


def mark_complete_batches(apps, schema_editor):
    """Set email_sent on each finished task with email_sent False whose batch has no unfinished task.

    A batch is the tasks of one user with send_email and one submission time, as in
    taskrunner.main.send_email_if_needed.
    """
    alias = schema_editor.connection.alias if schema_editor is not None else "default"
    task = apps.get_model("forcephot", "Task")

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
            .filter(pk__gt=lastpk, finishtimestamp__isnull=False, email_sent=False)
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
