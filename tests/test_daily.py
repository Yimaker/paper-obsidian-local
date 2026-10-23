import copy
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from paper_obsidian.core import Store, digest
from paper_obsidian.daily_model import Client, DailyError, ServiceBlocked, FIELDS, validate, review_checks
from paper_obsidian.daily import completed, portable
from paper_obsidian.evidence import attach_catalog


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.c={'paths':{'data':self.root},'daily':{'context_tokens':1048576,'output_tokens':16000,'safety_tokens':32768,'max_request_attempts':2,'max_revisions':2}}
        self.env={'API_BASE_URL':'https://example.test','API_KEY':'test-only','MODEL':'test'}
        self.store=Store(self.root);self.addCleanup(self.store.db.close)
        self.parsed=attach_catalog({'pages':['Result accuracy 77.92 versus 76.46 on test set.\n'],'page_count':1})
        self.result={'schema_version':'daily-reading-v1','title_zh':'测试方法','summary':'这是测试结果','summary_evidence_ids':['p001-b001'],
                     'sections':{k:[{'text':'原文中的77.92与76.46','evidence_ids':['p001-b001']}] for k in FIELDS}}

    def test_evidence_id_does_not_approve_semantics(self):
        validate(self.result,self.parsed)
        with self.assertRaises(DailyError):review_checks({'checks':{}},self.result,self.parsed)

    def test_arithmetic_and_unsupported_claim_block(self):
        from paper_obsidian.daily_model import all_claims
        checks={k:{'supported':True,'reason':'exact source context','evidence_ids':['p001-b001']} for k,_ in all_claims(self.result)}
        report={'checks':checks,'issues':[],'numeric_checks':[{'claim':'results.0','expression':'77.92-76.46','reported':3.2,'supported':True}]}
        self.assertTrue(review_checks(report,self.result,self.parsed))
        report['numeric_checks'][0]['reported']=1.46
        self.assertFalse(review_checks(report,self.result,self.parsed))
        checks['method.0']['supported']=False
        self.assertTrue(review_checks(report,self.result,self.parsed))

    def test_supported_caution_is_retained_without_rejection(self):
        from paper_obsidian.daily_model import all_claims
        review={'checks':{k:{'supported':True,'reason':'原文支持，已标范围','evidence_ids':['p001-b001']} for k,_ in all_claims(self.result)},
                'issues':[{'claim':'summary','severity':'minor','message':'草稿已经正确，保持范围'}]}
        self.assertFalse(review_checks(review,self.result,self.parsed))
        self.assertEqual(len(review['issues']),1)

    def test_api_cache_makes_zero_repeat_calls_and_receipts_usage(self):
        calls=[]
        def respond(request):
            calls.append(request)
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
                'usage':{'prompt_tokens':3,'completion_tokens':2,'total_tokens':5}})
        client=Client(self.c,self.env,self.store,httpx.MockTransport(respond))
        self.assertTrue(client.call('test','system',{'source':'original'})['ok'])
        self.assertTrue(client.call('test','system',{'source':'original'})['ok'])
        self.assertEqual(len(calls),1);self.assertEqual(client.hits,1)
        request=next((self.root/'daily-api').glob('*/request.json')).read_text()
        self.assertNotIn('test-only',request)
        receipt=json.loads(next((self.root/'daily-api').glob('*/attempts.json')).read_text())
        self.assertEqual(receipt[0]['usage']['total_tokens'],5)

    def test_auth_stops_other_model_calls(self):
        calls=[]
        def respond(request):calls.append(request);return httpx.Response(402,json={'error':'balance'})
        client=Client(self.c,self.env,self.store,httpx.MockTransport(respond))
        with self.assertRaises(ServiceBlocked):client.call('a','system',{})
        with self.assertRaises(ServiceBlocked):client.call('b','system',{})
        self.assertEqual(len(calls),1)

    def test_keepalive_does_not_bypass_wall_deadline(self):
        import signal,time
        if not hasattr(signal,'SIGALRM'):self.skipTest('daily runtime is WSL')
        from paper_obsidian.daily_model import wall_deadline
        with self.assertRaises(TimeoutError):
            with wall_deadline(.02):time.sleep(.2)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0],0)

    def test_partial_pdf_never_counts_as_delivery(self):
        p={'id':'fixture'}
        self.assertIsNone(completed(p,self.c,self.store))
        self.store.record('fixture','delivery','v','success',{'note':str(self.root/'missing.md'),'bilingual':str(self.root/'missing.pdf')})
        self.assertIsNone(completed(p,self.c,self.store))

    def test_large_input_uses_actual_context_not_sixty_thousand_chars(self):
        client=Client(self.c,self.env,self.store,httpx.MockTransport(lambda _:httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})))
        self.assertTrue(client.call('large','system',{'source':'original '*12000})['ok'])

    def test_publish_moves_archives_and_protects_manual_links(self):
        from paper_obsidian.daily import publish
        from paper_obsidian.vault import START,END,check_links
        vault=self.root/'vault';self.c['paths']['vault']=vault
        for name in ('10_论文笔记','20_附件','00_每日简报'):(vault/name).mkdir(parents=True,exist_ok=True)
        pid='fixture'
        old=vault/'10_论文笔记'/f'{pid}.md'
        old.write_text('---\ncustom_field: kept\n---\n'+START+'\n旧正文\n'+END+'\n\n## 我的笔记\n手写原话 [other](../other.md)\n',encoding='utf-8')
        (vault/'other.md').write_text('其他笔记',encoding='utf-8')
        brief=vault/'00_每日简报'/'old.md';brief.write_text('[论文](../10_论文笔记/fixture.md)',encoding='utf-8')
        source=vault/'20_附件'/'fixture-original.pdf';source.write_bytes(b'%PDF-fixture-original')
        bilingual=self.root/'bilingual.pdf';bilingual.write_bytes(b'%PDF-fixture-bilingual')
        self.store.record(pid,'pdf','v','success',{'path':str(source)})
        p={'id':pid,'title':'Fixture learning','venue':'CVPR','date':'2026-06','publication':{'proceedings_year':2026,'status':'accepted'},'pdf_url':'https://example.test/paper.pdf'}
        delivery=publish(p,{'eligible':True,'reason':'verified fixture','publication_status':'accepted','evidence':[]},self.c,self.store,source,self.parsed,
            {'path':str(bilingual),'output_sha256':digest(bilingual.read_bytes())},saved={'result':self.result,'review':{'reviewer_type':'ai_api'}})
        note=Path(delivery['note']);raw=note.read_text('utf-8')
        self.assertIn('custom_field: kept',raw);self.assertIn('手写原话',raw)
        self.assertFalse(old.exists());self.assertFalse(source.exists())
        archive=self.root/'archive'/pid
        self.assertEqual((archive/'fixture-original.pdf').read_bytes(),b'%PDF-fixture-original')
        self.assertEqual(set(q.name for q in note.parent.iterdir()),{'阅读笔记.md','中英对照.pdf'})
        self.assertEqual(check_links(vault)['missing_links'],[])
        self.assertIsNotNone(completed(p,self.c,self.store))

if __name__=='__main__':unittest.main()
