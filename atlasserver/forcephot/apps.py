from django.apps import AppConfig


class ForcephotConfig(AppConfig):
    # job filenames are only ever built from the id (f"job{id:05d}"), never parsed at a fixed
    # width, so widening the id does not change any path that already exists on disk or on sc01
    default_auto_field = "django.db.models.BigAutoField"
    name = "atlasserver.forcephot"
    verbose_name = "ATLAS Forced Photometry Interface"
