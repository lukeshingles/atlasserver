"""Stop *new* accounts sharing an email address, while leaving existing ones alone.

forms.email_is_taken() has always checked this, but only in a form clean() method, so two
concurrent registrations could both pass validation, and the admin and the shell bypassed it
altogether. Duplicates make the password reset flow ambiguous: it mails a reset link for every
matching account.

The rule is enforced at the database level rather than only in the form, but it cannot simply be a
unique index: this database already has accounts sharing an address, and those are to be kept. So
each address gets exactly one row that occupies the unique slot -- the oldest -- and any further
rows sharing it record that address as exempt and are excluded from the index.

That combination is what makes "existing yes, new no" hold. The grandfathered rows are exempt, so
they survive; the address they share is still claimed by their oldest sibling, so a new account
cannot take it either. A row created after this migration is never exempt, so two new accounts
cannot collide with each other.

The exemption stores the address rather than a yes/no flag, so it lapses the moment the row moves
on. A boolean would have put the row outside the index permanently: a grandfathered account that
later changed address -- through the confirmed email-change flow, or from the shell -- would have
gone on claiming nothing, leaving its new address free for someone else to register and recreating
the ambiguity this exists to remove. Comparing against the address means the row is constrained
again as soon as it holds anything other than the one it was pardoned for.

The columns are added here rather than on the model because this project uses
django.contrib.auth's User, which it does not own; swapping in a custom user model to add two
attributes would be a far larger change to a live site with existing accounts. Django never sees
them: it selects and inserts explicit column lists built from the model.

Blank addresses are excluded rather than collapsed: User.email is blank=True (createsuperuser will
happily leave it empty), so several accounts may legitimately have none, and an account with no
address is not ambiguous for password reset.

Deploy note: on MySQL/MariaDB this adds a stored generated column, which rebuilds the table, so
give it a maintenance window.

Upgrade note: because this reaches past the ORM, Django's migration state does not know the column
or the index exist. Anything that rebuilds auth_user from state therefore drops them. The
dependency below orders this after every auth migration Django ships today, which covers the case
that can happen now; a *future* Django adding another auth migration that alters auth_user would
reintroduce the risk on SQLite (MySQL alters in place and is unaffected). If that happens, the
answer is a follow-up migration that re-adds the column and index rather than a change here.
"""

from django.conf import settings
from django.db import migrations
from django.db.models import Count
from django.db.models.functions import Lower

INDEX_NAME = "auth_user_email_ci_uniq"

# Holds the one address a row is allowed to share, lowercased, or NULL for every row that has no
# such licence -- which is every row created after this migration. A real column, not a generated
# one: it records a decision taken once, at migration time, and nothing since should revise it.
EXEMPT_COLUMN = "email_unique_exempt"

# MySQL/MariaDB only. A generated column rather than a functional index on the expression: MariaDB
# has no functional indexes, and the README lists it as a supported server alongside MySQL, while
# Django reports connection.vendor == "mysql" for both. STORED generated columns work on both, so
# this is a single path -- and it is the one CI exercises against MySQL 8.4.
GENERATED_COLUMN = "email_ci"


def grandfather_existing_duplicates(apps, schema_editor):
    """Exempt every row that already shares an address, except the oldest of each set.

    Leaving the oldest unexempt is what keeps the address claimed: a new account trying to register
    it still collides with that row. Exempting all of them instead would hand the address back.
    """
    user_model = apps.get_model("auth", "User")

    duplicated = (
        user_model.objects.exclude(email="")
        .annotate(lowered=Lower("email"))
        .values("lowered")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .values_list("lowered", flat=True)
    )

    exempted = 0
    for address in duplicated:
        # by id: the lowest is the account that has held the address longest
        sharing = list(
            user_model.objects.annotate(lowered=Lower("email"))
            .filter(lowered=address)
            .order_by("id")
            .values_list("id", flat=True)
        )
        keep, exempt = sharing[0], sharing[1:]
        placeholders = ", ".join(["%s"] * len(exempt))
        # the address and the ids are passed as parameters; the only things interpolated into the
        # statement are the constant above and a run of %s placeholders, so there is no user input
        sql = f"UPDATE auth_user SET {EXEMPT_COLUMN} = %s WHERE id IN ({placeholders})"  # noqa: S608
        schema_editor.execute(sql, [address, *exempt])
        exempted += len(exempt)
        print(f"  grandfathered {len(exempt)} account(s) sharing {address!r}; id {keep} keeps the address")

    if exempted:
        print(f"  {exempted} existing duplicate account(s) exempted; new duplicates are still refused")


