import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper_obsidian.core import Store, atomic_write, digest, run_lock
from paper_obsidian.capacity import (apply, preview, archived, dormant, command,
    inventory, recover, evidence, fingerprint)
from paper_obsidian.library import reading_path, note_for_id, meta
from paper_obsidian.vault import managed_write, render_paper, check_links


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = {'paths': {'vault': self.root/'vault', 'data': self.root/'data'},
                  'search': {'keywords': ['machine learning', 'multimodal'], 'exclude': ['protein']}}
        self.s = Store(self.c['paths']['data']); self.addCleanup(self.s.db.close)
        self.gate = {'eligible': False, 'publication_status': 'unknown', 'reason': 'fixture', 'evidence': []}

    def paper(self, i, formal=False, title=None, pin=False):
        pid = self.s.ingest({'source': 'TEST', 'aliases': ['fixture:'+str(i)],
            'title': title or 'Paper '+str(i), 'publication': {'status': 'unknown'}}, cap=10000)
        p = self.s.get(pid); path = reading_path(p, self.c, self.s, formal)
        managed_write(path, {'paper_id': pid, 'capacity_pin': pin}, 'Saved source and review', self.c)
        if formal:
            pdf = path.parent/'中英对照.pdf'; atomic_write(pdf, b'original PDF bytes')
            d = {'note': str(path), 'bilingual': str(pdf), 'folder': path.parent.relative_to(self.c['paths']['vault']).as_posix(),
                 'bilingual_sha256': digest(pdf.read_bytes()), 'source_sha256':'source',
                 'metadata_version':digest([None,None]), 'completed_at':'2026-10-20T12:00:00+08:00',
                 'first_completed_at':'2026-10-20T12:00:00+08:00'}
            self.s.record(pid,'delivery','content-v1','success',d)
        return p, path

    def current(self):
        return {(x['area'],x['id']) for x in inventory(self.c,self.s)[0] if not x['archived']}

    def small(self, n=1):
        self.c['capacity'] = {'candidate_max':n, 'formal_max':n}

    def test_candidate_200_to_201_new_lower_higher_tied(self):
        for variant in ('lower','higher','tie'):
            with self.subTest(variant=variant):
                # Independent isolated fixtures; never production paths.
                self.c['paths']['vault'] = self.root/variant
                self.c['paths']['data'] = self.root/('data-'+variant)
                self.s = Store(self.c['paths']['data']); self.addCleanup(self.s.db.close)
                for i in range(200): self.paper(i, title=('machine learning ' if variant=='lower' else '')+str(i))
                before = self.current()
                p, _ = self.paper(201, title='machine learning new' if variant=='higher' else 'new')
                plan = apply(self.c,self.s,incumbents=before)
                self.assertEqual(preview(self.c,self.s)['counts']['candidate']['active'],200)
                self.assertEqual(len(plan['actions']),1)
                self.assertEqual(bool(archived(self.s,p['id'])),variant!='higher')
                self.assertFalse(apply(self.c,self.s)['actions'])

    def test_formal_100_to_101(self):
        for i in range(100): self.paper(i,True)
        before = self.current(); p, note = self.paper(101,True,title='machine learning priority')
        plan = apply(self.c,self.s,incumbents=before)
        self.assertEqual(preview(self.c,self.s)['counts']['formal']['active'],100)
        self.assertEqual(len(plan['actions']),1); self.assertIsNone(archived(self.s,p['id']))
        victim=plan['actions'][0]['id']; row=self.s.stage(victim,'delivery')
        self.assertEqual(row['version'],'content-v1'); d=json.loads(row['detail'])
        self.assertTrue(Path(d['note']).is_file()); self.assertTrue(Path(d['bilingual']).is_file())
        self.assertEqual(d['first_completed_at'],'2026-10-20T12:00:00+08:00')
        self.assertEqual(preview(self.c,self.s)['historical_delivered'],101)

    def test_formal_new_lower_and_tied_keep_incumbent(self):
        for mode in ('lower','tie'):
            with self.subTest(mode=mode):
                self.c['paths']['vault']=self.root/mode
                self.c['paths']['data']=self.root/('db-'+mode)
                self.s=Store(self.c['paths']['data']);self.addCleanup(self.s.db.close)
                for i in range(100):self.paper(i,True,title=('machine learning ' if mode=='lower' else '')+str(i))
                before=self.current();p,_=self.paper(101,True)
                apply(self.c,self.s,incumbents=before)
                self.assertTrue(archived(self.s,p['id']))
                self.assertEqual(preview(self.c,self.s)['counts']['formal']['active'],100)

    def test_candidate_promotion_preserves_manual_and_cannot_bypass_formal_cap(self):
        from paper_obsidian.daily import publish, completed
        from paper_obsidian.daily_model import FIELDS
        from paper_obsidian.evidence import attach_catalog
        self.small();self.paper(1,True,pin=True)
        p,path=self.paper(2,title='machine learning')
        atomic_write(path,path.read_text()+'\n晋升前的手写原话\n')
        source=self.root/'source.pdf';atomic_write(source,b'original')
        bilingual=self.root/'translation.pdf';atomic_write(bilingual,b'translation')
        parsed=attach_catalog({'pages':['Source fact.\n'],'page_count':1})
        result={'schema_version':'daily-reading-v1','title_zh':'测试','summary':'source',
                'summary_evidence_ids':['p001-b001'],
                'sections':{k:[{'text':'fact','evidence_ids':['p001-b001']}] for k in FIELDS}}
        d=publish(p,dict(self.gate,eligible=True),self.c,self.s,source,parsed,
                  {'path':str(bilingual)},saved={'result':result,'review':{'reviewer_type':'ai_api'}})
        self.assertIn('80_归档/论文归档',d['note'])
        self.assertIn('晋升前的手写原话',Path(d['note']).read_text())
        self.assertFalse(path.exists());self.assertIsNotNone(completed(p,self.c,self.s))
        self.assertEqual(preview(self.c,self.s)['counts']['formal']['active'],1)
        self.assertEqual(preview(self.c,self.s)['counts']['candidate']['active'],0)
        self.assertFalse(check_links(self.c['paths']['vault'])['missing_links'])

    def test_archived_candidate_promotion_does_not_leave_stale_archive_index(self):
        self.small();p,path=self.paper(1);self.paper(2,title='machine learning')
        apply(self.c,self.s)
        # Reuse the real promotion test's setup via actual publish with this archived candidate.
        from paper_obsidian.daily import publish
        from paper_obsidian.daily_model import FIELDS
        from paper_obsidian.evidence import attach_catalog
        src=self.root/'src.pdf';atomic_write(src,b'source')
        parsed=attach_catalog({'pages':['Source.'],'page_count':1})
        result={'schema_version':'daily-reading-v1','title_zh':'测试','summary':'source',
                'sections':{k:[{'text':'fact','evidence_ids':['p001-b001']}] for k in FIELDS}}
        d=publish(p,dict(self.gate,eligible=True),self.c,self.s,src,parsed,{'path':str(src)},
                  saved={'result':result,'review':{'reviewer_type':'ai_api'}})
        self.assertIsNone(archived(self.s,p['id']));self.assertTrue(Path(d['note']).is_file())
        self.assertFalse(preview(self.c,self.s)['problems'])

    def test_interrupted_admission_retains_same_score_incumbent(self):
        self.small();p,_=self.paper(1);before=self.current()
        q,_=self.paper(2)
        self.s.set_state('capacity:admission:'+q['id'],list(before))
        apply(self.c,self.s)
        self.assertTrue(archived(self.s,q['id']))
        self.assertIsNone(archived(self.s,p['id']))

    def test_destination_conflict_does_not_overwrite_or_restore_over_cap(self):
        from paper_obsidian.capacity import destination
        self.small();p,_=self.paper(1);self.paper(2,title='machine learning')
        plan=preview(self.c,self.s);dst=destination(self.c,self.s,plan['actions'][0])
        atomic_write(dst,b'conflicting user file')
        result=apply(self.c,self.s)
        self.assertTrue(result['problems']);self.assertFalse(result['actions'])
        self.assertEqual(dst.read_bytes(),b'conflicting user file')

    def test_pin_and_overflow_pause(self):
        self.small(); p,_=self.paper(1,pin=True); q,_=self.paper(2,title='machine learning')
        apply(self.c,self.s); self.assertIsNone(archived(self.s,p['id']))
        self.assertTrue(archived(self.s,q['id']))
        self.paper(3,pin=True)
        plan=apply(self.c,self.s)
        self.assertFalse(plan['actions']); self.assertTrue(plan['problems'])
        self.assertEqual(plan['counts']['candidate']['pinned'],2)

    def test_default_manual_heading_does_not_pin_real_handwriting_survives(self):
        self.small(); p,path=self.paper(1); q,_=self.paper(2,title='machine learning')
        with path.open('a',encoding='utf-8') as f:f.write('\n真实手写记录，不允许丢失。\n')
        apply(self.c,self.s)
        dest=note_for_id(self.c,self.s,p['id'])
        self.assertIn('真实手写记录，不允许丢失。',dest.read_text())
        self.assertFalse(meta(dest)['capacity_pin']); self.assertNotEqual(dest,path)

    def test_repeat_archive_never_reappears_or_reprocesses(self):
        self.small(); p,_=self.paper(1); self.paper(2,title='machine learning')
        apply(self.c,self.s); a=archived(self.s,p['id'])
        self.assertTrue(dormant(p,self.c,self.s))
        for _ in range(3):
            note=render_paper(p,self.gate,self.c,self.s)
            self.assertEqual(str(note.relative_to(self.c['paths']['vault'])),a['note'])
            self.assertFalse(apply(self.c,self.s)['actions'])

    def test_archive_material_change_and_explicit_restore_check_capacity(self):
        self.small(); p,_=self.paper(1); q,_=self.paper(2,title='machine learning')
        apply(self.c,self.s)
        self.assertFalse(apply(self.c,self.s,restore=p['id'])['actions'])
        archived_path=note_for_id(self.c,self.s,p['id'])
        managed_write(archived_path,{'capacity_reading_value':'high','capacity_reading_reason':'my thesis'},'same',self.c)
        # Topic priority still wins; high value is never a qualification override.
        self.assertFalse(apply(self.c,self.s)['actions'])
        self.s.ingest({'source':'TEST','aliases':['fixture:1'],'title':'machine learning new'},cap=10000)
        self.assertFalse(dormant(self.s.get(p['id']),self.c,self.s))
        plan=apply(self.c,self.s)
        self.assertEqual({x['action'] for x in plan['actions']},{'archive','restore'})
        self.assertIsNone(archived(self.s,p['id'])); self.assertTrue(archived(self.s,q['id']))
        self.assertFalse(apply(self.c,self.s)['actions'])

    def test_metadata_timestamp_only_does_not_trigger(self):
        self.small(); p,_=self.paper(1); self.paper(2,title='machine learning');apply(self.c,self.s)
        self.s.ingest({'source':'TEST','aliases':['fixture:1'],'dates':{'retrieved_at':'later'}},cap=10000)
        self.assertTrue(dormant(self.s.get(p['id']),self.c,self.s))

    def test_assets_links_db_and_history_preserved(self):
        self.small(); p,note=self.paper(1,True); self.paper(2,True,title='machine learning')
        original=note.parent/'原文.pdf';atomic_write(original,b'raw original')
        extra=note.parent/'review.json';atomic_write(extra,b'{"review":"saved"}')
        atomic_write(note,note.read_text()+'\n手写 [原文](原文.pdf#page=2)\n')
        history=self.c['paths']['vault']/'00_每日简报/old.md'
        atomic_write(history,'历史交付 1 篇\n[['+note.relative_to(self.c['paths']['vault']).as_posix()+'|阅读]]')
        self.s.record(p['id'],'pdf','source-v1','success',{'path':str(original)})
        with self.s.db:self.s.db.execute('INSERT INTO runs(started,ended,report) VALUES (?,?,?)',
            ('historical','historical',json.dumps({'delivered':[{'note':str(note),'id':p['id']}],'count':1})))
        # Force a more relevant paper to evict despite readiness.
        before_source=original.read_bytes();plan=apply(self.c,self.s)
        dest=note_for_id(self.c,self.s,p['id'],True)
        self.assertEqual((dest.parent/'原文.pdf').read_bytes(),before_source)
        self.assertEqual((dest.parent/'review.json').read_bytes(),extra.read_bytes() if extra.exists() else b'{"review":"saved"}')
        self.assertIn('手写 [原文]',dest.read_text());self.assertIn('历史交付 1 篇',history.read_text())
        self.assertIn('80_归档',history.read_text()); self.assertFalse(check_links(self.c['paths']['vault'])['missing_links'])
        self.assertEqual(Path(json.loads(self.s.stage(p['id'],'pdf')['detail'])['path']),dest.parent/'原文.pdf')
        historic=json.loads(self.s.db.execute('SELECT report FROM runs').fetchone()[0])
        self.assertEqual(historic['count'],1);self.assertEqual(historic['delivered'][0]['note'],str(dest))
        receipt=json.loads(Path(plan['receipt']).read_text());self.assertTrue(receipt['assets'])
        self.assertTrue((Path(receipt['backup'])/'papers.sqlite3').exists())

    def test_failure_rolls_back_files_markdown_and_database(self):
        self.small(); p,note=self.paper(1,True);self.paper(2,True,title='machine learning')
        original={str(f):f.read_bytes() for f in self.c['paths']['vault'].rglob('*') if f.is_file()}
        stage=self.s.stage(p['id'],'delivery')
        real=self.s.set_state
        def fail(key,value):
            if key.startswith('capacity:archive:'):raise OSError('injected disk failure')
            return real(key,value)
        with patch.object(self.s,'set_state',side_effect=fail):
            with self.assertRaises(OSError):apply(self.c,self.s)
        self.assertEqual(original,{str(f):f.read_bytes() for f in self.c['paths']['vault'].rglob('*') if f.is_file()})
        self.assertEqual(stage,self.s.stage(p['id'],'delivery'))
        self.assertFalse((self.c['paths']['data']/'capacity/pending.json').exists())
        self.assertEqual(len(apply(self.c,self.s)['actions']),1)

    def test_process_death_journal_recovers_before_next_write(self):
        self.small();p,note=self.paper(1);self.paper(2,title='machine learning')
        with patch.object(self.s,'set_state',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):apply(self.c,self.s)
        self.assertTrue((self.c['paths']['data']/'capacity/pending.json').exists())
        self.assertTrue(recover(self.c,self.s));self.assertTrue(note.exists())
        self.assertFalse(archived(self.s,p['id']));self.assertEqual(len(apply(self.c,self.s)['actions']),1)

    def test_duplicate_and_unknown_id_reported_no_moves(self):
        self.small();p,note=self.paper(1);self.paper(2)
        shutil.copy2(note,note.with_name('duplicate.md'))
        plan=apply(self.c,self.s)
        self.assertEqual(plan['counts']['candidate']['active'],2)
        self.assertTrue(plan['problems']); self.assertFalse(plan['actions'])

    def test_read_only_preview_does_not_write(self):
        self.paper(1)
        before={str(f):f.read_bytes() for f in self.root.rglob('*') if f.is_file()}
        command(self.c,'preview');command(self.c,'status')
        after={str(f):f.read_bytes() for f in self.root.rglob('*') if f.is_file()}
        self.assertEqual(before,after)

    def test_formal_restore_requires_existing_gate_and_completed_content(self):
        self.small();p,_=self.paper(1,True);self.paper(2,True,title='machine learning')
        apply(self.c,self.s)
        plan=apply(self.c,self.s,restore=p['id'])
        self.assertFalse(plan['actions']); self.assertTrue(archived(self.s,p['id']))
        whitelist=self.root/'whitelist.json'
        atomic_write(whitelist,json.dumps({'journals':[],'conferences':[{'acronym':'CVPR','grade':'A',
            'ccf_year':2026,'source':'https://www.ccf.org.cn/test', 'checked_at':'2026-01-01T00:00:00+08:00'}]}))
        self.c['paths']['whitelist']=whitelist
        self.s.ingest({'source':'TEST','aliases':['fixture:1'],'venue_kind':'conference','venue':'CVPR',
            'publication':{'status':'accepted','main_track':True,'paper_type':'full',
                'source':'https://openaccess.thecvf.com/test','checked_at':'2026-01-01T00:00:00+08:00'}},cap=10000)
        path=note_for_id(self.c,self.s,p['id'],True)
        managed_write(path,{'capacity_pin':True},'source',self.c)
        plan=apply(self.c,self.s,restore=p['id'])
        self.assertEqual(len(plan['actions']),2)
        self.assertEqual(preview(self.c,self.s)['counts']['formal']['active'],1)

    def test_journal_refresh_and_candidate_generation_enforce_limit(self):
        from paper_obsidian.journals import refresh_notes
        self.small();self.paper(1,title='machine learning')
        pid=self.s.ingest({'source':'TEST','aliases':['new-journal'],'title':'new','venue_kind':'journal'},cap=10000)
        with patch('paper_obsidian.journals.JournalService.annotate'):
            refresh_notes(self.c,{},self.s,{'journals':[],'conferences':[]},offline=True)
        self.assertTrue(archived(self.s,pid))
        self.assertEqual(preview(self.c,self.s)['counts']['candidate']['active'],1)

    def test_pin_cli_does_not_touch_manual_region(self):
        p,path=self.paper(1); raw=path.read_text().split('<!-- PAPER-AUTO:END -->')[1]
        command(self.c,'pin',p['id']); self.assertTrue(meta(path)['capacity_pin'])
        command(self.c,'unpin',p['id']);self.assertFalse(meta(path)['capacity_pin'])
        self.assertEqual(path.read_text().split('<!-- PAPER-AUTO:END -->')[1],raw)

    def test_same_process_publication_lock_can_nest_but_new_run_cannot(self):
        p,_=self.paper(1)
        with run_lock(self.c['paths']['data']):
            render_paper(p,self.gate,self.c,self.s)
            with self.assertRaises(RuntimeError):
                with run_lock(self.c['paths']['data']):pass

    def test_missing_data_not_quality_score_and_default_not_high(self):
        p,_=self.paper(1)
        e=evidence(p,self.c,self.s,{})
        self.assertIsNone(e['publication_date']);self.assertEqual(e['user_reading_value'],'unspecified')
        self.assertIn('未知',e['quality_assessment'])

    def test_same_basename_wiki_links_do_not_jump_to_other_paper(self):
        self.small();p,old=self.paper(1,True);q,retained=self.paper(2,True,title='machine learning')
        atomic_write(retained,retained.read_text()+'\n[[阅读笔记#own|自己的笔记]]')
        shared=old.parent/'shared.pdf';atomic_write(shared,b'pdf')
        atomic_write(old,old.read_text()+'\n![[shared.pdf#page=2|自己的附件]]')
        apply(self.c,self.s)
        self.assertIn('[[阅读笔记#own|自己的笔记]]',retained.read_text())
        moved=note_for_id(self.c,self.s,p['id'],True)
        self.assertIn('80_归档/论文归档',moved.read_text())
        self.assertIn('shared.pdf#page=2|自己的附件',moved.read_text())

    def test_external_edit_after_interruption_stops_recovery(self):
        self.small();p,_=self.paper(1);self.paper(2,title='machine learning')
        with patch.object(self.s,'set_state',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):apply(self.c,self.s)
        pending=json.loads((self.c['paths']['data']/'capacity/pending.json').read_text())
        target=Path(pending['moves'][0][1]);atomic_write(target,target.read_text()+'\nNew user handwriting')
        with self.assertRaises(ValueError):recover(self.c,self.s)
        self.assertIn('New user handwriting',target.read_text())


if __name__ == '__main__':unittest.main()
