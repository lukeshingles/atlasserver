"""Django settings for atlasserver project.

For more information on this file, see
https://docs.djangoproject.com/en/6.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/6.1/ref/settings/
"""

import os
import platform
from pathlib import Path
from typing import cast

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

SECRET_KEY = os.environ.get("ATLASSERVER_DJANGO_SECRET_KEY")
if not SECRET_KEY:
    # fail at import with the variable named, rather than at first use with Django's generic
    # "SECRET_KEY must not be empty". settings_test presets a placeholder before importing this
    # module, so tests and type checking work on machines with no .env
    _msg = "Set the ATLASSERVER_DJANGO_SECRET_KEY environment variable (e.g. in .env)"
    raise ImproperlyConfigured(_msg)

TEST_USERS = [int(x) for x in os.environ.get("ATLASSERVER_TEST_USERS", "").split(",") if x]

# How many reverse proxies in front of this server append to X-Forwarded-For. Zero ignores the
# header, which is right for the current deployment: nothing sets it, so any value is whatever the
# client chose to claim. Set it only for a proxy that overwrites the header, and count the hops
# exactly -- one too far left reads the attacker's value again. Validated rather than passed to
# int(), so a typo names the variable instead of failing the whole import with a bare ValueError.
_proxycount_env = os.environ.get("ATLASSERVER_TRUSTED_PROXY_COUNT", "0").strip() or "0"
# isdecimal, not isdigit: isdigit also accepts superscripts, and "²" would pass the check and
# then fail int() with the bare ValueError this exists to replace
if not _proxycount_env.isdecimal():
    _msg = f"ATLASSERVER_TRUSTED_PROXY_COUNT must be a non-negative integer, not {_proxycount_env!r}"
    raise ImproperlyConfigured(_msg)
TRUSTED_PROXY_COUNT = int(_proxycount_env)

# See https://docs.djangoproject.com/en/6.1/howto/deployment/checklist/

# SECURITY WARNING: don't run with debug turned on in production!
# macOS means a development machine, but that is not the only kind, and sometimes a Mac needs to
# simulate production: an explicit ATLASSERVER_DEBUG wins in both directions, and only when it is
# unset does the platform decide. Without the override, a developer on Linux picks up the
# production hardening below, where SECURE_SSL_REDIRECT makes runserver answer every request with
# a 301 to a port serving no TLS.
_debug_env = os.environ.get("ATLASSERVER_DEBUG", "").strip().lower()
if _debug_env and _debug_env not in {"1", "true", "yes", "0", "false", "no"}:
    # fail loudly rather than guess: a value like "on" silently selecting production mode would
    # give a developer the SSL-redirect 301 loop with nothing pointing at the typo that caused it
    _msg = f"ATLASSERVER_DEBUG must be one of 1/true/yes or 0/false/no, not {_debug_env!r}"
    raise ImproperlyConfigured(_msg)
DEBUG = _debug_env in {"1", "true", "yes"} if _debug_env else platform.system() == "Darwin"

# Not "*": django.contrib.sites is not installed, so the password reset email builds its link from
# the Host header. Accepting any host lets an attacker send a victim a reset link pointing at a
# server they control. Override with a comma-separated ATLASSERVER_ALLOWED_HOSTS if the server is
# reached under another name.
ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get(
        "ATLASSERVER_ALLOWED_HOSTS",
        "fallingstar-data.com,.fallingstar-data.com,.qub.ac.uk,deckard,localhost,127.0.0.1,[::1]",
    ).split(",")
    if host.strip()
]

# Absolute origin for links in verification and email-change mail. Not the request's Host header:
# ALLOWED_HOSTS above holds wildcard entries, so any subdomain of qub.ac.uk or
# fallingstar-data.com passes validation, and whoever can serve one could be sent a victim's token
# by aiming the Host at their own server. Empty means "use the request", which is what development
# wants; set it in production.
_siteorigin = os.environ.get("ATLASSERVER_SITE_ORIGIN", "").strip().rstrip("/")
if _siteorigin and not _siteorigin.startswith(("http://", "https://")):
    _msg = f"ATLASSERVER_SITE_ORIGIN must start with http:// or https://, but is {_siteorigin!r}"
    raise ImproperlyConfigured(_msg)
if not _siteorigin and not DEBUG:
    # required rather than optional in production, because the fallback is the exposure: an unset
    # variable would leave the links built from the Host header and say nothing about it
    _msg = "ATLASSERVER_SITE_ORIGIN must be set when DEBUG is off (e.g. https://fallingstar-data.com)"
    raise ImproperlyConfigured(_msg)
