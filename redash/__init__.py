import os
import sys
import logging
import urlparse
import urllib
import redis
import time
import requests

from flask import Flask, safe_join, request, redirect
from flask_sslify import SSLify
from werkzeug.contrib.fixers import ProxyFix
from werkzeug.routing import BaseConverter, ValidationError
from statsd import StatsClient
from flask_mail import Mail
from flask_limiter import Limiter
from flask_limiter.util import get_ipaddr
from flask_migrate import Migrate

from redash import settings
from redash.query_runner import import_query_runners
from redash.destinations import import_destinations


__version__ = '4.0.1'


def setup_logging():
    handler = logging.StreamHandler(sys.stdout if settings.LOG_STDOUT else sys.stderr)
    formatter = logging.Formatter(settings.LOG_FORMAT)
    handler.setFormatter(formatter)
    logging.getLogger().addHandler(handler)
    logging.getLogger().setLevel(settings.LOG_LEVEL)

    # Make noisy libraries less noisy
    if settings.LOG_LEVEL != "DEBUG":
        logging.getLogger("passlib").setLevel("ERROR")
        logging.getLogger("requests.packages.urllib3").setLevel("ERROR")
        logging.getLogger("snowflake.connector").setLevel("ERROR")
        logging.getLogger('apiclient').setLevel("ERROR")


def create_redis_connection():
    logging.debug("Creating Redis connection (%s)", settings.REDIS_URL)
    redis_url = urlparse.urlparse(settings.REDIS_URL)

    if redis_url.scheme == 'redis+socket':
        qs = urlparse.parse_qs(redis_url.query)
        if 'virtual_host' in qs:
            db = qs['virtual_host'][0]
        else:
            db = 0

        r = redis.StrictRedis(unix_socket_path=redis_url.path, db=db)
    else:
        if redis_url.path:
            redis_db = redis_url.path[1]
        else:
            redis_db = 0
        # Redis passwords might be quoted with special characters
        redis_password = redis_url.password and urllib.unquote(redis_url.password)
        r = redis.StrictRedis(host=redis_url.hostname, port=redis_url.port, db=redis_db, password=redis_password)

    return r


setup_logging()
redis_connection = create_redis_connection()
mail = Mail()
migrate = Migrate()
mail.init_mail(settings.all_settings())
statsd_client = StatsClient(host=settings.STATSD_HOST, port=settings.STATSD_PORT, prefix=settings.STATSD_PREFIX)
limiter = Limiter(key_func=get_ipaddr, storage_uri=settings.LIMITER_STORAGE)

import_query_runners(settings.QUERY_RUNNERS)
import_destinations(settings.DESTINATIONS)

from redash.version_check import reset_new_version_status
reset_new_version_status()


class SlugConverter(BaseConverter):
    def to_python(self, value):
        # This is ay workaround for when we enable multi-org and some files are being called by the index rule:
        # for path in settings.STATIC_ASSETS_PATHS:
        #     full_path = safe_join(path, value)
        #     if os.path.isfile(full_path):
        #         raise ValidationError()

        return value

    def to_url(self, value):
        return value


def create_app(load_admin=True):
    from jose import jwt
    from redash import extensions, handlers
    from redash.handlers.webpack import configure_webpack
    from redash.admin import init_admin
    from redash.models import db
    from redash.authentication import setup_authentication, get_jwt_public_key
    from redash.metrics.request import provision_app
    from jose import jwt

    os.environ['SCRIPT_NAME'] = settings.ROOT_UI_URL

    if settings.REMOTE_JWT_LOGIN_ENABLED:
        class JwtFlask(Flask):
            def process_response(self, response, *args, **kwargs):
                jwttoken = request.cookies.get('jwt', None)

                if jwttoken is not None:
                    try:
                        public_key = get_jwt_public_key()
                        jwt_decoded = jwt.get_unverified_claims(jwttoken) if public_key is '' else jwt.decode(jwttoken, public_key)
                        iat = jwt_decoded['iat']
                        exp = jwt_decoded['exp']
                        now = time.time()

                        if iat + 1200 < now <= exp:
                            email = jwt_decoded.get('email', None)
                            resp = requests.post(settings.REMOTE_JWT_REFRESH_PROVIDER, headers={ 'Authorization' : 'Bearer ' + jwttoken }, data={ 'email': email })
                            if resp.status_code < 300 and resp.data.get('jwt', None) is not None:
                                response.set_cookie('jwt', resp.data['jwt'], secure=True, httponly=True)
                            elif resp.status_code == 401:
                                raise jwt.JWTError('The authentication refresh service has denied a refresh, a login is likely in order.')
                        elif now > exp:
                            raise jwt.JWTClaimsError('The asserted expiration claim has passed.')
                    except (jwt.ExpiredSignatureError, jwt.JWTClaimsError, jwt.JWTError) as e:
                        return redirect(settings.REMOTE_JWT_EXPIRED_ENDPOINT + urllib.quote_plus(request.referrer))
                return super(JwtFlask, self).process_response(response, *args, **kwargs)
        
        app = JwtFlask(__name__,
                template_folder=settings.STATIC_ASSETS_PATH,
                static_folder=settings.STATIC_ASSETS_PATH,
                static_url_path=settings.ROOT_UI_URL + '/static')
    else:
        app = Flask(__name__,
                template_folder=settings.STATIC_ASSETS_PATH,
                static_folder=settings.STATIC_ASSETS_PATH,
                static_url_path=settings.ROOT_UI_URL + '/static')

    # Make sure we get the right referral address even behind proxies like nginx.
    app.wsgi_app = ProxyFix(app.wsgi_app, settings.PROXIES_COUNT)
    app.url_map.converters['org_slug'] = SlugConverter
    app.config["APPLICATION_ROOT"] = settings.ROOT_UI_URL
    app.config['TEMPLATES_AUTO_RELOAD'] = True

    if settings.ENFORCE_HTTPS:
        SSLify(app, skips=['ping'])

    if settings.SENTRY_DSN:
        from raven import Client
        from raven.contrib.flask import Sentry
        from raven.handlers.logging import SentryHandler

        client = Client(settings.SENTRY_DSN, release=__version__, install_logging_hook=False)
        sentry = Sentry(app, client=client)
        sentry.client.release = __version__

        sentry_handler = SentryHandler(client=client)
        sentry_handler.setLevel(logging.ERROR)
        logging.getLogger().addHandler(sentry_handler)

    # configure our database
    app.config['SQLALCHEMY_DATABASE_URI'] = settings.SQLALCHEMY_DATABASE_URI
    app.config.update(settings.all_settings())

    provision_app(app)
    db.init_app(app)
    migrate.init_app(app, db)
    if load_admin:
        init_admin(app)
    mail.init_app(app)
    setup_authentication(app)
    limiter.init_app(app)
    handlers.init_app(app)
    configure_webpack(app)
    extensions.init_extensions(app)
    return app
