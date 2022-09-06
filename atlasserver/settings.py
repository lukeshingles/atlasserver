"""
Django settings for atlasserver project.

Generated by 'django-admin startproject' using Django 3.1.1.

For more information on this file, see
https://docs.djangoproject.com/en/3.1/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/3.1/ref/settings/
"""
import os
import platform
from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)

SECRET_KEY = os.environ.get("ATLASSERVER_DJANGO_SECRET_KEY")

# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/3.1/howto/deployment/checklist/

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = platform.system() == "Darwin"

ALLOWED_HOSTS = ["*"]

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
    "geoip2_extras",
]

MIDDLEWARE = [
    "django.middleware.common.BrokenLinkEmailsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "geoip2_extras.middleware.GeoIP2Middleware",
    "atlasserver.forcephot.countryrestriction.CountryRestrictionMiddleware",
]

filecacheroot = Path("/lvm/hdd1/files/atlasforced/django_cache")
if not filecacheroot.is_dir():
    filecacheroot = Path("/tmp/atlasforced/django_cache")


CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        # "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        # "LOCATION": "/tmp/atlasforced/django_cache",
    },
    # required in order for IP address location lookups to be cached
    "geoip2-extras": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    },
    "taskderived": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": filecacheroot / "taskderived",
    },
    "usagestats": {
        "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
        "LOCATION": filecacheroot / "usagestats",
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
            ],
        },
    },
]

WSGI_APPLICATION = "atlasserver.wsgi.application"

# Database
# https://docs.djangoproject.com/en/3.1/ref/settings/#databases

DATABASES = {
    "default": {
        # 'ENGINE': 'django.db.backends.sqlite3',
        # 'NAME': BASE_DIR / 'db.sqlite3',
        "ENGINE": "django.db.backends.mysql",
        # 'OPTIONS': {
        #     # 'read_default_file': '/usr/local/etc/my.cnf',
        # },
        "NAME": os.environ.get("ATLASSERVER_DJANGO_MYSQL_DBNAME"),
        "USER": os.environ.get("ATLASSERVER_DJANGO_MYSQL_USER"),
        "PASSWORD": os.environ.get("ATLASSERVER_DJANGO_MYSQL_PASSWORD"),
        "HOST": "localhost",  # Or an IP Address that your DB is hosted on
        "PORT": "3306",
    }
}

# Password validation
# https://docs.djangoproject.com/en/3.1/ref/settings/#auth-password-validators

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
# https://docs.djangoproject.com/en/3.1/topics/i18n/

LANGUAGE_CODE = "en-uk"

TIME_ZONE = "UTC"

USE_I18N = True

USE_L10N = True

USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/3.1/howto/static-files/

PATHPREFIX = "/forcedphot" if platform.system() != "Darwin" else ""
STATIC_URL = PATHPREFIX + "/static/"

STATIC_ROOT = Path(BASE_DIR, "static")
RESULTS_DIR = Path(STATIC_ROOT, "results")

USE_X_FORWARDED_HOST = False
USE_X_FORWARDED_PORT = False

# If your Django app is behind a proxy that sets a header to specify secure
# connections, AND that proxy ensures that user-submitted headers with the
# same name are ignored (so that people can't spoof it), set this value to
# a tuple of (header_name, header_value). For any requests that come in with
# that header/value, request.is_secure() will return True.
# WARNING! Only set this if you fully understand what you're doing. Otherwise,
# you may be opening yourself up to a security risk.
# SECURE_PROXY_SSL_HEADER = ('X-FORWARDED-PROTO', 'https')
if platform.system() != "Darwin":
    SECURE_PROXY_SSL_HEADER = ("SERVER_SOFTWARE", "Apache")

CSRF_TRUSTED_ORIGINS = ["https://*.qub.ac.uk", "https://fallingstar-data.com", "http://localhost", "http://127.0.0.1"]

# When set to True, if the request URL does not match any of the patterns in the URLconf and it doesn’t end in a slash,
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
    },
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_RENDERER_CLASSES": (
        "rest_framework.renderers.TemplateHTMLRenderer",
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ),
    "EXCEPTION_HANDLER": "atlasserver.forcephot.exception.custom_exception_handler",
}

EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = "smtp.gmail.com"
EMAIL_USE_TLS = True
EMAIL_PORT = 587

EMAIL_HOST_USER = os.environ.get("ATLASSERVER_EMAIL_HOST_USER")
EMAIL_HOST_PASSWORD = os.environ.get("ATLASSERVER_EMAIL_HOST_PASSWORD")
SERVER_EMAIL = os.environ.get("ATLASSERVER_EMAIL_HOST_USER")
DEFAULT_FROM_EMAIL = os.environ.get("ATLASSERVER_EMAIL_HOST_USER")

GEOIP_PATH = os.path.dirname(__file__)

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
            "class": "django.utils.log.AdminEmailHandler",
            "email_backend": EMAIL_BACKEND,
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
        "django.server": {
            "handlers": ["django.server"],
            "level": "INFO",
            "propagate": False,
        },
    },
}
