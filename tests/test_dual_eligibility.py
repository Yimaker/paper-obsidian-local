import unittest,tempfile,copy,csv,json
from pathlib import Path
from paper_obsidian.eligibility import classify,reading_classify,import_csv,delivery_qualification_counts
from paper_obsidian.core import digest,now
class DualEligibilityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.f=Path(self.tmp.name)/'evidence.txt';self.f.write_text('Synthetic fixture only, never production')
  self.p={'id':'test','venue_kind':'journal','venue':'Test Journal','issns':['0885-6125'],'doi':'10.1/test','date':'2001-10-01','publication':{'status':'published'}}
  self.r={'qualification_schema':2,'scope':'current','name':'Test Journal','issns':['0885-6125'],'identity_review':'exact','reviewer_type':'codex','user_reviewed':False,'sci_index':'SCIE','sci_as_of':now()[:10],'cas_partition_scope':'major','cas_major_zone':2,'cas_edition':'test edition','cas_year':2025,'cas_major_category':'Computer Science','latest_available_basis':'Synthetic test','review_due':'2099-01-01'}
  for pre,url in [('sci','https://mjl.clarivate.com/'),('cas','https://www.fenqubiao.com/')]:
   self.r.update({pre+'_source':url,pre+'_checked_at':now(),pre+'_evidence_file':str(self.f),pre+'_evidence_sha256':digest(self.f.read_bytes()),pre+'_semantic_support':'Synthetic fixture supports this field'})
 def gate(self,*rows):return reading_classify(self.p,{'journals':list(rows),'conferences':[]})
 def historical(self):
  r=copy.deepcopy(self.r);r.update(scope='historical',applicability_rule='Explicit synthetic historical interval',coverage_policy_source='https://school.edu.cn/policy',coverage_policy_evidence_file=str(self.f),coverage_policy_evidence_sha256=digest(self.f.read_bytes()))
  for pre in ('sci','cas'):r.update({pre+'_coverage_from':'2001-10-01',pre+'_coverage_to':'2001-10-01'})
  return r
 def test_current_old_paper_legacy_unchanged(self):
  g=self.gate(self.r);self.assertTrue(g['eligible']);self.assertFalse(g['legacy_eligible']);self.assertEqual(g['historical_qualification']['status'],'pending');self.assertFalse(classify(self.p,{'journals':[self.r]})['eligible'])
 def test_historical_current_unknown_and_failed(self):
  h=self.historical();self.assertEqual(self.gate(h)['admission_basis'],['journal_historical']);r=dict(self.r,cas_major_zone=3);g=self.gate(h,r);self.assertEqual(g['current_qualification']['status'],'failed');self.assertEqual(g['historical_qualification']['status'],'passed');self.assertTrue(g['eligible'])
 def test_third_party_jcr_and_no_review(self):
  for patch in [{'cas_source':'https://letpub.com.cn/'},{'cas_partition_scope':'jcr'},{'cas_semantic_support':''},{'user_reviewed':True}]:
   self.assertFalse(self.gate(dict(self.r,**patch))['eligible'])
 def test_identity_version_and_date(self):
  for patch in [{'name':'Renamed Journal'},{'issns':['1234-5678']},{'cas_edition':''}]:self.assertFalse(self.gate(dict(self.r,**patch))['eligible'])
  self.p['issns'].append('1573-0565');self.assertFalse(self.gate(self.r)['eligible'])
 def test_historical_date_no_assumption(self):
  h=self.historical();h['sci_coverage_from']='2001-10-02';h['sci_coverage_to']='2001-10-03';self.assertFalse(self.gate(h)['eligible'])
  h=self.historical();h.pop('applicability_rule');self.assertFalse(self.gate(h)['eligible'])
 def test_expired_refresh_and_versions(self):
  for patch in [{'review_due':'2000-01-01'},{'source_status':'refresh_failed'}]:self.assertFalse(self.gate(dict(self.r,**patch))['eligible'])
  newer=dict(self.r,cas_year=2026,review_due='2000-01-01');self.assertFalse(self.gate(self.r,newer)['eligible']);self.assertTrue(self.gate(self.r,self.historical())['eligible'])
 def test_conflicting_and_corrupt(self):
  self.assertEqual(self.gate(self.r,dict(self.r,cas_major_zone=3))['current_qualification']['status'],'pending');self.f.write_text('changed');self.assertFalse(self.gate(self.r)['eligible'])
 def test_publication_and_doi_required(self):
  self.p['doi']='';self.assertFalse(self.gate(self.r)['eligible'])
 def test_counts_dedup_and_intersection(self):
  d={'qualification':self.gate(self.r,self.historical())};v=delivery_qualification_counts([(self.p,d)]*3);self.assertEqual((v['total'],v['current'],v['historical'],v['intersection']),(1,1,1,1))
 def test_import_idempotent_and_versions_retained(self):
  target=Path(self.tmp.name)/'venues.json';target.write_text('{"journals":[],"conferences":[]}');source=target.with_suffix('.csv');row=dict(self.r,issns='0885-6125',user_reviewed='false')
  def write(row):
   with source.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(row));w.writeheader();w.writerow(row)
  write(row);import_csv('journals',source,target);import_csv('journals',source,target);self.assertEqual(len(json.loads(target.read_text())['journals']),1)
  row['cas_year']=2024;write(row);import_csv('journals',source,target);self.assertEqual(len(json.loads(target.read_text())['journals']),2)
 def test_shared_capacity_and_admission_adapter(self):
  from paper_obsidian.core import Store
  from paper_obsidian.capacity import evidence,restorable_formal
  from unittest.mock import patch
  root=Path(self.tmp.name);w=root/'venues.json';w.write_text(json.dumps({'journals':[self.r],'conferences':[]}));c={'paths':{'data':root/'data','vault':root/'vault','whitelist':w},'search':{}}
  s=Store(c['paths']['data']);self.addCleanup(s.db.close)
  for branch in ('ordinary','cross'):
   s.set_state('branch:'+self.p['id'],{'primary':branch});ev=evidence(self.p,c,s,{})
   self.assertTrue(ev['eligible']);self.assertTrue(self.gate(self.r)['eligible'])
   with patch('paper_obsidian.daily.completed',return_value=False):self.assertFalse(restorable_formal(c,s,{'id':self.p['id'],'evidence':ev}))
 def test_current_header_does_not_assert_history(self):
  from paper_obsidian.core import Store
  from paper_obsidian.journals import header
  s=Store(Path(self.tmp.name)/'data');self.addCleanup(s.db.close)
  text=header(self.p,self.gate(self.r),s)
  self.assertIn('当前资格：通过',text);self.assertIn('发表时资格：待核验',text);self.assertIn('test edition',text);self.assertIn('按当前期刊资格推荐',text)
 def test_malformed_latest_version_fails_closed(self):
  self.assertFalse(self.gate(self.r,dict(self.r,cas_year='unknown'))['eligible'])
