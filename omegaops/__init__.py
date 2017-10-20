import json
from landingpage.models import ServiceDeployment, ServicePlan

from pymongo.mongo_client import MongoClient


def add_user(dbname, username, password):
    """
    add a user to omegaml giving readWrite access rights

    only this user will have access r/w rights to the database.
    """
    MONGO_URL = 'mongodb://{user}:{password}@localhost:27019/{dbname}'
    roles = roles = [{
        'role': 'readWrite',
        'db': dbname,
    }]
    # create the db but NEVER return this db. it will have admin rights.
    # TODO move admin db, user, password to secure settings
    client = MongoClient(MONGO_URL.format(user='admin',
                                          password='foobar',
                                          dbname='admin'))
    _admin_newdb = client[dbname]
    _admin_newdb.add_user(username, password, roles=roles)
    # we need to get the newdb from the client otherwise
    # newdb has admin rights (!)
    client_mongo_url = MONGO_URL.format(user=username,
                                        password=password,
                                        dbname=dbname)
    client = MongoClient(client_mongo_url)
    newdb = client[dbname]
    return newdb, client_mongo_url


def add_service_deployment(user, config):
    """
    add the service deployment
    """
    plan = ServicePlan.objects.get(name='omegaml')
    user.services.create(user=user,
                         offering=plan,
                         settings=json.dumps(config))
