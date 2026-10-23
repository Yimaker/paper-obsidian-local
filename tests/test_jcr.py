import unittest,tempfile,copy,json,csv
from pathlib import Path
from paper_obsidian.core import now,digest
from paper_obsidian.eligibility import reading_classify,classify,delivery_qualification_counts,import_csv
class JCRTests(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.addCleanup(self.t.cleanup);self.f=Path(self.t.name)/'evidence.txt';self.f.write_text('Synthetic fulltext: algorithms and compiler optimization; hardware evaluated.')
  self.p=dict(id='fixture',doi='10.test/x',venue_kind='journal',venue='Fixture',issns=['0885-6125'],date='2001-01-01',publication={'status':'published'})
  self.r=dict(qualification_schema=3,name='Fixture',issns=['0885-6125'],identity_review='exact',source='https://jcr.clarivate.com/',checked_at=now(),evidence_file=str(self.f),evidence_sha256=digest(self.f.read_bytes()),reviewer_type='codex',user_reviewed=False,semantic_support='Synthetic test only',jcr_year=2025,metric_year=2025,sci_index='SCIE',quartile_type='JIF',categories=[dict(category='Methods',quartile='Q2',rank='1/2'),dict(category='Hardware',quartile='Q3',rank='2/2')],all_categories_verified=True,latest_available_basis='fixture',review_due='2099-01-01')
  self.review=dict(paper_id='fixture',doi='10.test/x',checked_at=now(),reviewer_type='codex',user_reviewed=False,basis='fulltext',complete=True,evidence_file=str(self.f),evidence_sha256=digest(self.f.read_bytes()),relations=[dict(category=k,related=True,reason='fixture method mapping',location='section 1',quote='algorithms and compiler optimization') for k in ['Methods','Hardware']])
 def gate(self,review=True):return reading_classify(self.p,dict(journals=[self.r],jcr_content_reviews=[self.review] if review else []))
 def test_pass_preserves_legacy_history_all_categories(self):
  for q in ['Q1','Q2']:
   self.r['categories'][0]['quartile']=q;g=self.gate();self.assertTrue(g['eligible']);self.assertFalse(g['legacy_eligible']);self.assertEqual(g['historical_qualification']['status'],'pending');self.assertEqual(len(g['jcr_qualification']['categories']),2);self.assertIn('2025 指标',g['reason'])
 def test_no_jci_numeric_q3_q4(self):
  for q in ['Q3','Q4']:
   self.r['categories'][0]['quartile']=q;self.assertEqual(self.gate()['jcr_qualification']['status'],'failed')
  self.r['quartile_type']='JCI';self.assertFalse(self.gate()['eligible'])
  self.r['quartile_type']='JIF';self.r['categories']=[];self.r['jif']=9;self.assertFalse(self.gate()['eligible'])
 def test_irrelevant_q2_does_not_pass(self):
  self.review['relations'][0]['related']=False;self.assertEqual(self.gate()['jcr_qualification']['status'],'failed')
 def test_fulltext_preflight_no_formal_admission(self):
  g=self.gate(False);self.assertFalse(g['eligible']);self.assertTrue(g['fulltext_verification_allowed'])
  self.review['basis']='abstract';self.assertFalse(self.gate()['eligible'])
  self.review['basis']='fulltext';self.review['relations'][0]['quote']='not in original';self.assertFalse(self.gate()['eligible'])
 def test_identity_missing_year_expiry_refresh(self):
  original=copy.deepcopy(self.r)
  for change in [dict(metric_year=None),dict(review_due='2000-01-01'),dict(source_status='refresh_failed'),dict(conflict=True),dict(all_categories_verified=False)]:
   self.r=dict(original,**change);self.assertEqual(self.gate()['jcr_qualification']['status'],'pending')
  self.r=original;self.p['issns'].append('1573-0565');g=self.gate();self.assertFalse(g['eligible']);self.assertFalse(g['fulltext_verification_allowed'])
 def test_hash_content_review_and_user_identity(self):
  self.review['user_reviewed']=True;self.assertFalse(self.gate()['eligible']);self.review['user_reviewed']=False
  self.f.write_text('changed');self.assertFalse(self.gate()['eligible'])
 def test_dedup_and_old_receipts(self):
  g=self.gate();g['admission_basis']+=['journal_current','journal_historical'];d={'qualification':g};counts=delivery_qualification_counts([(self.p,d)]*3)
  self.assertEqual(counts['total'],1);self.assertEqual(counts['jcr'],1);self.assertEqual(delivery_qualification_counts([(self.p,{})])['jcr_unrecorded'],1)
 def test_capacity_restore_uses_same_gate(self):
  from paper_obsidian.core import Store
  from paper_obsidian.capacity import evidence,restorable_formal
  from unittest.mock import patch
  root=Path(self.t.name);w=root/'venues.json';w.write_text(json.dumps(dict(journals=[self.r],jcr_content_reviews=[self.review])));s=Store(root/'data');self.addCleanup(s.db.close)
  c={'paths':{'data':root/'data','vault':root/'vault','whitelist':w},'search':{}}
  ev=evidence(self.p,c,s,{});self.assertTrue(ev['eligible'])
  with patch('paper_obsidian.daily.completed',return_value=False):self.assertFalse(restorable_formal(c,s,{'id':self.p['id'],'evidence':ev}))
 def test_import_idempotence(self):
  root=Path(self.t.name);w=root/'venues.json';w.write_text('{"journals":[]}');f=root/'input.csv';r=dict(self.r,issns='0885-6125',categories=json.dumps(self.r['categories']),user_reviewed='false',all_categories_verified='true')
  with f.open('w',newline='') as h:
   writer=csv.DictWriter(h,fieldnames=r.keys());writer.writeheader();writer.writerow(r)
  import_csv('journals',f,w);import_csv('journals',f,w);self.assertEqual(len(json.loads(w.read_text())['journals']),1)
 def test_new_version_and_conflict_fail_closed(self):
  from paper_obsidian.jcr import classify as jclass
  newer=dict(self.r,metric_year=2026,review_due='2000-01-01')
  self.assertEqual(jclass(self.p,{'journals':[self.r,newer]})['status'],'pending')
  other=copy.deepcopy(self.r);other['categories'][0]['quartile']='Q1'
  self.assertEqual(jclass(self.p,{'journals':[self.r,other]})['status'],'pending')
 def test_incomplete_relations_and_ai_not_human(self):
  self.review['relations'].pop();self.assertFalse(self.gate()['eligible'])
  self.r['user_reviewed']=True;self.assertFalse(self.gate()['eligible'])
 def test_header_separate_channels(self):
  from paper_obsidian.journals import header
  from paper_obsidian.core import Store
  s=Store(Path(self.t.name)/'data');self.addCleanup(s.db.close)
  text=header(self.p,self.gate(),s)
  self.assertIn('JCR 阅读资格：通过',text);self.assertIn('发表时资格：待核验',text);self.assertIn('JIF Q2',text);self.assertIn('Hardware',text)
