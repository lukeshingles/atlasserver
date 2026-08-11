"""Require every task to have a target: an MPC object name, or both coordinates.

ForcePhotTaskSerializer.validate() has always enforced this, but it was the only thing that did,
so the admin and the shell could create a task with no target at all -- Task.__str__ still carries
a branch for exactly that case.

Deploy note: this can fail, on purpose. The pre-check reports how many existing rows violate the
rule and stops without changing anything, because a bare constraint violation from the database
names neither the rows nor the reason. Give the offenders a target or delete them; archiving does
not help, because neither the pre-check nor the constraint looks at is_archived. Do not relax the
constraint to fit them.
"""

from django.conf import settings
from django.db import migrations
from django.db import models
from django.db.models.functions import Replace
from django.db.models.functions import Trim
from django.db.models.lookups import Exact

# The rule, written once here and used twice below: the pre-check excludes it to find violators,
# AddConstraint enforces it. Spelled out by hand rather than imported, because a migration must
# keep working when the model moves on -- but spelled *exactly* as Task.Meta spells it, negation
# included, since Django compares constraints by deconstructed form and reads any rewording as
# model drift. TRIM strips only spaces, so the rest of the set is replaced with spaces first.
_MPC_NAME_WHITESPACE = " \t\n\r\v\f\u00a0"


def _space_normalised(field):
    """Return the field with the whitespace above collapsed to spaces and then trimmed.

    A copy of the model helper of the same name, because a migration must not import from the
    live model.
    """
    expression = field
    for character in _MPC_NAME_WHITESPACE:
        if character != " ":
            expression = Replace(expression, models.Value(character), models.Value(" "))

    return Trim(expression)


_NORMALISED_MPC_NAME = _space_normalised("mpc_name")
_BLANK_MPC_NAME = models.Q(Exact(_NORMALISED_MPC_NAME, models.Value("")))
_HAS_MPC_NAME = models.Q(mpc_name__isnull=False) & ~_BLANK_MPC_NAME
_NO_MPC_NAME = models.Q(mpc_name__isnull=True) | _BLANK_MPC_NAME
TARGET_PRESENT = (_HAS_MPC_NAME & models.Q(ra__isnull=True, dec__isnull=True)) | (
    _NO_MPC_NAME & models.Q(ra__isnull=False, dec__isnull=False)
)


def check_every_task_has_a_target(apps, schema_editor):
    task_model = apps.get_model("forcephot", "Task")

    violators = task_model.objects.exclude(TARGET_PRESENT)

    count = violators.count()
    if count:
        examples = ", ".join(str(taskid) for taskid in violators.values_list("id", flat=True)[:10])
        msg = (
            f"Cannot add the target constraint: {count} task(s) specify neither an MPC object name "
            f"nor a complete (ra, dec) pair, or specify both.\n"
            f"  example task ids: {examples}\n"
            "Give these rows a target or delete them, then run this migration again."
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
            constraint=models.CheckConstraint(condition=TARGET_PRESENT, name="task_target_is_mpcname_or_radec"),
        ),
    ]
