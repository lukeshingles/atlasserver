"""Settings for running the test suite.

Run with:
    DJANGO_SETTINGS_MODULE=atlasserver.settings_test ./manage.py test

The production settings hardcode MySQL, so without an override `manage.py test` fails on any
machine that has no MySQL server running. Tests default to an in-memory SQLite database here; CI
sets ATLASSERVER_TEST_DB=mysql so that the suite still runs against the engine used in production.
"""

import os

# Set before the star import, because settings.py applies its production hardening under a plain
# `if not DEBUG:` at import time: assigning DEBUG afterwards would be too late, and the suite would
# then run with SECURE_SSL_REDIRECT (a 301 on every test-client request) and secure-only cookies
# that the plain-http test client never sends back. Undoing each of those by hand here instead is a
# list that has to be kept in step with settings.py by memory alone.
os.environ.setdefault("ATLASSERVER_DEBUG", "1")

from atlasserver.settings import *  # noqa: F403

DEBUG = True

if os.environ.get("ATLASSERVER_TEST_DB", "sqlite").lower() != "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# a real key is not needed to run the tests, but Django refuses to start without one
SECRET_KEY = os.environ.get("ATLASSERVER_DJANGO_SECRET_KEY") or "test-secret-key-not-for-production"

# the file-based caches are shared between runs (and with the dev server), so a stale entry from an
# earlier run whose task happened to receive the same id would leak into a test. Use locmem, which
# starts empty in every process.
CACHES = {
    name: {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": name}
    for name in ("default", "taskderived", "usagestats")
}

# EMAIL_BACKEND is deliberately not overridden here: Django's test runner already swaps in the
# locmem backend, so result emails land in django.core.mail.outbox rather than being sent.

# password hashing dominates the runtime of tests that create users
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