SITE_ORIGIN = _siteorigin

ADMINS = [
    ("Luke Shingles", "luke.shingles@gmail.com"),
]  # send server error notifications to this person
MANAGERS = ADMINS


INSTALLED_APPS = [
    "atlasserver.forcephot",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.messages",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "django_filters",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "geoip2",
]

# django.middleware.common.BrokenLinkEmailsMiddleware is deliberately absent: its "Broken link on"
# reports are dominated by links to results that the retention sweep has since deleted, and by bots
# probing for URLs this site never had. Note that it mails MANAGERS from the middleware itself
# rather than through LOGGING below, so leaving it installed and filtering the log is not an option.
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

filecacheroot = Path("/files/atlasforced/django_cache")
if not filecacheroot.is_dir():
    filecacheroot = Path("/tmp/atlasforced/django_cache")


CACHES = {
    # file-based rather than locmem, because the DRF throttle counters live in the default cache
    # and each mod_wsgi process would otherwise keep its own, multiplying the effective rate limit
    "default": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": filecacheroot / "default",
    },
    "taskderived": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": filecacheroot / "taskderived",
        # generated plot data for finished tasks. It only changes when the result file does, so a
        # long timeout is fine, but not an unbounded one: entries for tasks that are never viewed
        # again would otherwise be kept forever, and any that were somehow stale could never heal
        "TIMEOUT": 60 * 60 * 24 * 30,  # 30 days
        "OPTIONS": {"MAX_ENTRIES": 5000},
    },
    "usagestats": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": filecacheroot / "usagestats",
    },
    # Throttle counters, kept out of "default" on purpose. There is one entry per (client, scope)
    # and they are not expired eagerly, so a few hundred distinct callers exceed the default
    # MAX_ENTRIES of 300 -- after which every throttled request culls a third of the directory at
    # random. Sharing that directory with the queue-recalc flag and the PDF render locks meant
    # ordinary API traffic could evict them. Reads are throttled now, so this is also the hottest
    # cache on the site: it gets its own directory rather than globbing over everything else's.
    "throttle": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": filecacheroot / "throttle",
        "OPTIONS": {"MAX_ENTRIES": 10000},
    },
}

ROOT_URLCONF = "atlasserver.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR, Path(BASE_DIR, "atlasserver", "forcephot", "templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "atlasserver.forcephot.context_processors.static_version",
                "atlasserver.forcephot.context_processors.queued_task_count",
            ],
        },
    },
]

WSGI_APPLICATION = "atlasserver.wsgi.application"

# Database
# https://docs.djangoproject.com/en/6.1/ref/settings/#databases

DATABASES = {
    # SQLite is not an alternative here: settings_test swaps the whole DATABASES entry for it, and
    # migration 0005 writes different DDL per vendor
    "default": {
        "ENGINE": "django.db.backends.mysql",
        "NAME": os.environ.get("ATLASSERVER_DJANGO_MYSQL_DBNAME"),
        "USER": os.environ.get("ATLASSERVER_DJANGO_MYSQL_USER"),
        "PASSWORD": os.environ.get("ATLASSERVER_DJANGO_MYSQL_PASSWORD"),
        # "localhost" makes libmysqlclient use the unix socket; CI overrides this with 127.0.0.1 to
        # reach its MySQL service container over TCP
        "HOST": os.environ.get("ATLASSERVER_DJANGO_MYSQL_HOST", "localhost"),
        "PORT": os.environ.get("ATLASSERVER_DJANGO_MYSQL_PORT", "3306"),
        # reuse connections instead of opening and closing one per request. The queue page polls
        # every couple of seconds per open tab, so the connection setup was a large share of the
        # work done for a typical request. Each mod_wsgi process holds one connection per worker
        # thread, so keep this shorter than the server's wait_timeout.
        "CONN_MAX_AGE": int(os.environ.get("ATLASSERVER_DB_CONN_MAX_AGE", "60")),
        # a pooled connection can have been closed by the server since it was last used; without
        # this the next request to pick it up fails instead of transparently reconnecting
        "CONN_HEALTH_CHECKS": True,
    }
}

# Password validation
# https://docs.djangoproject.com/en/6.1/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
# https://docs.djangoproject.com/en/6.1/topics/i18n/

# "en-gb", not "en-uk": en-uk is not a registered language tag, and Django silently falls back to
# plain "en" when it is used
LANGUAGE_CODE = "en-gb"

