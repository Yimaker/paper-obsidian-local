import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_obsidian.core import Store, atomic_write, digest, run_lock
from paper_obsidian.library import (reading_path, candidate_path, paper_year, day_of, clean,
    rewrite_text, brief_path, units, MAX_PATH_UNITS, sync_formal_path)
from paper_obsidian.briefs import daily_brief, delivery_events, refresh_latest, offline_replay
from paper_obsidian.vault import managed_write, check_links, START, END


class ReadingLayoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.c={'paths':{'data':self.root/'data','vault':self.root/'vault'}}
        self.s=Store(self.c['paths']['data']);self.addCleanup(self.s.db.close)
    def paper(self,alias='a',title='A / readable: title',year=2026):
        self.s.ingest({'source':'TEST','aliases':[alias],'title':title,'venue':'CVPR',
                       'publication':{'status':'accepted','proceedings_year':year}})
        return self.s.get(digest(alias)[:20])
    def report(self,stamp='2026-10-20T12:00:00+08:00',**kw):
        return dict(started=stamp,sources={},cached=[],delivered=[],pending=[],failures=[],**kw)
    def deliver(self,p,stamp,kind='新论文'):
        note=reading_path(p,self.c,self.s,True);pdf=note.parent/'中英对照.pdf'
        managed_write(note,{'paper_id':p['id'],'title_zh':'中文标题'},'原有正文',self.c)
        atomic_write(pdf,b'unchanged-pdf')
        d={'note':str(note),'bilingual':str(pdf),'folder':note.parent.relative_to(self.c['paths']['vault']).as_posix(),
           'completed_at':stamp,'delivery_kind':kind,'summary':'一句推荐。更多内容','bilingual_sha256':digest(pdf.read_bytes()),
           'metadata_version':digest([None,None]),'review_status':'自动生成','source_sha256':'source'}
        self.s.record(p['id'],'delivery','original-content-version','success',d)
        return note,d
    def test_year_never_uses_retrieval_date(self):
        self.assertEqual(paper_year({'date':'2026-10-20','dates':{'retrieved_at':'2026-10-20'}})[0],'年份待核实')
        self.assertEqual(paper_year({'publication':{'status':'published','proceedings_year':2025},'date':'2026-04-23'})[0],'2025')
        self.assertEqual(paper_year({'dates':{'preprint_published':'2024-12-31'}})[0],'2024')
    def test_beijing_midnight_and_naive_dates(self):
        self.assertEqual(day_of('2026-10-20T15:59:59Z'),'2026.10.20')
        self.assertEqual(day_of('2026-10-20T16:00:00Z'),'2026.10.21')
        self.assertIsNone(day_of('2026-10-19'))
    def test_case_collision_long_title_and_reserved_names(self):
        paths=[]
        for alias,title in [('a','A title'),('b','a TITLE'),('c','A title')]:
            p=self.paper(alias,title);path=reading_path(p,self.c,self.s)
            managed_write(path,{'paper_id':p['id']},'original '+alias,self.c);paths.append(path)
        self.assertEqual(len(set(str(p).casefold() for p in paths)),3)
        self.assertIn('_CVPR',paths[1].stem);self.assertTrue(paths[2].stem.endswith('_CVPR_2'))
        p=self.paper('long','超长😀标题'*100)
        path=reading_path(p,self.c,self.s,True)
        self.assertLessEqual(units(path),MAX_PATH_UNITS)
        self.assertEqual(clean('CON.'),'_CON')
        self.assertEqual([p.read_text().split(START)[1].split(END)[0].strip() for p in paths],['original a','original b','original c'])
    def test_metadata_rename_retains_custom_fields_and_handwriting(self):
        p=self.paper();old=candidate_path(p,self.c,self.s)
        managed_write(old,{'paper_id':p['id'],'custom':7},'original body',self.c)
        atomic_write(old,old.read_text()+'\n手写 [页码](./manual.pdf#page=3)')
        atomic_write(old.parent/'manual.pdf',b'pdf')
        ref=old.parent/'reference.md';atomic_write(ref,'[['+old.stem+'#方法|显示文字]]')
        p['title']='New official title';new=candidate_path(p,self.c,self.s)
        self.assertFalse(old.exists());self.assertIn('custom: 7',new.read_text())
        self.assertIn('手写 [页码]',new.read_text());self.assertIn('#page=3',new.read_text())
        self.assertIn('New official title#方法|显示文字',ref.read_text())
        self.assertFalse(check_links(self.c['paths']['vault'])['missing_links'])
    def test_link_rebase_preserves_anchors_labels_embeds(self):
        v=self.c['paths']['vault'];a=v/'90_候选与待核验'/'old.md';b=v/'10_论文'/'new'/'阅读笔记.md'
        pdf=a.parent/'old.pdf';newpdf=b.parent/'中英对照.pdf'
        raw='[显示](old.pdf#page=7) ![[90_候选与待核验/old#方法|别名]] [[old.pdf#page=2|PDF]]'
        new=rewrite_text(raw,a,b,{str(a.resolve()):b,str(pdf.resolve()):newpdf},v)
        self.assertIn('#page=7',new);self.assertIn('#方法|别名',new);self.assertIn('#page=2|PDF',new)
        self.assertIn('![[10_论文/new/阅读笔记',new)
    def test_cumulative_brief_failed_replay_and_next_day_completion(self):
        a=self.paper();note,d=self.deliver(a,'2026-10-20T23:59:59+08:00')
        r=self.report();r['delivered']=[dict(d,id=a['id'])]
        path=Path(daily_brief(self.c,self.s,r));atomic_write(path,path.read_text()+'\n手写日报原话')
        # Version refresh next day must not count the same paper as a new one.
        d['completed_at']='2026-10-21T13:00:00+08:00'
        self.s.record(a['id'],'delivery','same-content-version','success',d)
        r=self.report();r['failures']=[{'id':a['id'],'title':a['title'],'message':'更新失败'}]
        daily_brief(self.c,self.s,r)
        self.assertIn('新交付 1 篇',path.read_text());self.assertIn('手写日报原话',path.read_text())
        b=self.paper('b','Next day completed')
        self.deliver(b,'2026-10-20T16:00:00Z')
        tomorrow=Path(daily_brief(self.c,self.s,self.report('2026-10-21T12:00:00+08:00')))
        self.assertIn('新交付 1 篇',tomorrow.read_text())
        self.assertIn('2026.10.21', (self.c['paths']['vault']/'00_每日简报/最新简报.md').read_text())
        daily_brief(self.c,self.s,self.report(),actual=False)
        self.assertEqual(self.s.get_state('reading:latest-day'),'2026.10.21')
        no_run=brief_path(self.c,'2026.10.19')
        daily_brief(self.c,self.s,self.report('2026-10-19T12:00:00+08:00'),actual=False)
        self.assertFalse(no_run.exists())
    def test_formal_rename_preserves_content_version_and_links(self):
        p=self.paper();old,d=self.deliver(p,'2026-10-20T12:00:00+08:00')
        original=old.read_bytes()
        p['title']='Updated official title'
        new=sync_formal_path(p,self.c,self.s)
        self.assertFalse(old.exists());self.assertEqual(new.read_bytes(),original)
        row=self.s.stage(p['id'],'delivery')
        self.assertEqual(row['version'],'original-content-version')
        self.assertEqual(Path(json.loads(row['detail'])['note']),new)
    def test_one_run_crossing_midnight_writes_both_real_dates(self):
        a=self.paper();_,da=self.deliver(a,'2026-10-20T23:59:59+08:00')
        b=self.paper('b','Completed after midnight');_,db=self.deliver(b,'2026-10-21T00:00:01+08:00')
        report=self.report();report.update(ended='2026-10-21T00:10:00+08:00',delivered=[dict(da,id=a['id']),dict(db,id=b['id'])])
        path=Path(daily_brief(self.c,self.s,report))
        self.assertEqual(path,brief_path(self.c,'2026.10.21'))
        self.assertIn('新交付 1 篇',brief_path(self.c,'2026.10.20').read_text())
        self.assertIn('新交付 1 篇',path.read_text())
        self.assertEqual(self.s.get_state('reading:latest-day'),'2026.10.21')
    def test_legacy_translation_reuses_delivery_without_old_paths(self):
        from tools.translate_one import existing_reading_delivery
        p=self.paper();_,d=self.deliver(p,'2026-10-20T12:00:00+08:00')
        with patch('tools.translate_one.load_config',return_value=self.c):
            result=existing_reading_delivery({'paper_id':p['id']})
        self.assertEqual(result['output'],d['bilingual'])
        self.assertEqual(result['network_attempts_this_run'],0)
        self.assertFalse((self.c['paths']['vault']/'20_附件').exists())
    def test_maintenance_blocks_all_normal_writers(self):
        atomic_write(self.c['paths']['data']/'MAINTENANCE','test')
        with self.assertRaises(RuntimeError):
            with run_lock(self.c['paths']['data']):pass
        with run_lock(self.c['paths']['data'],maintenance=True):pass
    def test_offline_replay_cannot_touch_services_or_old_directories(self):
        p=self.paper();note,d=self.deliver(p,'2026-10-20T12:00:00+08:00')
        with patch('paper_obsidian.sources.HTTP',side_effect=AssertionError('network forbidden')), \
             patch('paper_obsidian.daily.Client',side_effect=AssertionError('model forbidden')), \
             patch('paper_obsidian.daily.translate',side_effect=AssertionError('translation forbidden')):
            result=offline_replay(self.c)
        self.assertEqual(result['cached'],[p['id']])
        self.assertEqual(result['network_requests'],0)
        self.assertFalse((self.c['paths']['vault']/'20_附件').exists())
        self.assertFalse((self.c['paths']['vault']/'10_论文笔记').exists())
        self.assertEqual(len(list((self.c['paths']['vault']/'00_每日简报').glob('*'))),0)


if __name__=='__main__':unittest.main()
