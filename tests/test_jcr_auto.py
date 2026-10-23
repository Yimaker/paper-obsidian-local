import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock
from paper_obsidian.core import Store,digest
from paper_obsidian.daily_model import DailyError
from paper_obsidian.jcr_auto import run,finalize,relations_valid,PROMPT
class AutomaticJCRTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.s=Store(self.root/'data');self.addCleanup(self.s.db.close)
  self.p={'id':'test','doi':'10.test/a','title':'Test'};self.w={'_jcr_review_root':str(self.root/'reviews')};self.folder=self.root/'paper';self.folder.mkdir()
  self.parsed={'source_sha256':'source','evidence_text':'[p001-b001]\nCore algorithm experiments','evidence_catalog':{'p001-b001':{'text':'Core algorithm experiments','physical_page':1}}}
  self.g={'fulltext_verification_allowed':True,'jcr_qualification':{'categories':[{'category':'Methods','quartile':'Q2'},{'category':'Hardware','quartile':'Q3'}],'evidence':[]}}
  self.result={'problem':'algorithm','method':'algorithm','application':'computing','material_gaps':[],'relations':[{'category':n,'decision':'related','reason':'Core contribution','quote':'Core algorithm experiments','page':1,'evidence_ids':['p001-b001']} for n in ['Methods','Hardware']]}
  self.client=Mock();self.client.c={'daily':{'max_request_attempts':2},'jcr_auto':{'enabled':True,'papers_per_day':3}};self.client.env={'MODEL':'mock'};self.client.budget=100000;self.client.call.return_value=self.result
 def run_review(self):return run(self.p,self.w,self.g,self.parsed,self.folder,self.client,self.s)
 def test_blind_all_categories(self):
  self.run_review();args=self.client.call.call_args.args;self.assertNotIn('quartile',json.dumps(args));self.assertNotIn('Q2',json.dumps(args));self.assertEqual(args[2]['categories'],['Hardware','Methods']);self.assertEqual(self.s.used('jcr_auto_papers'),1)
 def test_cache(self):
  self.run_review();self.run_review();self.assertEqual(self.client.call.call_count,1);self.assertEqual(self.s.used('jcr_auto_papers'),1)
 def test_partition_change_no_model(self):
  self.run_review();self.g['jcr_qualification']['categories'][0]['quartile']='Q3'
  with self.assertRaises(DailyError):self.run_review()
  self.assertEqual(self.client.call.call_count,1)
 def test_irrelevant_no_retry(self):
  self.result['relations'][0]['decision']='unrelated'
  for _ in range(2):
   with self.assertRaises(DailyError):self.run_review()
  self.assertEqual(self.client.call.call_count,1)
 def test_missing_fulltext(self):
  self.parsed['missing_pages']=[1]
  with self.assertRaises(DailyError):self.run_review()
  self.client.call.assert_not_called()
 def test_preflight(self):
  self.g['fulltext_verification_allowed']=False
  with self.assertRaises(DailyError):self.run_review()
  self.client.call.assert_not_called()
 def test_quota(self):
  self.client.c['jcr_auto']['papers_per_day']=0
  with self.assertRaises(DailyError):self.run_review()
  self.client.call.assert_not_called()
 def test_full_input_limit(self):
  self.client.c['jcr_auto']['max_input_bytes']=1
  with self.assertRaises(DailyError):self.run_review()
  self.client.call.assert_not_called()
 def test_manual_preserved(self):
  self.w['jcr_content_reviews']=[{'paper_id':'test'}]
  with self.assertRaises(DailyError):self.run_review()
  self.client.call.assert_not_called()
 def test_changed_fulltext_invalidates(self):
  self.run_review();self.parsed['source_sha256']='changed';self.run_review();self.assertEqual(self.client.call.call_count,2)
 def test_categories_invalidates(self):
  self.run_review();self.g['jcr_qualification']['categories'][1]['category']='Other'
  with self.assertRaises(DailyError):self.run_review()
  self.assertEqual(self.client.call.call_count,2)
 def test_semantic_rejection_not_structural_pass(self):
  record=self.run_review();checks={key:{'supported':False,'reason':'Quote does not support relation','evidence_ids':['p001-b001']} for key in ['summary','jcr_relations.0','jcr_relations.1']};self.client.call.return_value={'checks':checks,'issues':[]}
  for _ in range(2):
   with self.assertRaises(DailyError):finalize(self.p,self.w,record,self.parsed,self.folder,self.client,self.s)
  self.assertEqual(self.client.call.call_count,2);self.assertEqual(json.loads((self.root/'reviews/test.json').read_text())['status'],'rejected')
 def test_final_pass_and_cache(self):
  record=self.run_review();checks={key:{'supported':True,'reason':'Supported by core experiments','evidence_ids':['p001-b001']} for key in ['summary','jcr_relations.0','jcr_relations.1']};self.client.call.return_value={'checks':checks,'issues':[]}
  for _ in range(2):final=finalize(self.p,self.w,record,self.parsed,self.folder,self.client,self.s)
  self.assertFalse(final['user_reviewed']);self.assertEqual(final['reviewer_type'],'ai_api');self.assertEqual(self.client.call.call_count,2)
 def test_insufficient(self):
  self.result['relations'][0]['decision']='insufficient'
  with self.assertRaises(DailyError):self.run_review()
 def test_unknown_ticket_recovery_no_double_quota(self):
  self.client.call.side_effect=DailyError('unknown')
  for _ in range(2):
   with self.assertRaises(DailyError):self.run_review()
  self.assertEqual(self.s.used('jcr_auto_papers'),1)
 def test_bad_quote(self):
  self.result['relations'][0]['quote']='invented'
  with self.assertRaises(DailyError):self.run_review()
 def real_client(self,handler):
  import httpx
  from paper_obsidian.daily_model import Client
  c={'paths':{'data':self.root/'data'},'daily':{'context_tokens':100000,'output_tokens':2000,'safety_tokens':1000,'max_request_attempts':2},'jcr_auto':{'enabled':True,'papers_per_day':3}}
  self.client=Client(c,{'API_BASE_URL':'https://example.test','API_KEY':'fake','MODEL':'mock'},self.s,httpx.MockTransport(handler));self.client.run_id='isolated'
 def test_real_ledger_cache_usage_recovery(self):
  import httpx
  calls=[]
  def handler(request):
   calls.append(request);return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(self.result)}}],'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}})
  self.real_client(handler);self.run_review()
  folder=next((self.root/'data/daily-api').iterdir());(folder/'result.json').unlink()
  self.s.db.execute("DELETE FROM stages WHERE stage='jcr_auto'");self.s.db.commit()
  self.run_review();self.assertEqual(len(calls),1);receipt=json.loads((folder/'attempts.json').read_text());self.assertEqual(receipt[0]['run_id'],'isolated');self.assertEqual(receipt[0]['usage']['total_tokens'],10);self.assertEqual(self.s.used('jcr_auto_papers'),1)
 def test_real_unknown_cross_day_and_model_block(self):
  import httpx
  from unittest.mock import patch
  calls=[]
  def handler(request):calls.append(request);raise httpx.ReadTimeout('fixture')
  self.real_client(handler)
  with self.assertRaises(DailyError):self.run_review()
  self.client.temporarily_unavailable=False;self.client.env['MODEL']='other'
  with patch('paper_obsidian.core.today',return_value='2099-01-01'):
   with self.assertRaises(DailyError):self.run_review()
  self.assertEqual(len(calls),1)
 def test_final_consumer_accepts_ai_record(self):
  from test_jcr import JCRTests
  from paper_obsidian.eligibility import reading_classify
  fixture=JCRTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
  self.p.update(id='fixture',doi=fixture.p['doi']);record=self.run_review()
  self.client.call.return_value={'checks':{k:{'supported':True,'reason':'core support','evidence_ids':['p001-b001']} for k in ['summary','jcr_relations.0','jcr_relations.1']},'issues':[]}
  finalize(self.p,self.w,record,self.parsed,self.folder,self.client,self.s)
  self.w['journals']=[fixture.r];g=reading_classify(fixture.p,self.w);self.assertTrue(g['eligible']);self.assertEqual(g['admission_basis'],['journal_jcr'])
  self.w['jcr_content_reviews']=[fixture.review];g=reading_classify(fixture.p,self.w);self.assertFalse(g['eligible']);self.assertIn('冲突',g['jcr_qualification']['reason'])
