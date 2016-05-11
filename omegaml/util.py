import os
import urlparse

from mongoengine.connection import connect
__settings = None


def is_dataframe(obj):
    try:
        import pandas as pd
        return isinstance(obj, pd.DataFrame)
    except:
        return False


def is_estimator(obj):
    try:
        from sklearn.base import BaseEstimator
        from sklearn.pipeline import Pipeline
        return isinstance(obj, (BaseEstimator, Pipeline))
    except:
        False


def is_ndarray(obj):
    try:
        import numpy as np
        return isinstance(obj, np.ndarray)
    except:
        False


def settings():
    """ wrapper to get omega settings from either django or omegamldefaults """
    global __settings
    if __settings is not None:
        return __settings
    try:
        # see if we're running as a django app
        # DEBUG will probably always be as a djangon setting configuraiton
        from django.conf import settings as djsettings  # @UnresolvedImport
        defaults = djsettings
    except:
        import omegaml.defaults as omdefaults
        defaults = omdefaults
    __settings = defaults
    return __settings


def override_settings(**kwargs):
    """ test support """
    cfgvars = settings()
    for k, v in kwargs.iteritems():
        setattr(cfgvars, k, v)
    # -- OMEGA_CELERY_CONFIG updates
    celery_config = getattr(cfgvars, 'OMEGA_CELERY_CONFIG', {})
    for k in [k for k in kwargs.keys() if k.startswith('OMEGA_CELERY')]:
        celery_k = k.replace('OMEGA_', '')
        celery_config[celery_k] = kwargs[k]
    setattr(cfgvars, 'OMEGA_CELERY_CONFIG', celery_config)


def delete_database():
    """ test support """
    host = settings().OMEGA_MONGO_URL
    client = connect('omega', host=host)
    parsed_url = urlparse.urlparse(host)
    database_name = parsed_url.path[1:]
    client.drop_database(database_name)


def make_tuple(arg):
    if not isinstance(arg, (list, tuple)):
        arg = (arg,)
    return tuple(arg)


def make_list(arg):
    if not isinstance(arg, (list)):
        arg = list(arg)
    return arg


def flatten_columns(col, sep='_'):
    # source http://stackoverflow.com/a/29437514
    if not type(col) is tuple:
        return col
    else:
        new_col = ''
        for leveli, level in enumerate(col):
            if not level == '':
                if not leveli == 0:
                    new_col += sep
                new_col += level
        return new_col
