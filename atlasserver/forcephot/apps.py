from django.apps import AppConfig


class ForcephotConfig(AppConfig):
    # job filenames are only ever built from the id (f"job{id:05d}"), never parsed at a fixed
    # width, so widening the id does not change any path that already exists on disk or on sc01
    default_auto_field = "django.db.models.BigAutoField"
    name = "atlasserver.forcephot"
    verbose_name = "ATLAS Forced Photometry Interface"

    def ready(self) -> None:
        """Connect the signal handlers, which is what importing the module does."""
        # noqa/import-outside-toplevel is the documented shape for this: signals must be connected
        # after the app registry is populated, which is exactly what ready() means.
        from atlasserver.forcephot import signals  # noqa: F401  (imported for its @receiver)