TIME_ZONE = "UTC"

USE_I18N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.1/howto/static-files/

# keyed on DEBUG, not on the platform: a Linux development machine with ATLASSERVER_DEBUG=1 would
# otherwise get /forcedphot/static/ URLs that runserver does not serve — no CSS and no bundles —
# and a Mac simulating production (ATLASSERVER_DEBUG=0) needs the prefix the real deployment has
PATHPREFIX = "" if DEBUG else "/forcedphot"
STATIC_URL = f"{PATHPREFIX}/static/"

STATIC_ROOT = Path(BASE_DIR, "static")
RESULTS_DIR = Path(STATIC_ROOT, "results")


def _static_version() -> str:
    """Return a cache-busting suffix that changes whenever a served asset does.

    Appended as ?ver= to the stylesheets and JS bundles, which are served under stable names, so a
    browser holding an old copy alongside freshly deployed markup would otherwise keep using it.
    This replaced six hand-edited date strings across two templates.

    Taken from the files rather than from the package version: a deployment here is a git pull and
    a restart, which need not reinstall the package, and setuptools_scm bakes its version in at
    install time. The mtimes move whenever a deploy actually replaces an asset, and not otherwise,
    so every worker computes the same value and a browser keeps its cached copy until it is stale.
    """
    # globs rather than a list of names: every hand-written asset served under a stable name lives
    # at one of these three levels, so adding one does not mean remembering to add it here too
    assets = [*Path(STATIC_ROOT).glob("*.css"), *Path(STATIC_ROOT, "js").glob("*.js")]
    assets += list(Path(STATIC_ROOT, "js", "vendor").glob("*.js"))

    mtimes = [path.stat().st_mtime_ns for path in assets if path.is_file()]

    # nanoseconds, not seconds: two deploys landing within the same second would otherwise share
    # a suffix and leave browsers on the earlier bundle
    # no assets found means an unbuilt checkout; a constant is fine, there is nothing to bust
    return str(max(mtimes)) if mtimes else "0"


STATIC_VERSION = _static_version()

USE_X_FORWARDED_HOST = False
USE_X_FORWARDED_PORT = False

if not DEBUG:
    # httpconf.txt sets this header unconditionally (so a client cannot spoof it). The previous
    # value keyed off SERVER_SOFTWARE, which Apache always populates with its own banner, so
    # request.scheme was a constant that depended on the ServerTokens setting rather than on the
    # protocol the request actually arrived over.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

if not DEBUG:
    # the four warnings `manage.py check --deploy` raised, guarded because development runs over
    # plain http and would never send secure-only cookies. SECURE_SSL_REDIRECT never fires today
    # (httpconf.txt always sets X-Forwarded-Proto) and is a backstop for a deployment that stops.
    # Annotated because otherwise these infer as literal types and settings_test cannot switch them.
    SECURE_SSL_REDIRECT: bool = True
    SESSION_COOKIE_SECURE: bool = True
    CSRF_COOKIE_SECURE: bool = True
    # one year, and only for this host: subdomains are not all served by this deployment
    SECURE_HSTS_SECONDS: int = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS: bool = False
    SECURE_HSTS_PRELOAD: bool = False

CSRF_TRUSTED_ORIGINS = [
    "https://*.qub.ac.uk",
    "https://fallingstar-data.com",
    "http://localhost",
    "http://127.0.0.1",
    "http://deckard:8086",
]

# When set to True, if the request URL does not match any of the patterns in the URLconf and it doesn't end in a slash,
# an HTTP redirect is issued to the same URL with a slash appended. Note that the redirect may cause any data submitted
# in a POST request to be lost.
APPEND_SLASH = True

LOGIN_URL = "login"

LOGIN_REDIRECT_URL = "task-list"

LOGOUT_REDIRECT_URL = "index"

