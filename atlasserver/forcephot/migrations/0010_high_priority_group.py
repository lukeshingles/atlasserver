"""Make the group whose members have priority in the queue of the task runner.

The group has no permissions. The queue reads only its member list; see
forcephot.queue.priority_userids.
"""

from django.db import migrations

# the same text as forcephot.queue.HIGH_PRIORITY_GROUP. A migration keeps its own copy, because it
# must give the same result after a change of the application code.
HIGH_PRIORITY_GROUP = "High priority"


def make_priority_group(apps, schema_editor):
    group = apps.get_model("auth", "Group")
    group.objects.using(schema_editor.connection.alias).get_or_create(name=HIGH_PRIORITY_GROUP)


class Migration(migrations.Migration):
    dependencies = [
        ("forcephot", "0009_task_attempt_count"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        # The reverse operation keeps the group. A reverse that deletes the group also deletes its
        # member list, and the forward operation cannot make the list again.
        migrations.RunPython(make_priority_group, migrations.RunPython.noop),
    ]
