"""Settings for running the test suite.

Run with:
    DJANGO_SETTINGS_MODULE=atlasserver.settings_test ./manage.py test

The production settings hardcode MySQL, so without an override `manage.py test` fails on any
machine that has no MySQL server running. Tests default to an in-memory SQLite database here; CI
sets ATLASSERVER_TEST_DB=mysql so that the suite still runs against the engine used in production.
"""

import os

# Set before the star import, because settings.py applies its production hardening under a plain
# `if not DEBUG:` at import time — assigning DEBUG afterwards would be too late. A plain
# assignment, not setdefault: an operator's exported ATLASSERVER_DEBUG=0 (or one in .env, which
# load_dotenv applies with override=True during the import below) must not put the test suite
# into hardened mode.
os.environ["ATLASSERVER_DEBUG"] = "1"

# a real key is not needed to run the tests, but settings.py refuses to import without one, and
# this module must import everywhere: mypy's django-stubs plugin loads it, including on machines
# and CI jobs that have no .env or secrets. setdefault, not assignment, so a real key from the
# environment (or from .env, which load_dotenv applies with override=True) still wins.
os.environ.setdefault("ATLASSERVER_DJANGO_SECRET_KEY", "test-secret-key-not-for-production")

from atlasserver.settings import *  # noqa: F403
from atlasserver.settings import CACHES as _PRODUCTION_CACHES

DEBUG = True

# Belt and braces for the same settings: the env var above can still lose to BASE_DIR/.env
# (load_dotenv overrides the environment), and these four break the suite outright when they leak
# in — SECURE_SSL_REDIRECT answers every test-client request with a 301, and the plain-http test
# client never sends secure-only cookies back.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False
SECURE_HSTS_SECONDS = 0

# A developer's .env may set ATLASSERVER_SITE_NOTICE (load_dotenv overrides the environment).
# Tests that need a note supply one with override_settings; every other test renders with none.
SITE_NOTICE = ""

if os.environ.get("ATLASSERVER_TEST_DB", "sqlite").lower() != "mysql":
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": ":memory:",
        }
    }

# the file-based caches are shared between runs (and with the dev server), so a stale entry from an
# earlier run whose task happened to receive the same id would leak into a test. Use locmem, which
# starts empty in every process.
# the aliases are taken from the real settings rather than listed again: a second list is one that
# can be forgotten, and an alias missing from it fails as "cache not configured" only once some
# test happens to touch it.
CACHES = {
    name: {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": name} for name in _PRODUCTION_CACHES
}

# MAILERS is deliberately not overridden here: Django's test runner already swaps every mailer's
# backend for locmem, so result emails land in django.core.mail.outbox rather than being sent.

# password hashing dominates the runtime of tests that create users
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
