from tastypie.api import Api

from omegaweb.resources import DatasetResource, ModelResource
from omegaweb.resources.clientconfig import ClientConfigResource
v1_api = Api('v1')
v1_api.register(DatasetResource())
v1_api.register(ModelResource())
v1_api.register(ClientConfigResource())
