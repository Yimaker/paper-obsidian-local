import copy,json,tempfile,unittest,subprocess,sys
from pathlib import Path
from unittest.mock import patch
import httpx
from paper_obsidian.core import Store,load_config,run_lock,digest
from paper_obsidian.daily_model import Client,DailyError
from paper_obsidian.daily import search,run_daily
from tools.translate_daily import DailyMeter
from tools.translate_one import LimitError
from types import SimpleNamespace

class ReliabilityTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.root=Path(self.t.name)
  self.c=load_config("tests/fixtures/config.toml");self.c['paths']['data']=self.root;self.c['search']['sources']=['arxiv'];self.c['limits']['candidates_per_day']=50
  self.s=Store(self.root);self.addCleanup(self.s.db.close)
  self.env={'API_BASE_URL':'https://example.test','API_KEY':'test','MODEL':'test'}
 def client(self,fn):return Client(self.c,self.env,self.s,httpx.MockTransport(fn))
 def timeout(self,r):raise httpx.ReadTimeout('test')
 def response(self,r):return httpx.Response(200,json={'id':'x','choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],'usage':{'total_tokens':5}})
 def test_unknown_cross_day(self):
  with self.assertRaises(DailyError):self.client(self.timeout).call('draft','system',{})
  p=next((self.root/'daily-api').glob('*/attempts.json'));rows=json.loads(p.read_text());rows[-1]['started']='2020-01-01';p.write_text(json.dumps(rows))
  with self.assertRaises(DailyError):self.client(lambda r:self.fail('charged twice')).call('draft','system',{})
 def test_pending_before_send_blocks(self):
  with patch('paper_obsidian.daily_model.wall_deadline',side_effect=SystemExit):
   with self.assertRaises(SystemExit):self.client(self.response).call('draft','system',{})
  with self.assertRaises(DailyError):self.client(lambda r:self.fail()).call('draft','system',{})
 def test_saved_response_recovers_without_double_usage(self):
  self.client(self.response).call('draft','system',{})
  p=next((self.root/'daily-api').glob('*/result.json'));p.unlink()
  c=self.client(lambda r:self.fail());self.assertTrue(c.call('draft','system',{})['ok']);self.assertEqual(c.network,0)
  rows=json.loads((p.parent/'attempts.json').read_text());self.assertEqual(len(rows),1);self.assertEqual(rows[0]['usage']['total_tokens'],5)
 def test_authorization_single_use(self):
  with self.assertRaises(DailyError):self.client(self.timeout).call('draft','system',{})
  p=next((self.root/'daily-api').glob('*/attempts.json'));row=json.loads(p.read_text())[-1]
  (p.parent/'authorize-once.json').write_text(json.dumps({'id':'fixture','request_id':row['request_id'],'authorized_by':'test user','reason':'fixture','duplicate_charge_risk_accepted':True}))
  with self.assertRaises(DailyError):self.client(self.timeout).call('draft','system',{})
  with self.assertRaises(DailyError):self.client(lambda r:self.fail()).call('draft','system',{})
  self.assertTrue((p.parent/'authorization-fixture.json').exists());self.assertEqual(len(json.loads(p.read_text())),2)
 def test_translation_unknown_not_changed_next_day(self):
  cfg={'max_output_tokens':10,'thinking':'disabled','max_attempts_per_request':2}
  env={'TRANSLATION_API_BASE_URL':'https://example.test'}
  m=DailyMeter(cfg,env,self.root)
  def timeout(**kw):raise TimeoutError()
  c=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=timeout)))
  m.wrap(c)
  with self.assertRaises(TimeoutError):c.chat.completions.create(messages=[])
  m.rows[0]['started']='2020-01-01';m.save()
  m2=DailyMeter(cfg,env,self.root);self.assertEqual(m2.rows[0]['status'],'uncertain')
  c.chat.completions.create=lambda **kw:self.fail('translation replay');m2.wrap(c)
  with self.assertRaises(LimitError):c.chat.completions.create(messages=[])
 def test_pause_persists(self):
  (self.root/'STOP').touch();self.assertEqual(run_daily(self.c,{},fetch=False)['status'],'paused');self.assertTrue((self.root/'STOP').exists())
 def do_search(self,fn):
  report={'sources':{}}
  with patch('paper_obsidian.daily.discover',side_effect=fn):search(self.c,{},self.s,None,{},report)
  return report['sources']['arxiv']
 def test_page_budgets(self):
  for budget in (1,2):
   self.c['search']['max_pages']=budget;calls=[]
   def discover(*args):calls.append(args[1]['search']['_page_start']);return [],True
   r=self.do_search(discover);self.assertEqual(len(calls),budget);self.assertEqual(r['pages'],budget)
 def test_failure_does_not_advance(self):
  self.c['search']['max_pages']=1
  def fail(*args):raise TimeoutError()
  r=self.do_search(fail);self.assertEqual(r['status'],'failed');self.assertTrue(r['truncated'])
  calls=[]
  self.do_search(lambda *a:(calls.append(a[1]['search']['_page_start']) or [],True));self.assertEqual(calls,[0])
 def test_quota_keeps_page_size_and_pending(self):
  self.c['limits']['candidates_per_day']=1;self.c['search']['max_pages']=2;self.c['search']['per_source']=12
  def discover(*a):
   self.assertEqual(a[1]['search']['per_source'],12)
   return [{'title':'irrelevant'},{'title':'irrelevant'}],True
  r=self.do_search(discover);self.assertEqual(r['returned'],1);self.assertEqual(r['stop_reason'],'daily_record_limit')
  states=[json.loads(x[0]) for x in self.s.db.execute("select value from state where key like '%:incremental'")];self.assertEqual(len(states[0]['batch']),1);self.assertEqual(states[0]['resume']['page'],0)
 def test_403_cooldown(self):
  self.c['search']['sources']=['arxiv']
  def denied(*a):raise RuntimeError('HTTP 403')
  self.assertEqual(self.do_search(denied)['stop_reason'],'source_unavailable_403')
  self.assertEqual(self.do_search(lambda *a:self.fail())['stop_reason'],'source_unavailable_cooldown')
 def test_lock_release_after_abrupt_exit(self):
  code="from pathlib import Path;from paper_obsidian.core import run_lock;import os;\nwith run_lock(Path("+repr(str(self.root))+")): os._exit(9)"
  self.assertEqual(subprocess.run([sys.executable,'-c',code]).returncode,9)
  with run_lock(self.root):
   with self.assertRaises(RuntimeError):
    with run_lock(self.root):pass
  with run_lock(self.root):pass
 def test_prepared_not_sent_can_retry(self):
  with patch.object(self.s,'spend',side_effect=SystemExit):
   with self.assertRaises(SystemExit):self.client(self.response).call('draft','system',{})
  p=next((self.root/'daily-api').glob('*/attempts.json'))
  self.assertEqual(json.loads(p.read_text())[-1]['status'],'prepared')
  self.assertTrue(self.client(self.response).call('draft','system',{})['ok'])
 def test_query_change_is_explicit_and_restarts(self):
  self.c['search']['max_pages']=1
  self.do_search(lambda *a:([],True))
  previous=self.s.get_state('active-query:arxiv')
  self.c['search']['keywords']=['new query'];calls=[]
  self.do_search(lambda *a:(calls.append(a[1]['search']['_page_start']) or [],True))
  self.assertEqual(calls,[0]);self.assertNotEqual(previous,self.s.get_state('active-query:arxiv'))
  self.assertTrue(self.s.db.execute("select count(*) from state where key like 'query-change:%'").fetchone()[0]>=2)
 def test_translation_fallback_does_not_bypass_unknown(self):
  cfg={'max_output_tokens':10,'thinking':'disabled','max_attempts_per_request':2}
  m=DailyMeter(cfg,{'TRANSLATION_API_BASE_URL':'https://example.test'},self.root)
  def timeout(**kw):raise TimeoutError()
  c=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=timeout)));m.wrap(c)
  with self.assertRaises(TimeoutError):c.chat.completions.create(messages=['first'])
  with self.assertRaises(LimitError):c.chat.completions.create(messages=['fallback'])
  self.assertEqual(m.network,1)
if __name__=='__main__':unittest.main()
