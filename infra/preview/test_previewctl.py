import json
from pathlib import Path
import unittest
from unittest.mock import patch
import previewctl

class ConfigurationTests(unittest.TestCase):
    def controller(self):
        c = previewctl.Controller.__new__(previewctl.Controller)
        c.config = {'auth0_domain': 'preview.auth0.com', 'environment_command': ['source', '{application}']}
        return c

    def state(self, branch='a'):
        return {'web_client': {'client_id': branch+'-web', 'client_secret': branch+'-web-secret'},
                'rpc_client': {'client_id': branch+'-rpc', 'client_secret': branch+'-rpc-secret'},
                'origin': 'https://'+branch+'.example.com', 'rpc_origin': 'https://'+branch+'-api.example.com',
                'db_runtime': {'database_url': branch+'-runtime'}, 'db_migrator': {'database_url': 'never-runtime'},
                'auth_secret': branch+'-session', 'signing_key': branch+'-signing'}

    def test_source_is_replaceable_and_credentials_stay_per_preview(self):
        def source(args, **kwargs):
            values = {'AUTH0_CLIENT_ID': '${'+args[1].upper()+'_CLIENT_ID}'}
            if args[1]=='rpc': values['DATABASE_URL']='${DATABASE_URL}'
            return json.dumps(values)
        with patch.object(previewctl, 'run', side_effect=source):
            a=self.controller().configuration(self.state('a')); b=self.controller().configuration(self.state('b'))
        self.assertNotEqual(a['DATABASE_URL'], b['DATABASE_URL'])
        self.assertEqual(a['WEB_AUTH0_CLIENT_ID'], 'a-web')
        self.assertEqual(a['RPC_AUTH0_CLIENT_ID'], 'a-rpc')
        self.assertNotIn('never-runtime', a.values())

    def test_conflicting_shared_values_fail_instead_of_silently_overwriting(self):
        with patch.object(previewctl, 'run', side_effect=['{"AUTH0_ISSUER":"one"}', '{"AUTH0_ISSUER":"two"}']):
            with self.assertRaisesRegex(ValueError, 'Conflicting shared'):
                self.controller().configuration(self.state())

    def test_main_cannot_be_deleted(self):
        with self.assertRaises(ValueError): self.controller().destroy('main')

    def test_unknown_template_value_fails(self):
        with patch.object(previewctl, 'run', return_value='{"DATABASE_URL":"${UNKNOWN}"}'):
            with self.assertRaises(KeyError): self.controller().configuration(self.state())

if __name__ == '__main__': unittest.main()
