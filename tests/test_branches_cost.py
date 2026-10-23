import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import test_capacity as capacity_fixtures
from paper_obsidian import branches
from paper_obsidian.capacity import apply, preview, archived, inventory
from paper_obsidian.library import reading_path, note_for_id
from paper_obsidian.sources import relevance
from paper_obsidian.api_accounting import estimate, day_estimate
from paper_obsidian.core import now, today
from paper_obsidian.vault import managed_write


class BranchCostTests(unittest.TestCase):
    def setUp(self):
        capacity_fixtures.CapacityTests.setUp(self)
        self.c['cross']={'enabled':True,'new_since':'2026-01-01T00:00:00+08:00','keywords':['machine learning'],'exclude':[],'complete_per_day':1}

    def paper(self,i,pin=False):
        pid=self.s.ingest(dict(source='TEST',aliases=['fixture:'+str(i)],title='machine learning medical '+str(i)),10000)
        p=self.s.get(pid);branches.assign(self.s,p,self.c)
        path=reading_path(p,self.c,self.s,True)
        managed_write(path,dict(paper_id=pid,capacity_pin=pin),'user content',self.c)
        pdf=path.parent/'中英对照.pdf';pdf.write_bytes(b'original')
        self.s.record(pid,'delivery','v1','success',dict(note=str(path),bilingual=str(pdf),folder=path.parent.relative_to(self.c['paths']['vault']).as_posix(),completed_at=now(),delivery_kind='新论文'))
        return p,path

    def test_50_to_51_archive_preserves_paths_and_stable_identity(self):
        for i in range(50):self.paper(i)
        old={(x['area'],x['id']) for x in inventory(self.c,self.s)[0]}
        p,path=self.paper(51)
        apply(self.c,self.s,incumbents=old)
        plan=preview(self.c,self.s)
        self.assertEqual(plan['counts']['cross']['active'],50)
        self.assertEqual(plan['counts']['formal']['active'],0)
        self.assertTrue(archived(self.s,p['id']))
        self.assertIn('user content',note_for_id(self.c,self.s,p['id'],True).read_text())
        self.assertEqual(self.s.ingest(p,10000),p['id'])
        self.assertFalse(apply(self.c,self.s)['actions'])
        restore=preview(self.c,self.s,restore=p['id'])
        self.assertFalse(restore['actions']) # no verified formal eligibility

    def test_pins_conflict_and_no_translation_admission(self):
        self.c['capacity']={'cross_max':1}
        self.paper(1,True);p,_=self.paper(2,True)
        self.assertTrue(preview(self.c,self.s)['problems'])
        q=dict(p,id='new')
        self.assertFalse(branches.admission(self.c,self.s,q))
        self.assertFalse(apply(self.c,self.s)['actions'])

    def test_independent_event_quota_retry_and_filter(self):
        p,_=self.paper(1)
        self.assertEqual(branches.used(self.s,'cross'),1)
        self.assertEqual(branches.used(self.s,'ordinary'),0)
        self.s.record(p['id'],'daily','v2','pending',{})
        self.assertEqual(branches.used(self.s,'cross'),1)
        self.assertEqual(branches.assign(self.s,p,self.c),'cross')
        p['title']='machine learning protein medical'
        self.assertEqual(relevance(p,self.c),0)
        cc=copy.deepcopy(self.c);cc['_branch']='cross';cc['search']['exclude']=[]
        self.assertGreater(relevance(p,cc),0)

    def test_historical_entity_not_reclassified(self):
        p,_=capacity_fixtures.CapacityTests.paper(self,4,True,title='machine learning medical')
        self.assertEqual(branches.assign(self.s,p,self.c),'ordinary')
        self.assertIn('10_论文',str(note_for_id(self.c,self.s,p['id'],True)))

    def test_shared_search_limit_and_durable_cross_page(self):
        from paper_obsidian.daily import search
        from paper_obsidian.core import load_config
        c=load_config("tests/fixtures/config.toml");c['paths']=self.c['paths'];c['cross']=self.c['cross']
        c['search'].update(sources=['arxiv'],max_pages=1);c['limits']['candidates_per_day']=4
        c['_search_budget']=2
        report={'sources':{}}
        def source(name,config,*args):
            prefix='cross' if config.get('_branch') else 'ordinary'
            return [dict(source='TEST',aliases=[prefix+str(i)],title='machine learning medical '+str(i)) for i in range(4)],True
        with patch('paper_obsidian.daily.discover',side_effect=source),patch('paper_obsidian.daily.official_upgrade',return_value=None):
            search(c,{},self.s,None,{'journals':[],'conferences':[]},report)
            cc=branches.search_config(c);search(cc,{},self.s,None,{'journals':[],'conferences':[]},report)
            search(cc,{},self.s,None,{'journals':[],'conferences':[]},report)
        self.assertEqual(self.s.used('daily_search_records'),4)
        self.assertEqual(len(self.s.papers()),4)
        self.assertIn('arxiv:cross',report['sources'])
        self.assertEqual(report['sources']['arxiv:cross']['stop_reason'],'daily_record_limit')

    def test_cross_review_requires_positive_fulltext_admission(self):
        from paper_obsidian.daily_model import review_checks
        result={'summary':'fixture','summary_evidence_ids':['p1'],'sections':{},'branch_schema':'cross-v1'}
        parsed={'evidence_catalog':{'p1':{'text':'original evidence'}}}
        review={'checks':{'summary':{'supported':True,'reason':'source','evidence_ids':['p1']}},'issues':[]}
        self.assertTrue(review_checks(review,result,parsed))
        review['cross_admission']={'eligible':True,'reason':'specific method and clinical experiment','evidence_ids':['p1']}
        self.assertFalse(review_checks(review,result,parsed))

    def test_cross_publish_resume_uses_one_entity_and_preserves_handwriting(self):
        import test_daily as fixture
        from paper_obsidian.daily import publish
        from paper_obsidian.core import digest
        fixture.DailyTests.setUp(self)
        self.c['paths']['vault']=self.root/'vault'
        pid=self.store.ingest(dict(source='TEST',aliases=['one'],title='medical machine learning',venue='CVPR'),100)
        p=self.store.get(pid);self.store.set_state('branch:'+pid,{'primary':'cross'})
        self.result['branch_schema']='cross-v1'
        self.result['sections'].update({k:[{'text':'领域证据','evidence_ids':['p001-b001']}] for k in branches.FIELDS})
        source=self.root/'original.pdf';source.write_bytes(b'original')
        bilingual=self.root/'bilingual.pdf';bilingual.write_bytes(b'translated')
        gate={'eligible':True,'reason':'fixture','publication_status':'accepted','evidence':[]}
        saved={'result':self.result,'review':{'reviewer_type':'ai_api','cross_admission':{'eligible':True}}}
        tr={'path':str(bilingual),'output_sha256':digest(bilingual.read_bytes())}
        first=publish(p,gate,self.c,self.store,source,self.parsed,tr,saved=saved)
        note=Path(first['note']);note.write_text(note.read_text()+'\n我的手写\n')
        second=publish(p,gate,self.c,self.store,source,self.parsed,tr,saved=saved)
        self.assertEqual(first['note'],second['note'])
        self.assertIn('20_交叉领域论文',str(note))
        self.assertIn('我的手写',note.read_text())
        self.assertEqual(branches.used(self.store,'cross'),1)
        self.assertEqual(len(list(self.c['paths']['vault'].rglob('阅读笔记.md'))),1)

    def price_fixture(self):
        price=dict(version='v1',source_id='a',model='m',currency='CNY',cache_hit='0.04',cache_miss='2',output='8',source='official',checked_at='2026-10-23',valid_from='2026-10-23T18:00:00+08:00',valid_until='2026-10-30T00:00:00+08:00',tariff='beijing-offpeak-v1')
        self.c['api_accounting']={'prices':[price]}
        row=dict(request_id='unique',kind='summary',source_id='a',requested_model='m',status='success',started='2026-10-24T12:01:00+08:00',ended='2026-10-24T12:02:00+08:00',usage=dict(prompt_tokens=300,prompt_cache_hit_tokens=100,prompt_cache_miss_tokens=200,completion_tokens=10))
        return row

    def test_price_version_decimal_dedup_unknown_and_peak(self):
        row=self.price_fixture();run=dict(model_requests=1)
        est=estimate(self.c,run,[row,row])
        self.assertEqual(est['amounts'],{'CNY':'0.000242'})
        self.assertEqual(est['requests'][0]['price']['version'],'v1')
        price=self.c['api_accounting']['prices'][0]
        price['calendar']={'source':'official-calendar','valid_from':'2026-10-23','valid_until':'2026-10-30','holidays':[]}
        row.update(started='2026-10-26T10:00:00+08:00',ended='2026-10-26T10:01:00+08:00')
        self.assertEqual(estimate(self.c,run,[row])['amounts'],{'CNY':'0.000484'})
        row.update(started='2026-10-26T11:59:59+08:00',ended='2026-10-26T12:00:01+08:00')
        self.assertEqual(estimate(self.c,run,[row])['status'],'incomplete')
        row['started']='2026-10-23T12:01:00+08:00';row['ended']='2026-10-23T12:02:00+08:00'
        self.assertEqual(estimate(self.c,run,[row])['status'],'incomplete')
        row['source_id']='proxy'
        self.assertEqual(estimate(self.c,run,[row])['amounts'],{})
        row['usage']={}
        self.assertEqual(estimate(self.c,run,[row])['requests'][0]['cost_status'],'usage_unknown')

    def test_day_unique_receipts_keep_incomplete_runs(self):
        row=self.price_fixture();est=estimate(self.c,dict(model_requests=1),[row])
        folder=self.c['paths']['data']/'daily-runs';folder.mkdir()
        run=dict(run_id='1',started=row['started'],estimate=est)
        (folder/'1.api.json').write_text(json.dumps(run))
        run['run_id']='2'
        result=day_estimate(self.c,run)
        self.assertEqual(result['requests'],1)
        self.assertEqual(result['amounts'],{'a/CNY':'0.000242'})
        run['estimate']={'status':'incomplete','reasons':['missing usage']}
        self.assertEqual(day_estimate(self.c,run)['status'],'incomplete')
