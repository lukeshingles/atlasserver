"""Widen the Task primary key from AutoField (int) to BigAutoField (bigint).

Deploy note: MySQL cannot do int -> bigint in place, so this rebuilds the table, and Django's
schema editor widens the parent_task_id self-FK in the same operation. Both need a maintenance
window sized to the row count -- take the server down rather than running it under load.

The ceiling this lifts is not close (AutoField runs out at 2.1 billion tasks), so deferring the
migration is reasonable; the model change is safe to ship without it, because new ids keep being
allocated from the existing int column until it runs.
"""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("forcephot", "0003_task_callback_url_task_task_userlist_idx_and_more")]

    operations = [
        migrations.AlterField(
            model_name="task",
            name="id",
            field=models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID"),
        )
    ]
