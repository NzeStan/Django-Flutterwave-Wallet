"""Settings used by the test-suite and by ``manage.py`` in this repository."""
from django.core.management.utils import get_random_secret_key

SECRET_KEY = get_random_secret_key()
DEBUG = False
USE_TZ = True
TIME_ZONE = 'Africa/Lagos'
ALLOWED_HOSTS = ['testserver', 'localhost']

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'rest_framework',
    'djmoney',
    'flutterwave_wallet',
]

MIDDLEWARE = [
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
]

ROOT_URLCONF = 'tests.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [],
    'APP_DIRS': True,
    'OPTIONS': {'context_processors': [
        'django.template.context_processors.request',
        'django.contrib.auth.context_processors.auth',
        'django.contrib.messages.context_processors.messages',
    ]},
}]

DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

DEFAULT_FROM_EMAIL = 'wallet@example.com'
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': ['rest_framework.authentication.SessionAuthentication'],
}

# Deliberately NOT shaped like real keys, so secret scanners don't flag them.
FLUTTERWAVE_SECRET_KEY = 'FLWSECK_TEST-dummy-key-for-tests-X'
FLUTTERWAVE_PUBLIC_KEY = 'FLWPUBK_TEST-dummy-key-for-tests-X'
FLUTTERWAVE_SECRET_HASH = 'test-secret-hash-0123456789abcdefghijklmnop'
FLUTTERWAVE_MAX_RETRIES = 0
FLW_WALLET_ALLOWED_CURRENCIES = ['NGN', 'USD', 'KES']
FLW_WALLET_FIELD_ENCRYPTION_KEYS = ['3eM5n4b7XhzJbS1l2pQ8k9wQm0Zr6Yt4u5v6w7x8y9A=']
FLUTTERWAVE_CLIENT_ID = 'test-client-id-for-tests'
FLUTTERWAVE_CLIENT_SECRET = 'test-client-secret-for-tests'
