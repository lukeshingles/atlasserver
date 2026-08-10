"""Keep the pending-verification marker in step with accounts activated outside the flow.

forcephot.views.verify_email clears the marker when someone follows their link, but that is not
the only way an account becomes active. An administrator ticking is_active in the Django admin
(the usual answer to "I never got the email"), or a shell one-liner doing the same, would leave
the row behind on an account that is now perfectly ordinary.

That matters later rather than immediately: awaiting_verification() reads the marker, so if the
account were disabled afterwards it would be classified as unverified again -- and the resend path
would hand out a link that undoes the administrative disable. The marker has to mean "registered
and still unproved", so it is cleared wherever activation actually happens.
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
