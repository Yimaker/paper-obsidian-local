import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_obsidian.core import Store, load_config, digest, now
from paper_obsidian.cnki import parse, normalize, import_file, scan, folders, daily_stats, attach_fulltext
from paper_obsidian.capacity import preview, archived
from paper_obsidian.language import detect, prepare

REAL = Path(__file__).parent/'fixtures/cnki/real-cnki-20261021.enw'


class CNKITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); self.c=load_config("tests/fixtures/config.toml")
        self.c['paths'].update(data=self.root/'data',vault=self.root/'vault')
        self.s=Store(self.c['paths']['data']); self.addCleanup(self.s.db.close)
        self.c['limits']['candidates_per_day']=100
        self.c['capacity']={'candidate_max':200,'formal_max':100}

    def file(self, text, name='input.enw', encoding='utf-8'):
        path=self.root/name; path.write_bytes(text.encode(encoding)); return path

    def record(self, title='隔离测试论文', extra='', year='2026'):
        return f'%0 Journal Article\n%T {title}\n%A 张三\n%J 测试期刊\n'+(f'%D {year}\n' if year else '')+extra

    def test_real_export_encoding_and_missing_year(self):
        records, enc=parse(REAL.read_bytes()); self.assertEqual(len(records),1)
        p=normalize(records[0]['fields'])
        self.assertEqual(p['doi'],'10.16157/j.issn.0258-7998.268230')
        self.assertEqual(p['authors'],['俞发洪','舒征宇','刘颂凯','姚钦'])
        self.assertEqual(p['date'],''); self.assertEqual(p['publication']['status'],'unknown')
        for encoding in ('gb18030','utf-16','utf-8-sig'):
            raw=REAL.read_text('utf-8-sig').encode(encoding)
            self.assertEqual(normalize(parse(raw)[0][0]['fields']),p)
        self.assertEqual(enc,'utf-8-sig')

    def test_no_doi_abstract_year_not_invented(self):
        report=import_file(self.c,self.s,self.file(self.record(year='')))
        p=self.s.get(report['rows'][0]['paper_id'])
        for field in ('doi','abstract','date'): self.assertFalse(p.get(field))
        self.assertFalse(self.s.stage(p['id'],'delivery'))

    def test_content_not_extension_and_foreign_formats_rejected(self):
        self.assertEqual(import_file(self.c,self.s,self.file(self.record(),name='wrong.ris'))['new'],1)
        for content in ('TY  - JOUR\nTI  - Example\nER  -','@article{x,title={Example}}','RT Journal Article\nT1 Example','张三.标题[J].测试期刊,2026.'):
            report=import_file(self.c,self.s,self.file(content))
            self.assertEqual(report['failed'],1); self.assertTrue(report['errors'])
            self.assertTrue(Path(report['original']).exists())

    def test_duplicate_file_records_and_fill_only(self):
        text=self.record(extra='%R 10.1234/test\n')
        path=self.file(text+'\n'+text)
        a=import_file(self.c,self.s,path); self.assertEqual((a['new'],a['duplicate']),(1,1))
        b=import_file(self.c,self.s,path); self.assertTrue(b['repeated_file']); self.assertEqual(b['new'],0)
        pid=a['rows'][0]['paper_id']; before=self.s.get(pid)
        report=import_file(self.c,self.s,self.file(self.record(title='不能覆盖原题',extra='%R 10.1234/test\n%X 新补摘要\n')))
        self.assertEqual(report['updated'],1); self.assertEqual(self.s.get(pid)['title'],before['title'])
        self.assertEqual(self.s.get(pid)['abstract'],'新补摘要')
        self.assertEqual(len(self.s.papers()),1)

    def test_cross_source_doi_preserves_verified(self):
        pid=self.s.ingest({'source':'openalex','aliases':['doi:10.1234/test','openalex:W123'],
            'title':'Verified title','authors':['Verified author'],'date':'2025','venue':'Verified journal',
            'publication':{'status':'published','checked_at':now(),'source':'https://example.org'}})
        report=import_file(self.c,self.s,self.file(self.record(extra='%R 10.1234/test\n%X 新摘要\n')))
        self.assertEqual(report['rows'][0]['paper_id'],pid)
        self.assertEqual(self.s.get(pid)['title'],'Verified title')
        self.assertEqual(self.s.get(pid)['publication']['status'],'published')
        self.assertEqual(set(self.s.get(pid)['sources']),{'cnki','openalex'})
        self.assertEqual(self.s.db.execute('SELECT COUNT(*) FROM provenance WHERE paper_id=?',(pid,)).fetchone()[0],2)

    def test_composite_exact_but_not_fuzzy_or_conflicting_doi(self):
        pid=self.s.ingest({'source':'openalex','aliases':['openalex:W1'],'title':'隔离测试论文',
            'authors':['张三'],'date':'2026-01-01','venue':'测试期刊'})
        a=import_file(self.c,self.s,self.file(self.record()))
        self.assertEqual(a['rows'][0]['paper_id'],pid)
        b=import_file(self.c,self.s,self.file(self.record(title='隔离测试论文研究')))
        self.assertNotEqual(b['rows'][0]['paper_id'],pid)
        c=import_file(self.c,self.s,self.file(self.record(extra='%R 10.1234/one\n')))
        d=import_file(self.c,self.s,self.file(self.record(extra='%R 10.1234/two\n')))
        self.assertNotEqual(c['rows'][0]['paper_id'],d['rows'][0]['paper_id'])
        self.assertIn(pid,d['rows'][0]['related_pending_confirmation'])

    def test_strong_bridge_conflict_never_merges(self):
        a=self.s.ingest({'source':'test','aliases':['doi:10.1234/a'],'title':'A'})
        b=self.s.ingest({'source':'test','aliases':['cnki:CJFD:TEST2026001'],'title':'B'})
        report=import_file(self.c,self.s,self.file(self.record(extra='%R 10.1234/a\n%U https://kns.cnki.net/kcms/detail?dbcode=CJFD&filename=TEST2026001\n')))
        self.assertEqual(report['failed'],1); self.assertEqual(len(self.s.papers()),2)
        self.assertIn('强标识',report['rows'][0]['error'])

    def test_opaque_parameter_not_stable_identifier(self):
        p=normalize(parse(self.record(extra='%U https://kns.cnki.net/article?v=opaque&filename=opaque\n').encode())[0][0]['fields'])
        self.assertEqual(p['cnki_ids'],[]); self.assertIn('v=opaque',p['url'])

    def test_partial_failure_survives_and_daily_limit_retries(self):
        self.c['limits']['candidates_per_day']=1
        path=self.file(self.record('A')+'\n%0 Journal Article\n%A 无标题\n\n'+self.record('B'))
        report=import_file(self.c,self.s,path)
        self.assertEqual((report['new'],report['failed'],report['deferred']),(1,1,1))
        self.c['limits']['candidates_per_day']=2
        report=import_file(self.c,self.s,path)
        self.assertEqual((report['new'],report['failed'],report['deferred']),(2,1,0))

    def test_capacity_archived_reimport_no_restore(self):
        self.c['capacity']['candidate_max']=1
        a=import_file(self.c,self.s,self.file(self.record('A',extra='%R 10.1234/a\n')))
        b=import_file(self.c,self.s,self.file(self.record('B',extra='%R 10.1234/b\n')))
        pid=b['rows'][0]['paper_id']; self.assertTrue(archived(self.s,pid))
        before=copy.deepcopy(archived(self.s,pid))
        import_file(self.c,self.s,self.file(self.record('B',extra='%R 10.1234/b\n%K 新关键词\n')))
        self.assertEqual(archived(self.s,pid),before)
        self.assertEqual(preview(self.c,self.s)['counts']['candidate']['active'],1)
        self.assertEqual(len(self.s.papers()),2)

    def test_scan_failure_isolated_no_paid_calls(self):
        pending=folders(self.c)/'pending'
        (pending/'good.enw').write_bytes(REAL.read_bytes()); (pending/'bad.enw').write_bytes(b'not records')
        with patch('httpx.Client.send',side_effect=AssertionError('network')), patch('subprocess.run',side_effect=AssertionError('process')), patch('paper_obsidian.daily_model.Client.call',side_effect=AssertionError('paid')):
            r=scan(self.c,self.s); self.assertEqual(r['new'],1); self.assertEqual(r['failed'],1)
            r2=scan(self.c,self.s); self.assertEqual(r2['new'],0)
        stat=daily_stats(self.s,now()[:10]); self.assertEqual(stat['new'],1); self.assertEqual(stat['pending_fulltext'],1)
        self.assertEqual(self.s.used('model_calls'),0)
        note=next(self.c['paths']['vault'].rglob('*.md')).read_text()
        self.assertNotIn('OpenAlex提供ISSN',note)
        self.assertIn('本次未请求模型总结',note)

    def test_caj_preserved_not_fake_pdf(self):
        pid=self.s.ingest({'source':'test','aliases':['test:1'],'title':'隔离'})
        path=self.file('CAJ original',name='original.caj')
        r=attach_fulltext(self.c,pid,path,'zh')
        self.assertEqual(r['status'],'待转换/待补全文'); self.assertEqual(Path(r['path']).read_bytes(),path.read_bytes())
        self.assertIsNone(self.s.stage(pid,'pdf'))

    def test_chinese_skip_english_and_unknown_translate(self):
        pid=self.s.ingest({'source':'test','aliases':['test:lang'],'title':'测试'})
        p=self.s.get(pid); source=self.file('%PDF-original',name='source.pdf')
        with patch('paper_obsidian.daily.translate',side_effect=AssertionError('should skip')):
            r=prepare(p,source,{'pages':['中文正文测试'*200]},self.c,self.s)
        self.assertTrue(r['skipped']); self.assertEqual(self.s.stage(pid,'translation')['status'],'skipped')
        for content in ('English original text '*200,'unknown'):
            with patch('paper_obsidian.daily.translate',return_value={'existing':'behavior'}) as translator:
                r=prepare(p,source,{'pages':[content]},self.c,self.s)
                translator.assert_called_once(); self.assertEqual(r,{'existing':'behavior'})

    def test_chinese_cannot_publish_without_review(self):
        from paper_obsidian.daily import publish
        from paper_obsidian.daily_model import DailyError
        pid=self.s.ingest({'source':'test','aliases':['test:zh'],'title':'测试'})
        p=self.s.get(pid); source=self.file('%PDF-original',name='source.pdf')
        parsed={'pages':['中文正文'*200],'source_sha256':digest(source.read_bytes())}
        tr=prepare(p,source,parsed,self.c,self.s)
        with self.assertRaises(DailyError):
            publish(p,{'eligible':True},self.c,self.s,source,parsed,tr)
        self.assertIsNone(self.s.stage(pid,'delivery'))

    def test_reverse_cross_source_composite_dedup(self):
        report=import_file(self.c,self.s,self.file(self.record()))
        pid=report['rows'][0]['paper_id']
        other=self.s.ingest({'source':'openalex','aliases':['openalex:W999'],'title':'隔离测试论文',
            'authors':['张三'],'venue':'测试期刊','date':'2026-01-01'})
        self.assertEqual(other,pid); self.assertEqual(len(self.s.papers()),1)

    def test_pdf_attachment_parser_and_immutable_original(self):
        import pymupdf
        pid=self.s.ingest({'source':'test','aliases':['test:pdf'],'title':'PDF测试'})
        path=self.root/'test.pdf'
        doc=pymupdf.open(); page=doc.new_page()
        content='中文原文测试方法结果实验数据'*100
        page.insert_textbox(pymupdf.Rect(40,40,550,800),content,fontname='china-s',fontsize=10)
        doc.save(path); doc.close()
        original=path.read_bytes()
        with patch('httpx.Client.send',side_effect=AssertionError('network')), patch('paper_obsidian.daily_model.Client.call',side_effect=AssertionError('paid')):
            r=attach_fulltext(self.c,pid,path,'zh')
        self.assertFalse(r.get('failed'),r)
        self.assertEqual(Path(r['path']).read_bytes(),original)
        self.assertEqual(self.s.stage(pid,'pdf')['status'],'success')
        self.assertIsNone(self.s.stage(pid,'fulltext'))

    def test_chinese_complete_delivery_and_review_invalidation(self):
        from paper_obsidian.daily import publish, completed
        from paper_obsidian.daily_model import FIELDS
        from paper_obsidian.evidence import attach_catalog
        from paper_obsidian.briefs import daily_brief
        pid=self.s.ingest({'source':'test','aliases':['test:complete'],'title':'中文原文测试','venue':'测试期刊'})
        p=self.s.get(pid); source=self.file('%PDF-original',name='source.pdf'); sha=digest(source.read_bytes())
        parsed=attach_catalog({'pages':['中文正文测试方法实验结果'*200],'page_count':1,'source_sha256':sha})
        result={'schema_version':'daily-reading-v1','title_zh':'中文原文测试','summary':'隔离测试总结',
            'sections':{k:[{'text':'隔离测试原文','evidence_ids':['p001-b001']}] for k in FIELDS}}
        saved={'result':result,'review':{'reviewer_type':'ai_api'}}
        self.s.record(pid,'daily_summary',sha,'success',saved)
        self.s.record(pid,'auto_review','v','success',saved['review'])
        self.s.record(pid,'pdf',sha,'success',{'path':str(source),'sha256':sha})
        tr=prepare(p,source,parsed,self.c,self.s)
        gate={'eligible':True,'publication_status':'published','evidence':[],'reason':'隔离测试资格'}
        d=publish(p,gate,self.c,self.s,source,parsed,tr,saved)
        self.assertTrue(Path(d['bilingual']).name=='原始中文.pdf')
        self.assertEqual(set(x.name for x in Path(d['note']).parent.iterdir()),{'原始中文.pdf','阅读笔记.md'})
        self.assertEqual(Path(d['bilingual']).read_bytes(),source.read_bytes())
        self.assertIsNotNone(completed(p,self.c,self.s))
        note=Path(d['note']).read_text(); self.assertNotIn('原始英文 PDF',note); self.assertNotIn('双语第',note)
        report={'started':now(),'ended':now(),'delivered':[dict(d,id=pid)],'cached':[],'pending':[],'failures':[]}
        brief=Path(daily_brief(self.c,self.s,report)); self.assertIn('原始中文 PDF',brief.read_text())
        self.s.record(pid,'auto_review','v','failed',{})
        self.assertIsNone(completed(p,self.c,self.s))

    def test_daily_scans_but_offline_render_does_not(self):
        from paper_obsidian.daily import run_daily
        from paper_obsidian.briefs import daily_brief
        pending=folders(self.c)/'pending'; (pending/'real.enw').write_bytes(REAL.read_bytes())
        with patch('paper_obsidian.daily.backup'), patch('paper_obsidian.daily.archive_legacy_attachments'), \
             patch('paper_obsidian.daily.official_upgrade',return_value=None), \
             patch('paper_obsidian.journals.JournalService.annotate'), \
             patch('paper_obsidian.api_accounting.query_balance',return_value={'status':'unconfigured'}), \
             patch('httpx.Client.send',side_effect=AssertionError('network')), \
             patch('paper_obsidian.daily_model.Client.call',side_effect=AssertionError('paid')):
            r=run_daily(self.c,{},fetch=False,metadata_only=True)
        self.assertEqual(r['cnki']['new'],1); self.assertEqual(r['delivered'],[])
        brief=Path(r['brief']); self.assertIn('新增 1',brief.read_text())
        with patch('paper_obsidian.cnki.scan',side_effect=AssertionError('render imported')):
            daily_brief(self.c,self.s,r,actual=False)
        self.assertIn('新增 1',brief.read_text())
        self.assertEqual(daily_stats(self.s,now()[:10])['new'],1)

    def test_import_failure_does_not_block_daily_sources(self):
        from paper_obsidian.daily import run_daily
        with patch('paper_obsidian.daily.backup'), patch('paper_obsidian.daily.archive_legacy_attachments'), \
             patch('paper_obsidian.cnki.scan',side_effect=OSError('bad file')), \
             patch('paper_obsidian.daily.search') as search, \
             patch('httpx.Client.send',side_effect=AssertionError('network')):
            r=run_daily(self.c,{},metadata_only=True)
        self.assertEqual(search.call_count,2 if self.c.get("cross",{}).get("enabled") else 1); self.assertIn('导入失败',r['cnki']['status'])


if __name__=='__main__': unittest.main()