def create_index(apps, schema_editor):
    vendor = schema_editor.connection.vendor

    if vendor == "mysql":
        schema_editor.execute(f"ALTER TABLE auth_user ADD COLUMN {EXEMPT_COLUMN} VARCHAR(254) NULL")
        grandfather_existing_duplicates(apps, schema_editor)
        # No partial indexes on either engine, so the rows that must not be constrained -- blank
        # and grandfathered -- are excluded by generating NULL, which a unique index allows to
        # repeat. LOWER() guards against a _bin or _cs collation. The exemption is compared rather
        # than merely tested, so a row that moves off its pardoned address rejoins the index;
        # STORED redoes that on every write.
        schema_editor.execute(
            f"ALTER TABLE auth_user ADD COLUMN {GENERATED_COLUMN} VARCHAR(254) GENERATED ALWAYS AS "
            f"(CASE WHEN email = '' OR LOWER(email) = {EXEMPT_COLUMN} THEN NULL ELSE LOWER(email) END) STORED"
        )
        schema_editor.execute(f"CREATE UNIQUE INDEX {INDEX_NAME} ON auth_user ({GENERATED_COLUMN})")
    elif vendor == "sqlite":
        # the test default. SQLite has partial indexes and COLLATE NOCASE, so it needs no generated
        # column -- the exemption goes straight into the index's WHERE clause.
        schema_editor.execute(f"ALTER TABLE auth_user ADD COLUMN {EXEMPT_COLUMN} VARCHAR(254) NULL")
        grandfather_existing_duplicates(apps, schema_editor)
        schema_editor.execute(
            f"CREATE UNIQUE INDEX {INDEX_NAME} ON auth_user (email COLLATE NOCASE) "
            f"WHERE email != '' AND ({EXEMPT_COLUMN} IS NULL OR LOWER(email) != {EXEMPT_COLUMN})"
        )
    else:
        # named rather than guessed at: COLLATE NOCASE is SQLite's spelling, and running it against
        # (say) PostgreSQL produced a syntax error partway through the migration that said nothing
        # about the real problem. Adding a backend means writing its case-insensitive unique index
        # here, not falling through to another engine's.
        msg = (
            f"Cannot add the case-insensitive unique index on auth_user.email: no SQL is defined for the "
            f"{vendor!r} backend. Add a branch here for it, using an index that ignores case and skips "
            f"blank addresses and rows whose {EXEMPT_COLUMN} matches the address they hold."
        )
        raise NotImplementedError(msg)


def drop_index(apps, schema_editor):
    if schema_editor.connection.vendor == "mysql":
        schema_editor.execute(f"DROP INDEX {INDEX_NAME} ON auth_user")
        schema_editor.execute(f"ALTER TABLE auth_user DROP COLUMN {GENERATED_COLUMN}")
        schema_editor.execute(f"ALTER TABLE auth_user DROP COLUMN {EXEMPT_COLUMN}")
    else:
        schema_editor.execute(f"DROP INDEX {INDEX_NAME}")
        schema_editor.execute(f"ALTER TABLE auth_user DROP COLUMN {EXEMPT_COLUMN}")


class Migration(migrations.Migration):
    # MySQL and MariaDB cannot roll back DDL, and Django refuses to run raw DDL inside a
    # transaction on such a backend ("Executing DDL statements while in a transaction on databases
    # that can't perform a rollback is prohibited"), which is what the RunPython below does. The
    # atomicity was never real there in any case: each ALTER commits as it executes.
    atomic = False

    dependencies = [
        ("forcephot", "0004_alter_task_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        # after every auth migration that exists, not merely after the user model was created.
        # The swappable dependency alone only orders this after auth.0001, which leaves the rest
        # of contrib.auth free to run afterwards -- and on SQLite an ALTER is a table remake built
        # from migration *state*, which knows nothing of the column and index added below, so a
        # later auth migration would copy the table without them and silently take the constraint
        # with it.
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [migrations.RunPython(create_index, drop_index)]
