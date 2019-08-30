from unittest import TestCase

import six
from mock import patch
from omegacommon.userconf import get_omega_from_apikey
from omegaml.defaults import update_from_config, update_from_obj, update_from_dict


class BareObj(object):
    pass


class ConfigurationTests(TestCase):
    def setUp(self):
        self.defaults = {
            'OMEGA_MONGO_URL': 'foo'
        }

    def test_update_from_configfile(self):
        """
        Test updates from config file works
        """
        data = """
        OMEGA_MONGO_URL: updated-foo
        NOTOMEGA_VALUE: some other value
        """
        cfgfile = six.StringIO(data)
        cfgfile.seek(0)
        update_from_config(self.defaults, cfgfile)
        self.assertIn('OMEGA_MONGO_URL', self.defaults)
        self.assertEqual(self.defaults['OMEGA_MONGO_URL'], 'updated-foo')
        self.assertNotIn('NOTOMEGA_VALUE', self.defaults)

    def test_update_from_obj(self):
        data = BareObj()
        data.OMEGA_MONGO_URL = 'updated-foo'
        update_from_obj(data, self.defaults)
        self.assertIn('OMEGA_MONGO_URL', self.defaults)
        self.assertEqual(self.defaults['OMEGA_MONGO_URL'], 'updated-foo')
        self.assertNotIn('NOTOMEGA_VALUE', self.defaults)

    def test_update_from_dict(self):
        data = {
            'OMEGA_MONGO_URL': 'updated-foo'
        }
        update_from_dict(data, self.defaults)
        self.assertIn('OMEGA_MONGO_URL', self.defaults)
        self.assertEqual(self.defaults['OMEGA_MONGO_URL'], 'updated-foo')
        self.assertNotIn('NOTOMEGA_VALUE', self.defaults)

    def test_config_from_apikey(self):
        """
        Test an Omega instance can be created from user specific configs
        """
        import omegaml as om
        from omegaml import settings
        # check we get default without patching
        defaults = settings()
        # ensure the default omegaml
        om.initialized = False
        om.setup()
        with patch.object(defaults, 'OMEGA_MONGO_URL') as mock:
            defaults.OMEGA_MONGO_URL = 'foo'
            omx = om.setup()
            self.assertEqual(omx.datasets.mongo_url, 'foo')
        # reset om.datasets to restored defaults
        self.assertNotEqual(om.datasets.mongo_url, 'foo')
        # now test we can change the default through config
        # we patch the actual api call to avoid having to set up the user db
        # the objective here is to test get_omega_from_apikey
        with patch('omegacommon.userconf.get_user_config_from_api') as mock:
            mock.return_value = {
                'objects': [
                   {
                        'data': {
                            'OMEGA_MONGO_URL': 'updated-foo'
                        }
                    }
                ]
            }
            omx = get_omega_from_apikey('foo', 'bar')
            self.assertEqual(omx.datasets.mongo_url, 'updated-foo')
        self.assertNotEqual(om.datasets.mongo_url, 'updated-foo')

