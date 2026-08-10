"""Require every task to have a target: an MPC object name, or both coordinates.

ForcePhotTaskSerializer.validate() has always enforced this, but it was the only thing that did,
so the admin and the shell could create a task with no target at all -- Task.__str__ still carries
a branch for exactly that case.

Deploy note: this can fail, on purpose. The pre-check reports how many existing rows violate the
rule and stops without changing anything, because a bare constraint violation from the database
names neither the rows nor the reason. If the offenders are old and harmless, archive or delete
them; do not relax the constraint to fit them.
"""

from django.conf import settings
from django.db import migrations
from django.db import models


def check_every_task_has_a_target(apps, schema_editor):
    task_model = apps.get_model("forcephot", "Task")

    has_mpc_name = models.Q(mpc_name__isnull=False) & ~models.Q(mpc_name="")
    has_radec = models.Q(ra__isnull=False, dec__isnull=False)

    # the same condition as the constraint, negated: anything that is neither form
    violators = task_model.objects.exclude(
        (has_mpc_name & models.Q(ra__isnull=True, dec__isnull=True)) | (~has_mpc_name & has_radec)
    )

    count = violators.count()
    if count:
        examples = ", ".join(str(taskid) for taskid in violators.values_list("id", flat=True)[:10])
        msg = (
            f"Cannot add the target constraint: {count} task(s) specify neither an MPC object name "
            f"nor a complete (ra, dec) pair, or specify both.\n"
            f"  example task ids: {examples}\n"
            "Archive or correct these rows, then run this migration again."
        )
        raise RuntimeError(msg)


def noop(apps, schema_editor):
    """Nothing to undo: the check only reads."""


class Migration(migrations.Migration):
    dependencies = [
        ("forcephot", "0005_auth_user_email_unique"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(check_every_task_has_a_target, noop),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("mpc_name__isnull", False),
                        models.Q(("mpc_name", ""), _negated=True),
                        ("dec__isnull", True),
                        ("ra__isnull", True),
                    ),
                    models.Q(
                        models.Q(("mpc_name__isnull", True), ("mpc_name", ""), _connector="OR"),
                        ("dec__isnull", False),
                        ("ra__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="task_target_is_mpcname_or_radec",
            ),
        ),
    ]
