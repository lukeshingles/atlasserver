DEBUG = True

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
        # 'ENGINE': 'django.db.backends.mysql',
        # 'OPTIONS': {
        #     # 'read_default_file': '/usr/local/etc/my.cnf',
        # },
        # 'NAME': os.environ.get('ATLASSERVER_DJANGO_MYSQL_DBNAME'),
        # 'USER': os.environ.get('ATLASSERVER_DJANGO_MYSQL_USER'),
        # 'PASSWORD': os.environ.get('ATLASSERVER_DJANGO_MYSQL_PASSWORD'),
        # 'HOST': 'localhost',   # Or an IP Address that your DB is hosted on
        # 'PORT': '3306',
    }
}
