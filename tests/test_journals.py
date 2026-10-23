"""Synthetic unit fixtures are NOT evidence for live journal metrics."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_obsidian.core import Store, atomic_write, digest, now
from paper_obsidian.eligibility import classify
from paper_obsidian.journals import (JournalService, issn, parse_mjl, import_metrics, validate_metric,
    qualification, header, patch_note, BEGIN, END, conflicts, merge_metrics)
from paper_obsidian.vault import managed_write, render_paper, START, END as PAPER_END


class JournalsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.c={'paths':{'data':self.root/'data','vault':self.root/'vault'},
                'journals':{'mjl_enabled':True,'max_official_per_day':3}}
        self.s=Store(self.c['paths']['data']);self.addCleanup(self.s.db.close)
        self.p={'id':'test-journal','title':'Unit fixture (not a real acceptance result)', 'venue_kind':'journal',
                'venue':'Fixture Journal','issns':['1532-4435','1533-7928'],'date':'2024-01-01',
                'doi':'10.test/fixture','publication':{'status':'published'}}
        self.gate=classify(self.p,{'journals':[],'conferences':[]})
        self.checked=now()
        self.source={'id':'https://openalex.org/S-fixture','type':'journal','display_name':'Fixture Journal',
                     'issn_l':'1532-4435','issn':self.p['issns']}
        self.mjl='''Science Citation Index Expanded (SCIE)
## Search Results
Found 1 results (Page 1)
## Exact Match Found
FIXTURE JOURNAL
ISSN / eISSN:
1532-4435 / 1533-7928
_Web of Science_ Core Collection:
Science Citation Index Expanded
Additional _Web of Science_ Indexes:
Current Contents
* * *
Login Create Free Account
'''

    def metric(self,field='jif',value=4.2,**kw):
        return dict(field=field,value=value,metric_year=2024,source='https://www.letpub.com.cn/index.php?page=journalapp',
                    checked_at=self.checked,**kw)

    def http(self,fail=False):
        owner=self
        class Stub:
            def __init__(self):self.calls=[]
            def json(self,url):
                self.calls.append(url)
                if fail:raise TimeoutError()
                return copy.deepcopy(owner.source)
        return Stub()

    def test_issn_check_and_normalization(self):
        self.assertEqual(issn('15324435'),'1532-4435')
        with self.assertRaises(ValueError):issn('1532-4430')

    def test_exact_issn_official_and_cache_by_journal(self):
        h=self.http();queries=[]
        j=JournalService(self.c,{},self.s,h,lambda u:queries.append(u) or self.mjl)
        a=j.annotate(self.p,self.gate)
        p=dict(self.p,id='second-paper',issns=['1533-7928'])
        b=j.annotate(p,self.gate)
        self.assertEqual(a['issn_l'],b['issn_l']);self.assertEqual(len(h.calls),2)
        self.assertEqual(len(queries),1);self.assertTrue(a['metrics'][0]['value'])
        self.assertEqual(a['metrics'][0]['verification'],'verified_official')
        self.assertFalse(classify(self.p,{'journals':[],'conferences':[]})['eligible'])

    def test_mjl_filter_and_wrong_result_rejected(self):
        for m in (self.mjl.split('## Exact Match Found')[0],self.mjl.replace('Found 1 results','Found 2 results'),
                  self.mjl.replace('1532-4435 / 1533-7928','0885-6125 / 1573-0565')):
            with self.assertRaises(ValueError):parse_mjl(m,'1532-4435','https://mjl.clarivate.com/search-results',self.checked,'x')

    def test_esci_explicit_false_and_has_jif_not_scie(self):
        m=self.mjl.replace('Science Citation Index Expanded\nAdditional','Emerging Sources Citation Index\nAdditional')
        self.assertFalse(parse_mjl(m,'1532-4435','url',self.checked,'x')['value'])
        m=self.mjl.replace('Science Citation Index Expanded\nAdditional','Has 2025 JIF\nAdditional')
        with self.assertRaises(ValueError):parse_mjl(m,'1532-4435','url',self.checked,'x')

    def test_failure_still_renders_note_and_negative_cache(self):
        h=self.http(fail=True);j=JournalService(self.c,{},self.s,h)
        j.annotate(self.p,self.gate);calls=len(h.calls);j.annotate(self.p,self.gate)
        self.assertEqual(len(h.calls),calls)
        path=render_paper(self.p,self.gate,self.c,self.s)
        raw=path.read_text('utf-8')
        self.assertIn('待核验／暂无数据',raw);self.assertIn('OpenAlex期刊识别未完成',raw)
        self.assertIn('中文总结状态',raw);self.assertNotIn('JIF）\n- 0',raw)

    def test_failed_official_keeps_identity_and_saves_reason(self):
        def fail(url):raise TimeoutError()
        j=JournalService(self.c,{},self.s,self.http(),fail)
        r=j.annotate(self.p,self.gate)
        self.assertEqual(r['identity_status'],'issn_exact')
        self.assertTrue(any('Clarivate' in x for x in r['issues']))
        self.assertFalse(r['metrics'])

    def test_name_only_no_network_or_forced_match(self):
        h=self.http();j=JournalService(self.c,{},self.s,h)
        r=j.lookup(dict(self.p,issns=[]))
        self.assertEqual(h.calls,[]);self.assertEqual(r['identity_status'],'unverified')

    def test_multiple_sources_ambiguous_and_not_cached_as_alias(self):
        h=self.http();orig=h.json
        def get(url):
            r=orig(url)
            if url.endswith('1533-7928'):r['id']='https://openalex.org/DIFFERENT'
            return r
        h.json=get
        r=JournalService(self.c,{},self.s,h).lookup(self.p)
        self.assertEqual(r['identity_status'],'unverified')
        self.assertIsNone(self.s.get_state('journal:alias:1533-7928'))

    def test_conference_skips_journal_lookup(self):
        h=self.http();p=dict(self.p,venue_kind='conference',venue='CVPR',publication={'status':'accepted','proceedings_year':2026})
        JournalService(self.c,{},self.s,h).lookup(p)
        body=header(p,self.gate,self.s)
        self.assertEqual(h.calls,[]);self.assertIn('CVPR',body);self.assertNotIn('Journal Impact Factor',body)

    def test_byte_preserving_patch_and_rerun(self):
        path=self.root/'vault'/'existing.md'
        raw='---\r\npaper_id: test-journal\r\ncustom: 保留\r\n---\r\n前置手写\r\n'+START+'\r\n已有正文公式 $x$ [附件](a.pdf)\r\n'+PAPER_END+'\r\n## 我的笔记\r\n手写重点\r\n'
        atomic_write(path,raw)
        self.assertTrue(patch_note(path,self.p,self.gate,self.c,self.s))
        after=path.read_bytes().decode('utf-8')
        a=after.index(BEGIN);b=after.index(END)+len(END)
        restored=after[:a-1]+after[b+1:]
        self.assertEqual(restored,raw)
        self.assertFalse(patch_note(path,self.p,self.gate,self.c,self.s))
        self.assertEqual(after,path.read_bytes().decode('utf-8'))

    def test_malformed_markers_never_overwrite(self):
        p=self.root/'broken.md';atomic_write(p,START+' old '+START+PAPER_END)
        old=p.read_bytes()
        with self.assertRaises(ValueError):patch_note(p,self.p,self.gate,self.c,self.s)
        self.assertEqual(old,p.read_bytes())

    def test_import_separate_years_systems_all_jcr_categories_and_conflicts(self):
        metrics=[self.metric('jif',4.2,report_year=2025),
                 self.metric('jcr','Q1',category='Fixture subject A',report_year=2025),
                 self.metric('jcr','Q3',category='Fixture subject B',report_year=2025),
                 dict(field='cas_major',value=2,category='Fixture major',edition='Fixture historical',edition_year=2023,
                      source='https://www.letpub.com.cn/',checked_at=self.checked),
                 dict(field='xinrui',value=1,category='Fixture major',edition='Fixture new',edition_year=2025,
                      source='https://www.letpub.com.cn/',checked_at=self.checked)]
        path=self.root/'import.json';path.write_text(json.dumps([{'issns':self.p['issns'],'metrics':metrics}]))
        import_metrics(path,self.s);import_metrics(path,self.s)
        j=JournalService(self.c,{},self.s,self.http())
        r=j.annotate(self.p,self.gate,offline=True)
        self.assertEqual(len(r['metrics']),5)
        text=header(self.p,self.gate,self.s)
        for v in ('2023','2024','2025','Fixture subject A','Fixture subject B','Q1','Q3','新锐分区','第三方来源'):self.assertIn(v,text)
        self.assertNotIn('已核验符合SCI一二区',text)
        extra=validate_metric(self.metric('jcr','Q2',category='Fixture subject A',report_year=2025),self.root)
        self.assertEqual(len(conflicts(r['metrics']+[extra])),1)

    def test_cas_or_jcr_without_explicit_year_rejected(self):
        for row in [self.metric('cas_major',1,category='A',edition='x'),
                    dict(field='jcr',value='Q1',category='A',source='https://www.letpub.com.cn',checked_at=self.checked)]:
            with self.assertRaises(ValueError):validate_metric(row,self.root)

    def test_fake_official_and_mixed_batch_rejected_atomically(self):
        metric=self.metric(verification='verified_official')
        with self.assertRaises(ValueError):validate_metric(metric,self.root)
        path=self.root/'bad.json';path.write_text(json.dumps([{'issns':['1532-4435'],'metrics':[self.metric()]},
            {'issns':['1533-7928'],'metrics':[metric]}]))
        with self.assertRaises(ValueError):import_metrics(path,self.s)
        self.assertIsNone(self.s.get_state('journal:import:1532-4435'))

    def test_jif_never_implies_scie_and_third_party_not_eligible(self):
        r={'metrics':[dict(self.metric(),authority='third_party')],'stale':False}
        self.assertIn('未知',qualification(r,self.gate))
        r['metrics'] += [dict(field='scie',value=True,as_of='2026-01-01',authority='third_party'),
            dict(field='cas_major',value=2,edition_year=2025,edition='fixture',category='fixture',authority='third_party')]
        self.assertIn('第三方资料显示符合',qualification(r,self.gate))
        r['metrics'].append(dict(field='scie',value=False,as_of='2026-02-01',authority='official'))
        self.assertIn('未知',qualification(r,self.gate))

    def test_expired_cache_offline_marked_historical(self):
        j=JournalService(self.c,{},self.s,self.http(),lambda u:self.mjl)
        r=j.lookup(self.p);r['expires_at']='2000-01-01T00:00:00+08:00'
        self.s.set_state('journal:cache:1532-4435',r)
        self.assertTrue(j.lookup(self.p,offline=True)['stale'])

    def test_failed_refresh_never_makes_old_scie_current_on_rerun(self):
        j=JournalService(self.c,{},self.s,self.http(),lambda u:self.mjl)
        r=j.lookup(self.p);r['expires_at']='2000-01-01T00:00:00+08:00'
        r['metrics'][0]['checked_at']='2000-01-01T00:00:00+08:00'
        self.s.set_state('journal:cache:1532-4435',r)
        def fail(u):raise TimeoutError()
        j.mjl_fetch=fail
        failed=j.lookup(self.p)
        self.assertTrue(failed['stale']);self.assertTrue(failed['metrics'])
        self.assertTrue(j.lookup(self.p)['stale'])

    def test_csv_uses_same_cache_and_unknown_report_year(self):
        path=self.root/'metrics.csv'
        path.write_text('issns,field,value,metric_year,source,checked_at\n1532-4435,jif,4.2,2024,https://www.letpub.com.cn/,'+self.checked+'\n')
        import_metrics(path,self.s)
        j=JournalService(self.c,{},self.s,self.http());r=j.annotate(self.p,self.gate,offline=True)
        self.assertEqual(r['metrics'][0]['metric_year'],2024)
        self.assertNotIn('report_year',r['metrics'][0])
        self.assertIn('JCR报告发布年份 待核验',header(self.p,self.gate,self.s))

    def test_query_daily_cap_no_retry_loop(self):
        self.s.spend('journal_official_lookup',1)
        self.c['journals']['max_official_per_day']=1
        j=JournalService(self.c,{},self.s,self.http(),lambda u:(_ for _ in ()).throw(AssertionError('must not fetch')))
        r=j.lookup(self.p)
        self.assertTrue(any('上限' in x for x in r['issues']))

    def test_daily_journal_exception_does_not_block_fulltext_summary_translation(self):
        from paper_obsidian.core import load_config
        from paper_obsidian.daily import run_daily
        from paper_obsidian.daily_model import Client
        c=load_config("tests/fixtures/config.toml");c['paths'].update(self.c['paths'])
        p=dict(self.p,source='UNIT_FIXTURE',aliases=['fixture:daily-journal'])
        pid=self.s.ingest(p)
        pdf=self.root/'source.pdf';atomic_write(pdf,b'fixture bytes; parser is mocked')
        gate=dict(eligible=True,reason='UNIT FIXTURE ONLY',publication_status='published',evidence=[])
        saved={'result':{},'review':{},'scope':'UNIT FIXTURE'}
        with patch('paper_obsidian.daily.classify',return_value=gate), \
             patch.object(JournalService,'annotate',side_effect=TimeoutError()), \
             patch('paper_obsidian.daily.get_pdf',return_value=pdf) as fulltext, \
             patch('paper_obsidian.daily.parse_original',return_value={'evidence_catalog':{}}), \
             patch.object(Client,'generate_review',return_value=saved) as summary, \
             patch('paper_obsidian.daily.translate',return_value={}) as translation, \
             patch('paper_obsidian.daily.publish',return_value={'folder':'UNIT_ONLY','completed_at':now()}) as publish:
            r=run_daily(c,{},fetch=False,only=[pid],max_complete=1)
        fulltext.assert_called_once();summary.assert_called_once();translation.assert_called_once();publish.assert_called_once()
        self.assertEqual(r['model_requests'],0);self.assertEqual(len(r['delivered']),1)
        self.assertTrue(r['journal_warnings']);self.assertFalse(r['failures'])

    def test_formal_note_lookup_does_not_create_candidate_duplicate(self):
        from paper_obsidian.journals import refresh_notes
        from paper_obsidian.library import reading_path
        p=dict(self.p,source='UNIT_FIXTURE',aliases=['fixture:formal'])
        pid=self.s.ingest(p);p=self.s.get(pid)
        path=reading_path(p,self.c,self.s,formal=True)
        managed_write(path,{'paper_id':pid},'original reviewed body',self.c)
        r=refresh_notes(self.c,{},self.s,{'journals':[],'conferences':[]},offline=True)
        self.assertFalse(r['failures']);self.assertIn(BEGIN,path.read_text())
        self.assertIn('original reviewed body',path.read_text())
        self.assertFalse(list((self.root/'vault'/'90_候选与待核验').glob('*.md')))

    def test_published_journal_filename_year_is_metadata_not_eligibility(self):
        from paper_obsidian.library import paper_year
        p=dict(self.p,dates={'openalex_publication_date':'2024-01-01'},
               publication={'status':'published','version':'publishedVersion'})
        self.assertEqual(paper_year(p),('2024','openalex_published_journal_metadata'))
        self.assertFalse(classify(p,{'journals':[]})['eligible'])


if __name__=='__main__':unittest.main()
