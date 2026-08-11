"""Keep the pending-verification marker in step with accounts activated outside the flow.

verify_email clears the marker, but an administrator ticking is_active -- the usual answer to "I
never got the email" -- does not. A marker left on an active account matters once that account is
disabled: awaiting_verification() would read it as unverified again, and the resend path would
hand out a link undoing the disable.

Known gap: QuerySet.update() emits no signals, so update(is_active=True) leaves the marker behind.
That is what update() is, a deliberate bypass of save() that skips auto_now and full_clean too;
closing it means a database trigger, which is a lot of machinery for a shell idiom. Every
Model.save() path is covered. If you do activate accounts with update(), delete their rows too.
"""

import typing as t

from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver

from atlasserver.forcephot.models import PendingEmailVerification


@receiver(post_save, sender=settings.AUTH_USER_MODEL, dispatch_uid="forcephot.clear_verification_marker")
def clear_verification_marker(sender: t.Any, instance: t.Any, **kwargs: t.Any) -> None:
    """Delete the pending-verification row once its account is active, however that happened."""
    # login() saves with update_fields=["last_login"], which is by far the most frequent write to
    # this table; skipping the ones that cannot have touched is_active keeps this off that path.
    update_fields = kwargs.get("update_fields")
    if update_fields is not None and "is_active" not in update_fields:
        return

    if instance.is_active:
        PendingEmailVerification.objects.filter(user=instance).delete()
