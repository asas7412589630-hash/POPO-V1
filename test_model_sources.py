import os, tempfile, json, unittest
from unittest.mock import patch
import model_sources as ms

class SourcesTest(unittest.TestCase):
 def setUp(self):
  self.d=tempfile.mkdtemp(); self.old=(ms.REGISTRY,ms.SECRETS,ms.CATALOG,ms.ROUTES)
  ms.REGISTRY=self.d+'/registry.json';ms.SECRETS=self.d+'/secrets.json';ms.CATALOG=self.d+'/aliases.json';ms.ROUTES=self.d+'/routes.json'
 def tearDown(self):
  ms.REGISTRY,ms.SECRETS,ms.CATALOG,ms.ROUTES=self.old
 def test_secret_hidden_and_0600(self):
  row=ms.save_source({'id':'s','name':'Source','base_url':'https://x.example/v1'},'topsecret')
  self.assertTrue(row['configured']);self.assertNotIn('topsecret',json.dumps(row))
  self.assertEqual(os.stat(ms.SECRETS).st_mode & 0o777,0o600)
 def test_publish_is_namespaced_and_routes(self):
  ms.save_source({'id':'s','name':'Source','base_url':'https://x.example/v1'},'secret')
  with patch.object(ms,'discover',return_value=['m1','m2']):
   # publish() returns per-model detail now; assert the fields that matter
   # instead of the whole dict, which would break on every added field.
   out = ms.publish('s',['m1'],{'m1':'native_gpt'})
   self.assertTrue(out['ok'])
   self.assertEqual(out['published'], ['m1'])
   self.assertEqual(out['published_count'], 1)
   self.assertEqual(ms.route_for('s/m1')['model_id'],'m1')
   # A bare alias IS created, so a client can say model="m1" without the source
   # prefix. It must resolve to the same model.
   self.assertEqual(ms.route_for('m1')['model_id'], 'm1')
   self.assertEqual(ms.models('s')[0]['alias'],'s/m1')
 def test_unknown_profile_rejected(self):
  ms.save_source({'id':'s','name':'Source','base_url':'https://x.example/v1'},'secret')
  with patch.object(ms,'discover',return_value=['m']):
   with self.assertRaises(ValueError):ms.publish('s',['m'],{'m':'bogus'})

if __name__=='__main__':unittest.main()
