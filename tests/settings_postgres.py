"""PostgreSQL test settings (CI; enables the race-condition tests).

    pytest --ds=tests.settings_postgres

Connection details come from the standard PG* environment variables.
"""
import os

from tests.settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('PGDATABASE', 'postgres'),
        'USER': os.environ.get('PGUSER', 'postgres'),
        'PASSWORD': os.environ.get('PGPASSWORD', ''),
        'HOST': os.environ.get('PGHOST', '127.0.0.1'),
        'PORT': os.environ.get('PGPORT', '5432'),
    }
}
