"""Enforce case-insensitive unique email addresses on auth_user at the database level.

forms.email_is_taken() has always checked this, but only in a form clean() method, so two
concurrent registrations could both pass validation, and the admin and the shell bypassed it
altogether. Duplicates make the password reset flow ambiguous: it mails a reset link for every
matching account.

The constraint is declared here rather than on the model because this project uses
django.contrib.auth's User, which it does not own; swapping in a custom user model to add one
attribute would be a far larger change to a live site with existing accounts.

Deploy note: this can fail, on purpose. If any two accounts already share an address (ignoring
case) the forwards step raises with the addresses listed, and nothing is changed. Resolve those
accounts by hand and run it again -- do not weaken the index to get past it. On MySQL/MariaDB it
also adds a stored generated column, which rebuilds the table, so give it a maintenance window.

Blank addresses are excluded rather than collapsed: User.email is blank=True (createsuperuser will
happily leave it empty), so several accounts may legitimately have none, and an account with no
address is not ambiguous for password reset.
"""

from django.conf import settings
from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower

INDEX_NAME = "auth_user_email_ci_uniq"

# MySQL/MariaDB only. A generated column rather than a functional index on the expression: MariaDB
# has no functional indexes, and the README lists it as a supported server alongside MySQL, while
# Django reports connection.vendor == "mysql" for both. STORED generated columns work on both, so
# this is a single path -- and it is the one CI exercises against MySQL 8.4.
#
# Django never sees the column: it selects and inserts explicit column lists built from the model,
# and this is not on the model.
GENERATED_COLUMN = "email_ci"


def check_no_duplicate_emails(apps, schema_editor):
    """Fail with an actionable message rather than letting the CREATE INDEX raise a duplicate-key error."""
    user_model = apps.get_model("auth", "User")

    duplicates = (
        user_model.objects.exclude(email="")
        .annotate(lowered=Lower("email"))
        .values("lowered")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .order_by("lowered")
    )

    conflicts = [f"{row['lowered']} ({row['count']} accounts)" for row in duplicates]
    if conflicts:
        listed = "\n  ".join(conflicts)
        msg = (
            f"Cannot add a unique index on auth_user.email: {len(conflicts)} address(es) are used by more "
            f"than one account.\n  {listed}\n"
            "Merge or correct these accounts, then run this migration again."
        )
        raise RuntimeError(msg)


def create_index(apps, schema_editor):
    check_no_duplicate_emails(apps, schema_editor)

    if schema_editor.connection.vendor == "mysql":
        # Neither engine has partial indexes, so blank addresses are excluded by generating NULL
        # for them: a unique index permits repeated NULLs. LOWER() is belt and braces -- the
        # default collation of both is already case-insensitive -- so that the index cannot
        # silently become case-sensitive on a server configured with a _bin or _cs collation.
        schema_editor.execute(
            f"ALTER TABLE auth_user ADD COLUMN {GENERATED_COLUMN} VARCHAR(254) "
            "GENERATED ALWAYS AS (CASE WHEN email = '' THEN NULL ELSE LOWER(email) END) STORED"
        )
        schema_editor.execute(f"CREATE UNIQUE INDEX {INDEX_NAME} ON auth_user ({GENERATED_COLUMN})")
    else:
        # SQLite (the test default) has partial indexes and COLLATE NOCASE, so it needs no column
        schema_editor.execute(f"CREATE UNIQUE INDEX {INDEX_NAME} ON auth_user (email COLLATE NOCASE) WHERE email != ''")


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor == "mysql":
        schema_editor.execute(f"DROP INDEX {INDEX_NAME} ON auth_user")
        schema_editor.execute(f"ALTER TABLE auth_user DROP COLUMN {GENERATED_COLUMN}")
    else:
        schema_editor.execute(f"DROP INDEX {INDEX_NAME}")


class Migration(migrations.Migration):
    dependencies = [
        ("forcephot", "0004_alter_task_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [migrations.RunPython(create_index, drop_index)]
