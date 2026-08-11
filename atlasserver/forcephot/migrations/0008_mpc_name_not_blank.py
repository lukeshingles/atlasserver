"""Guarantee that mpc_name is NULL, "" or a real name -- never whitespace alone.

Task.save() normalises the field, but bulk_create, bulk_update, QuerySet.update() and raw SQL do
not go through it, so the guarantee has to be in the database. What it buys is that every reader
can test the field for truth instead of re-deriving what counts as blank: five of them were doing
that independently (the target constraint, the task runner, __str__, the usage stats and the queue
page), and each one was a place for the definitions to drift apart.

Existing rows are normalised first, because the constraint would otherwise refuse to apply to a
database that already holds a padded name.
"""

from django.conf import settings
from django.db import migrations
from django.db import models
from django.db.models.functions import Replace
from django.db.models.functions import Trim
from django.db.models.lookups import Exact

# Task.MPC_NAME_WHITESPACE and _space_normalised(), written out because a migration must keep
# working when the model moves on. See migration 0006, which spells out the same thing.
_MPC_NAME_WHITESPACE = " \t\n\r\v\f\u00a0"


def _space_normalised(field):
    """Return the field with that whitespace collapsed to spaces and then trimmed."""
    expression = field
    for character in _MPC_NAME_WHITESPACE:
        if character != " ":
            expression = Replace(expression, models.Value(character), models.Value(" "))

    return Trim(expression)


BLANK_MPC_NAME = models.Q(Exact(_space_normalised("mpc_name"), models.Value("")))


def normalise_existing_names(apps, schema_editor):
    """Strip the padding from every stored name, leaving "" where nothing survives."""
    task_model = apps.get_model("forcephot", "Task")

    # only the rows that need it, and read as a pair so the log below can say which is which
    padded = [
        (taskid, name)
        for taskid, name in task_model.objects.exclude(mpc_name__isnull=True)
        .exclude(mpc_name="")
        .values_list("id", "mpc_name")
        if name != name.strip(_MPC_NAME_WHITESPACE)
    ]

    for taskid, name in padded:
        task_model.objects.filter(id=taskid).update(mpc_name=name.strip(_MPC_NAME_WHITESPACE))

    if padded:
        blanked = sum(1 for _, name in padded if not name.strip(_MPC_NAME_WHITESPACE))
        print(f"  normalised {len(padded)} padded mpc_name(s); {blanked} of them held nothing else")


def noop(apps, schema_editor):
    """Nothing to undo: the padding it removed was never meaningful."""


class Migration(migrations.Migration):
    dependencies = [
        ("forcephot", "0007_pending_email_verification"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(normalise_existing_names, noop),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=models.Q(mpc_name__isnull=True) | models.Q(mpc_name="") | ~BLANK_MPC_NAME,
                name="task_mpc_name_not_blank",
            ),
        ),
    ]
