import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from paper_obsidian.core import Store, load_config, digest, now
from paper_obsidian.cnki_browser import (Budget, Halt, allowed, safe_url, sanitize, pause_reason,
    validate_action, process_downloads, run, root, stats, save)
from paper_obsidian.cnki import attach_fulltext, import_file
from tests.test_cnki import REAL


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name);self.c=load_config("tests/fixtures/config.toml")
        self.c['paths'].update(data=self.path/'data',vault=self.path/'vault')
        # Isolated price evidence; never depend on a personal crawl cache.
        evidence=self.path/'.firecrawl/deepseek-pricing-cny.md'
        evidence.parent.mkdir(parents=True);evidence.write_bytes(b'TEST ONLY synthetic tariff evidence')
        self.c['cnki_browser']['price']['evidence_sha256']=digest(evidence.read_bytes())
        root_patch=patch('paper_obsidian.cnki_browser.ROOT',self.path)
        root_patch.start();self.addCleanup(root_patch.stop)
        self.s=Store(self.c['paths']['data']);self.addCleanup(self.s.db.close)
        self.env={'API_BASE_URL':'https://api.deepseek.com','API_KEY':'sk-test-secret','MODEL':'deepseek-flash'}
        self.view={'url':'https://www.cnki.net/','text':'深圳理工大学', 'controls':[{'ref':'r0','label':'检索','kind':'click'}]}
        self.session={'id':'1234567890abcdef','query':'机器学习','started':now(),'status':'created','url':'https://www.cnki.net/',
                      'download_one':False,'paper_ids':[],'processed_downloads':[],'steps':[]}
        self.calls=0

    def response(self,request):
        self.calls+=1
        self.assertEqual(request.headers['Authorization'],'Bearer sk-test-secret')
        self.assertNotIn('sk-test-secret',request.content.decode())
        return httpx.Response(200,json={'model':'deepseek-flash','usage':{'prompt_tokens':100,'completion_tokens':20,
            'prompt_cache_hit_tokens':0,'prompt_cache_miss_tokens':100},'choices':[{'finish_reason':'stop',
                'message':{'content':'{"action":"click","ref":"r0"}'}}]})

    def budget(self,handler=None): return Budget(self.c,self.env,self.s,httpx.MockTransport(handler or self.response))

    def download(self,raw,name,kind):
        path=root(self.c)/'sessions'/self.session['id']/'downloads'/name
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
        return {'path':str(path),'filename':name,'kind':kind,'source_url':'https://kns.cnki.net/kcms2/article/abstract', 'downloaded_at':now()}

    def pdf(self):
        import pymupdf
        doc=pymupdf.open();page=doc.new_page()
        page.insert_textbox(pymupdf.Rect(40,40,550,800),'基于HHO优化CNN-BiLSTM-Attention的智能电表异常诊断方法\n'+'中文原文方法实验结果数据'*150,fontname='china-s',fontsize=10)
        raw=doc.tobytes();doc.close();return raw

    def test_domain_and_sensitive_url(self):
        for u in ('http://cnki.net','https://cnki.net.evil.org/','file:///x','https://szu.edu.cn','https://sztu.edu.cn','https://cnki.net:8443/'):
            self.assertFalse(allowed(u))
        self.assertTrue(allowed('https://kns.cnki.net/'))
        self.assertTrue(allowed('https://idp.suat-sz.edu.cn/'))
        self.assertTrue(allowed('https://sso.suat-sz.edu.cn/gravity-login/'))
        self.assertEqual(safe_url('https://kns.cnki.net/x?token=private&filename=ABCD1234'),'https://kns.cnki.net/x?filename=ABCD1234')

    def test_sanitizer_and_injection(self):
        v=sanitize({'cookie':'x','password':'secret','text':'Bearer secret sk-abcdef person@example.com token=hidden https://kns.cnki.net/x?token=hidden'})
        text=json.dumps(v)
        for value in ('hidden','secret','abcdef','person@example.com'):self.assertNotIn(value,text)
        for a in ({'action':'evaluate','code':'steal()'},{'action':'click','ref':'r99'},{'action':'type','ref':'r0'},{'action':'navigate','url':'https://evil.org'}):
            with self.assertRaises(Halt):validate_action(a,self.view)
        evil={'controls':[{'ref':'r0','label':'购买全文','kind':'click'}]}
        with self.assertRaises(Halt):validate_action({'action':'click','ref':'r0'},evil)

    def test_pause_reasons(self):
        self.assertEqual(pause_reason({'navigation_error':'net::ERR_FAILED'}),'navigation_failed')
        for text,reason in [('验证码','captcha_or_access_limit'),('权限不足','payment_or_permission'),('购买全文','payment_or_permission'),('请先登录','login_required'),('深圳技术大学','wrong_institution')]:
            self.assertEqual(pause_reason({'text':text}),reason)
        self.assertEqual(pause_reason({'login':True}),'login_required')
        self.assertIsNone(pause_reason({'text':'深圳理工大学 登录 注册 PDF下载'}))
        self.assertIsNone(pause_reason({'text':'深圳理工大学 论文作者：深圳大学研究团队'}))

    def test_budget_success_cache_and_total(self):
        b=self.budget();a=b.decide(self.view,{'query':'机器学习'})
        self.assertEqual(a['action'],'click');self.assertEqual(self.calls,1)
        self.assertEqual(b.decide(self.view,{'query':'机器学习'}),a);self.assertEqual(self.calls,1)
        b=self.budget();b.ledger['spent_upper_cny']='0.9999';save(b.path,b.ledger)
        with self.assertRaisesRegex(Halt,'budget_stop'):self.budget().decide(self.view,{'query':'different'})
        self.assertEqual(self.calls,1)

    def test_unknown_request_persistently_stops(self):
        def timeout(request):raise httpx.ReadTimeout('secret',request=request)
        with self.assertRaises(Halt):self.budget(timeout).decide(self.view,{})
        with self.assertRaisesRegex(Halt,'cost_or_request_unknown'):self.budget().decide(self.view,{})
        self.assertEqual(self.calls,0)
        self.assertNotIn('secret',self.budget().path.read_text())

    def test_unknown_usage_stops(self):
        def incomplete(request):return httpx.Response(200,json={'usage':{},'model':'deepseek-flash'})
        with self.assertRaisesRegex(Halt,'usage_unknown'):self.budget(incomplete).decide(self.view,{})
        with self.assertRaisesRegex(Halt,'cost_or_request_unknown'):self.budget().decide(self.view,{})

    def test_unknown_price_model_expiry_input_and_calls(self):
        price=self.c['cnki_browser']['price']
        for field,value,expected in [('currency','USD','price_unknown'),('valid_until','2000-01-01T00:00:00+08:00','price_unknown'),('cache_miss','-1','price_unknown'),('model','different','model_price_mismatch')]:
            with self.subTest(field=field):
                old=price[field];price[field]=value
                with self.assertRaisesRegex(Halt,expected):self.budget().decide(self.view,{})
                price[field]=old
        self.c['cnki_browser']['max_input_bytes']=10
        with self.assertRaisesRegex(Halt,'input_limit'):self.budget().decide(self.view,{})
        self.c['cnki_browser']['max_input_bytes']=24000;self.c['cnki_browser']['max_calls']=0
        with self.assertRaisesRegex(Halt,'budget_stop'):self.budget().decide(self.view,{})
        self.assertEqual(self.calls,0)

    def test_import_manifest_zero_paid_calls_and_capacity(self):
        item=self.download(REAL.read_bytes(),'real.enw','metadata')
        with patch('httpx.Client.send',side_effect=AssertionError('network')),patch.object(Budget,'decide',side_effect=AssertionError('paid')):
            process_downloads(self.c,self.s,self.session,{'downloads':[item]})
            process_downloads(self.c,self.s,self.session,{'downloads':[item]})
        self.assertEqual(len(self.s.papers()),1);self.assertEqual(len(self.session['imports']),1)
        self.assertFalse(self.s.get_state('cnki:processing:'+self.session['paper_ids'][0])['approved'])
        self.assertFalse(self.s.stage(self.session['paper_ids'][0],'delivery'))
        self.assertEqual(self.s.used('model_calls'),0)

    def test_record_limit_and_path_escape(self):
        item=self.download(REAL.read_bytes()+b'\n'+REAL.read_bytes(),'batch.enw','metadata')
        with self.assertRaisesRegex(Halt,'record_limit'):process_downloads(self.c,self.s,self.session,{'downloads':[item]})
        self.assertEqual(len(self.s.papers()),0)
        item['path']=str(REAL)
        with self.assertRaisesRegex(Halt,'download_path_invalid'):process_downloads(self.c,self.s,self.session,{'downloads':[item]})

    def test_pdf_download_and_duplicate_original_preserved(self):
        report=import_file(self.c,self.s,REAL);pid=report['rows'][0]['paper_id'];self.session['paper_ids']=[pid]
        raw=self.pdf();item=self.download(raw,'paper.pdf','fulltext')
        process_downloads(self.c,self.s,self.session,{'downloads':[item]})
        process_downloads(self.c,self.s,self.session,{'downloads':[item]})
        self.assertEqual(len(self.session['fulltexts']),1);result=self.session['fulltexts'][0]
        self.assertEqual(Path(result['path']).read_bytes(),raw)
        self.assertEqual(self.s.stage(pid,'pdf')['status'],'success')
        self.assertIsNone(self.s.stage(pid,'daily_summary'))

    def test_integrity_failure_and_caj_are_preserved(self):
        pid=import_file(self.c,self.s,REAL)['rows'][0]['paper_id'];self.session['paper_ids']=[pid]
        for raw,name in [(b'%PDF-broken','truncated.pdf'),(b'CAJ original','original.caj'),(b'<html>login</html>','fake.pdf')]:
            item=self.download(raw,name,'fulltext')
            with self.assertRaisesRegex(Halt,'fulltext_pending'):process_downloads(self.c,self.s,self.session,{'downloads':[item]})
            self.assertEqual(Path(item['path']).read_bytes(),raw)
            self.assertIsNone(self.s.stage(pid,'pdf'))

    def test_resumable_pause_and_step_limit_no_paid_repeat(self):
        class FakeBrowser:
            def __init__(inner,c,session):pass
            def call(inner,action):return {'url':'https://www.cnki.net/','text':'请先登录','login':True,'controls':[]}
            def close(inner):pass
        with patch('paper_obsidian.cnki_browser.Browser',FakeBrowser),patch.object(Budget,'decide',side_effect=AssertionError('paid')):
            r=run(self.c,self.env,query='机器学习')
        self.assertEqual(r['reason'],'login_required')
        class ReadyBrowser(FakeBrowser):
            def call(inner,action):return self.view
        with patch('paper_obsidian.cnki_browser.Browser',ReadyBrowser),patch.object(Budget,'decide',return_value={'action':'pause'}):
            r2=run(self.c,self.env,session_id=r['id'])
        self.assertEqual(r2['id'],r['id']);self.assertEqual(r2['reason'],'model_pause')
        self.c['cnki_browser']['max_steps']=0
        with patch('paper_obsidian.cnki_browser.Browser',ReadyBrowser),patch.object(Budget,'decide',side_effect=AssertionError('paid')):
            r3=run(self.c,self.env,query='机器学习')
        self.assertEqual(r3['reason'],'step_limit')

    def test_daily_does_not_pay_browser_and_preserves_sources(self):
        from paper_obsidian.daily import run_daily
        with patch('paper_obsidian.daily.backup'),patch('paper_obsidian.daily.archive_legacy_attachments'),patch('paper_obsidian.daily.search') as search,patch('paper_obsidian.cnki_browser.stats',side_effect=ValueError()),patch.object(Budget,'decide',side_effect=AssertionError('paid')),patch('httpx.Client.send',side_effect=AssertionError('network')):
            # Brief independently uses stats; return valid stats there.
            with patch('paper_obsidian.daily.daily_brief',return_value='test'):
                result=run_daily(self.c,{},metadata_only=True)
        self.assertEqual(search.call_count,2 if self.c.get("cross",{}).get("enabled") else 1);self.assertIn('读取失败',result['cnki_browser']['status'])

    def test_same_day_stats_are_sets(self):
        self.session.update(status='paused',reason='login_required',fulltexts=[{'sha256':'same'}])
        self.s.set_state('cnki:browser:session:'+self.session['id'],self.session)
        second=dict(self.session,id='abcdef1234567890')
        self.s.set_state('cnki:browser:session:'+second['id'],second)
        for _ in range(2):
            value=stats(self.s,now()[:10]);self.assertEqual(value['download_success'],1);self.assertEqual(value['manual_pending'],2)

    def test_unknown_cnki_language_waits_without_translation(self):
        from paper_obsidian.language import prepare
        p={'id':'p','sources':['cnki']};path=self.path/'source.pdf';path.write_bytes(b'original')
        pid=self.s.ingest({'source':'cnki','aliases':['cnki:x'],'title':'测试'});p['id']=pid
        with patch('paper_obsidian.daily.translate',side_effect=AssertionError('paid')):
            with self.assertRaisesRegex(RuntimeError,'语言未知'):prepare(p,path,{'pages':['short']},self.c,self.s)

    def test_full_workflow_uses_import_download_integrity_and_budget_hold(self):
        raw=self.pdf();outer=self
        class FlowBrowser:
            def __init__(inner,c,session):inner.session=session;inner.index=0
            def call(inner,action):
                outer.session=inner.session
                if inner.index==0:
                    item=outer.download(REAL.read_bytes(),'real.enw','metadata')
                else:
                    item=outer.download(raw,'full.pdf','fulltext')
                inner.index+=1
                title='基于HHO优化CNN-BiLSTM-Attention的智能电表异常诊断方法'
                return {'url':'https://link.cnki.net/doi/10.16157/j.issn.0258-7998.268230',
                        'text':'深圳理工大学 '+title,'institution':'深圳理工大学','controls':[{'ref':'r0','label':'PDF下载','kind':'click','download':True}], 'downloads':[item]}
            def close(inner):pass
        with patch('paper_obsidian.cnki_browser.Browser',FlowBrowser),patch.object(Budget,'decide',return_value={'action':'click','ref':'r0'}),patch('paper_obsidian.daily_model.Client.call',side_effect=AssertionError('reading paid')):
            result=run(self.c,self.env,query='机器学习',download_one=True)
        self.assertEqual(result['status'],'complete');self.assertEqual(result['reason'],'fulltext_acquired_pending_processing')
        pid=result['paper_ids'][0]
        self.assertEqual(self.s.stage(pid,'pdf')['status'],'success')
        self.assertFalse(self.s.get_state('cnki:processing:'+pid)['approved'])
        self.assertIsNone(self.s.stage(pid,'delivery'))

    def test_wrong_paper_download_is_blocked_before_click(self):
        pid=import_file(self.c,self.s,REAL)['rows'][0]['paper_id'];outer=self
        class WrongBrowser:
            def __init__(inner,c,session):pass
            def call(inner,action):
                if action['action']!='navigate':raise AssertionError('wrong download executed')
                return {'url':'https://kns.cnki.net/article/abstract','text':'深圳理工大学 另一篇论文','institution':'深圳理工大学',
                        'controls':[{'ref':'r0','label':'PDF下载','kind':'click','download':True}]}
            def close(inner):pass
        with patch('paper_obsidian.cnki_browser.Browser',WrongBrowser),patch.object(Budget,'decide',return_value={'action':'click','ref':'r0'}):
            result=run(self.c,self.env,query='机器学习',paper_id=pid,download_one=True)
        self.assertEqual(result['reason'],'paper_identity_unconfirmed');self.assertIsNone(self.s.stage(pid,'pdf'))

    def test_archived_browser_entry_does_not_restore(self):
        self.c['capacity']['candidate_max']=1
        a=self.path/'a.enw';a.write_text('%0 Journal Article\n%T A\n%R 10.1234/a\n')
        b=self.path/'b.enw';b.write_text('%0 Journal Article\n%T B\n%R 10.1234/b\n')
        import_file(self.c,self.s,a)
        pid=import_file(self.c,self.s,b)['rows'][0]['paper_id']
        with patch('paper_obsidian.cnki_browser.Browser',side_effect=AssertionError('browser')):
            with self.assertRaisesRegex(Halt,'archived_no_restore'):run(self.c,self.env,query='机器学习',paper_id=pid,download_one=True)

    def test_truncated_existing_valid_pdf_and_duplicate_attach(self):
        pid=import_file(self.c,self.s,REAL)['rows'][0]['paper_id'];raw=self.pdf()
        item=self.download(raw,'valid.pdf','fulltext')
        result=attach_fulltext(self.c,pid,item['path']);repeat=attach_fulltext(self.c,pid,item['path'])
        self.assertTrue(repeat['repeated_file']);self.assertEqual(result['attached_at'],repeat['attached_at'])
        broken=self.download(raw[:-20],'broken.pdf','fulltext')
        failed=attach_fulltext(self.c,pid,broken['path']);self.assertTrue(failed['failed'])
        self.assertEqual(json.loads(self.s.stage(pid,'pdf')['detail'])['sha256'],digest(raw))

    def test_manual_login_keeps_window_on_navigation_error_without_main_lock(self):
        from paper_obsidian.core import run_lock
        outer=self
        class LoginBrowser:
            def __init__(inner,c,session):
                # Daily/local work can acquire its normal lock during manual login.
                with run_lock(c['paths']['data']):pass
                class Process:
                    def poll(self):return None
                inner.p=Process()
            def call(inner,a):raise Halt('browser_error')
            def close(inner):pass
        def finish(seconds):(root(self.c)/'DISABLED').write_text('test close')
        with patch('paper_obsidian.cnki_browser.Browser',LoginBrowser),patch('paper_obsidian.cnki_browser.time.sleep',side_effect=finish),patch.object(Budget,'decide',side_effect=AssertionError('paid')):
            result=run(self.c,self.env,query='机器学习',open_only=True)
        self.assertEqual(result['reason'],'user_login_window_open')

    def test_school_entry_is_only_manual(self):
        from types import SimpleNamespace
        from paper_obsidian.cnki_browser import command
        with self.assertRaisesRegex(Halt,'manual_only'):
            command(self.c,self.env,SimpleNamespace(school=True,action='run'))

    def test_existing_fulltext_new_session_reuses_without_browser_or_paid_call(self):
        pid=import_file(self.c,self.s,REAL)['rows'][0]['paper_id']
        item=self.download(self.pdf(),'valid.pdf','fulltext');attach_fulltext(self.c,pid,item['path'])
        with patch('paper_obsidian.cnki_browser.Browser',side_effect=AssertionError('duplicate download')),patch.object(Budget,'decide',side_effect=AssertionError('paid')):
            r=run(self.c,self.env,query='机器学习',paper_id=pid,download_one=True)
        self.assertEqual(r['reason'],'existing_fulltext_reused_pending_processing')
        self.assertEqual(stats(self.s,now()[:10])['download_success'],0)


if __name__=='__main__':unittest.main()