REST_FRAMEWORK = {
    # Use Django's standard `django.contrib.auth` permissions,
    # or allow read-only access for unauthenticated users.
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.BasicAuthentication",
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.DjangoModelPermissionsOrAnonReadOnly"],
    "DEFAULT_PAGINATION_CLASS": "atlasserver.forcephot.pagination.TaskPagination",
    "PAGE_SIZE": 6,
    "DEFAULT_THROTTLE_CLASSES": [
        # 'rest_framework.throttling.ScopedRateThrottle',
        "atlasserver.forcephot.throttles.ForcedPhotRateThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "forcephottasks": "60/min",
        # Reads. Deliberately loose: the queue page polls the task list every 6 seconds, so a user
        # with several tabs open is legitimately in the tens per minute, and this only has to stop
        # someone hammering it. Applied to GET/HEAD/OPTIONS, which used to bypass the throttle
        # entirely -- see forcephot.throttles.
        "forcephotread": "600/min",
    },
    # the same knob as TRUSTED_PROXY_COUNT above, so the throttle's idea of the client address
    # cannot drift from the GeoIP lookup's. Left unset, DRF's get_ident() trusts the whole
    # client-written X-Forwarded-For header for anonymous callers — the exact forgery
    # netaddr.client_address exists to stop. With 0, it uses REMOTE_ADDR.
    "NUM_PROXIES": TRUSTED_PROXY_COUNT,
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    # JSON first: DRF picks the first renderer for an "Accept: */*" request (what a script sends
    # when it sets no Accept header), and TemplateHTMLRenderer answers those with an HTML page,
    # reducing validation errors to a bare "400 Bad Request". Browsers ask for text/html
    # explicitly, so they still get the HTML pages.
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.TemplateHTMLRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    "EXCEPTION_HANDLER": "atlasserver.forcephot.exception.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# the API was documented only by a hand-written page, which had to be kept in step with the
# serializer by hand. The generated schema is derived from the code, so it cannot drift.
SPECTACULAR_SETTINGS = {
    "TITLE": "ATLAS Forced Photometry API",
    "DESCRIPTION": (
        "Request forced photometry at any position on the sky over the full history of the ATLAS survey.\n\n"
        "Authenticate with a token from /api-token-auth/ and send it as `Authorization: Token <token>`.\n"
        "See the API guide at /apiguide/ for worked examples."
    ),
    "VERSION": "1.0.0",
    # the schema endpoint itself is not part of the API being described
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/",
}

MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.smtp.EmailBackend",
        "OPTIONS": {
            "host": "smtp.gmail.com",
            "port": 587,
            "use_tls": True,
            # admin error mail is sent synchronously from the request thread, so without a timeout
            # an unreachable mail server holds that worker (and eventually the whole pool)
            # indefinitely
            "timeout": 10,
            "username": os.environ.get("ATLASSERVER_EMAIL_HOST_USER"),
            "password": os.environ.get("ATLASSERVER_EMAIL_HOST_PASSWORD"),
        },
    },
}

SERVER_EMAIL = os.environ.get("ATLASSERVER_EMAIL_HOST_USER")
DEFAULT_FROM_EMAIL = os.environ.get("ATLASSERVER_EMAIL_HOST_USER")

GEOIP_PATH = Path(__file__).parent


LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {
            "()": "django.utils.log.RequireDebugFalse",
        },
        "require_debug_true": {
            "()": "django.utils.log.RequireDebugTrue",
        },
    },
    "formatters": {
        "timestamp": {
            "format": "{asctime} {levelname} {message}",
            "style": "{",
        },
        "django.server": {
            "()": "django.utils.log.ServerFormatter",
            "format": "[{server_time}] {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "filters": ["require_debug_true"],
            "class": "logging.StreamHandler",
        },
        "django.server": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "django.server",
        },
        "mail_admins": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "atlasserver.forcephot.misc.AdminEmailHandlerNo404",
            "include_html": True,
        },
        "file": {
            "level": "ERROR",
            "class": "logging.FileHandler",
            "filename": BASE_DIR / "djangodebug.log",
            "formatter": "timestamp",
        },
    },
    "loggers": {
        "django": {
            "handlers": ["console", "mail_admins", "file"],
            "level": "INFO",
        },
        # this project's own modules. Without an entry here they inherit a root logger that has no
        # handler either, so logging.lastResort writes them unformatted to stderr — which under
        # mod_wsgi means Apache's error log rather than the file below, and nothing at all under
        # runserver. No mail_admins: what these report is degraded service (e.g. an unusable GeoIP
        # database), which would otherwise mail on every affected request.
        "atlasserver": {
            "handlers": ["console", "file"],
            "level": "INFO",
        },
        # every bot probing with a forged Host header raises DisallowedHost, which would otherwise
        # email ADMINS per hit (same reasoning as omitting BrokenLinkEmailsMiddleware above). The
        # old mail_admins handler config hid this by forcing the SMTP backend even under tests,
        # where sending failed silently without credentials; with MAILERS the reports would reach
        # the test outbox and real inboxes alike.
        "django.security.DisallowedHost": {
            "handlers": cast("list[str]", []),
            "propagate": False,
        },
        "django.server": {
            "handlers": ["django.server"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
