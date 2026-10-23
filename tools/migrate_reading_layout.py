"""Resumable local migration. Run with daily task disabled and MAINTENANCE set.

No service configuration is loaded, and no model/network/translation is invoked.
The plan, copy journal and whole-vault/SQLite backups are retained on failure.
"""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))

import json
import re
import sqlite3
import yaml
import zipfile

from paper_obsidian.core import ROOT, Store, load_config, atomic_write, digest, now, run_lock
from paper_obsidian.library import (portable,reading_path,naming_meta,rewrite_text,
                                    brief_path,day_of,meta)
from paper_obsidian.briefs import delivery_events,daily_brief,refresh_latest


def add_meta(raw, fields):
    if not raw.startswith('---\n'): raise ValueError('缺少受管理元数据，拒绝改写')
    _,head,tail=raw.split('---\n',2)
    existing=yaml.safe_load(head) or {}
    # Only owned new metadata fields. Existing custom frontmatter stays literal.
    additions={k:v for k,v in fields.items() if k not in existing}
    if not additions:return raw
    return '---\n'+head+yaml.safe_dump(additions,allow_unicode=True,sort_keys=False,width=1000)+'---\n'+tail


def build_plan(c,s):
    v=c['paths']['vault']; events=delivery_events(s);moves=[]
    def move(a,b,kind,pid=None):
        if a.resolve()==b.resolve():return
        if not a.exists():raise FileNotFoundError(a)
        if b.exists():raise ValueError('迁移目标已存在：'+str(b))
        moves.append({'from':str(a),'to':str(b),'kind':kind,'paper_id':pid,'sha256':digest(a.read_bytes())})
    for p in sorted(s.papers(),key=lambda x:x['id']):
        row=s.stage(p['id'],'delivery')
        if row and row['status']=='success':
            d=json.loads(row['detail']);new=reading_path(p,c,s,True)
            move(portable(d['note']),new,'formal-note',p['id'])
            move(portable(d['bilingual']),new.parent/'中英对照.pdf','formal-pdf',p['id'])
    for old in sorted((v/'90_候选与待核验').glob('*.md')):
        pid=meta(old).get('paper_id')
        if not pid or not s.get(pid):raise ValueError('非项目候选，需保留：'+str(old))
        if pid in events:raise ValueError('正式与候选同ID，不自动合并：'+pid)
        move(old,reading_path(s.get(pid),c,s),'candidate',pid)
    real_days=sorted(set(e['day'] for e in events.values()))
    for old in sorted((v/'00_每日简报').glob('*.md')):
        if old.name=='最新简报.md':continue
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}\.md',old.name) and old.stem.replace('-','.') in real_days:
            move(old,brief_path(c,old.stem.replace('-','.')),'daily')
        else:move(old,ROOT/'docs/history/vault-reports'/old.name,'report')
    # Migrate only files proven project-owned; user-created files remain in place.
    for directory in ('10_论文笔记','20_附件'):
        for old in (v/directory).glob('*'):
            if not old.is_file():continue
            m=re.match(r'^([0-9a-f]{20})(?:\.md|-(?:original|bilingual|evidence).*)$',old.name)
            if not m or not s.get(m[1]):continue
            target=c['paths']['data']/'archive'/m[1]/old.name
            if target.exists():target=target.with_name(target.stem+'-'+digest(old.read_bytes())[:12]+target.suffix)
            move(old,target,'legacy',m[1])
    return {'created':now(),'first_formal_day':real_days[0] if real_days else None,'actual_days':real_days,'moves':moves}


def migrate():
    c=load_config();data=c['paths']['data'];v=c['paths']['vault']
    if not (data/'MAINTENANCE').exists():raise RuntimeError('须先停写、禁用任务、备份并建立维护标记')
    folder=data/'layout-migration';folder.mkdir(parents=True,exist_ok=True)
    with run_lock(data,maintenance=True):
        s=Store(data)
        try:
            planpath=folder/'plan.json'
            if planpath.exists():plan=json.loads(planpath.read_text('utf-8'))
            else:
                plan=build_plan(c,s)
                atomic_write(planpath,json.dumps(plan,ensure_ascii=False,indent=2))
            journalpath=folder/'progress.json'
            progress=json.loads(journalpath.read_text('utf-8')) if journalpath.exists() else {'copied':[],'finished':False}
            if progress['finished']:
                print('migration already completed; no changes');return plan
            def checkpoint():atomic_write(journalpath,json.dumps(progress,ensure_ascii=False,indent=2))
            for i,m in enumerate(plan['moves']):
                if i in progress['copied']:continue
                src,dst=Path(m['from']),Path(m['to'])
                if digest(src.read_bytes())!=m['sha256']:raise ValueError('停写后原件变化：'+str(src))
                if dst.exists() and digest(dst.read_bytes())!=m['sha256']:raise ValueError('目标冲突，保留现场：'+str(dst))
                atomic_write(dst,src.read_bytes());progress['copied'].append(i);checkpoint()
            mapping={m['from']:Path(m['to']) for m in plan['moves']}
            origins={str(Path(m['to']).resolve()):Path(m['from']) for m in plan['moves']}
            sources={str(Path(m['from']).resolve()) for m in plan['moves']}
            def original_text(origin):
                if origin.exists():return origin.read_text('utf-8')
                # A crash can occur after some verified sources were removed.
                # Recover their original bytes from the required stopped backup.
                rel=origin.relative_to(v).as_posix()
                expected=next(m['sha256'] for m in plan['moves'] if Path(m['from'])==origin)
                for zp in sorted((data/'backups').glob('layout-*/vault-before.zip'),reverse=True):
                    with zipfile.ZipFile(zp) as z:
                        if rel in z.namelist():
                            raw=z.read(rel)
                            if digest(raw)==expected:return raw.decode('utf-8')
                raise ValueError('无法从停写快照恢复原笔记：'+str(origin))
            # Read from untouched originals on every resume; deterministic and idempotent.
            docs=list(ROOT.glob('*.md'))+list((ROOT/'docs').rglob('*.md'))
            notes=list(v.rglob('*.md'))+docs
            for note in dict.fromkeys(notes):
                if str(note.resolve()) in sources:continue
                origin=origins.get(str(note.resolve()),note)
                original=original_text(origin) if origin!=note else note.read_text('utf-8')
                raw=rewrite_text(original,origin,note,mapping,v)
                pid=(yaml.safe_load(original.split('---\n',2)[1]) or {}).get('paper_id') if original.startswith('---\n') else None
                if pid and (paper:=s.get(pid)) and (note.is_relative_to(v/'10_论文') or note.is_relative_to(v/'90_候选与待核验')):
                    fields=naming_meta(paper)
                    created=s.db.execute('SELECT created FROM papers WHERE id=?',(pid,)).fetchone()
                    fields['first_discovered_at']=created[0]
                    if pid in delivery_events(s):fields['first_delivered_at']=delivery_events(s)[pid]['first_completed_at']
                    raw=add_meta(raw,fields)
                atomic_write(note,raw,data/'backups')
            # Exact paths only; never touch paper bodies, identifiers, content versions,
            # request payloads, model fingerprints or translation caches.
            aliases={}
            for old,new in mapping.items():
                oldpath=Path(old)
                variants={str(oldpath),oldpath.as_posix()}
                if re.match(r'^[A-Za-z]:',old): variants.add('/mnt/'+old[0].lower()+'/'+old[3:].replace('\\','/'))
                try:variants.add(oldpath.relative_to(v).as_posix())
                except ValueError:pass
                for a in variants:aliases[a]=str(new) if a.startswith(('/mnt/','C:','c:')) else new.relative_to(v).as_posix() if new.is_relative_to(v) else str(new)
            def update(value):
                if isinstance(value,dict):return {k:update(v) for k,v in value.items()}
                if isinstance(value,list):return [update(v) for v in value]
                if isinstance(value,str):
                    if value in aliases:return aliases[value]
                    # A delivery-folder state refers to the parent, not a file.
                    for m in plan['moves']:
                        if m['kind']=='formal-note':
                            a=Path(m['from']).parent.relative_to(v).as_posix()
                            if value==a:return Path(m['to']).parent.relative_to(v).as_posix()
                return value
            with s.db:
                for row in s.db.execute('SELECT paper_id,stage,detail FROM stages').fetchall():
                    value=json.loads(row['detail']);new=update(value)
                    if row['stage']=='delivery':
                        event=delivery_events(s).get(row['paper_id'])
                        if event:new['first_completed_at']=event['first_completed_at']
                    if new!=value:s.db.execute('UPDATE stages SET detail=? WHERE paper_id=? AND stage=?',(json.dumps(new,ensure_ascii=False),row['paper_id'],row['stage']))
                for row in s.db.execute('SELECT key,value FROM state').fetchall():
                    value=json.loads(row['value']);new=update(value)
                    if new!=value:s.db.execute('UPDATE state SET value=? WHERE key=?',(json.dumps(new,ensure_ascii=False),row['key']))
                for row in s.db.execute('SELECT id,report FROM runs WHERE report IS NOT NULL').fetchall():
                    value=json.loads(row['report']);new=update(value)
                    if new!=value:s.db.execute('UPDATE runs SET report=? WHERE id=?',(json.dumps(new,ensure_ascii=False),row['id']))
            for path in list((data/'daily-runs').glob('*.json'))+[data/'last-run.json']:
                if path.exists():
                    value=json.loads(path.read_text('utf-8'));new=update(value)
                    if new!=value:atomic_write(path,json.dumps(new,ensure_ascii=False,indent=2),data/'backups')
            # Retain complete old bodies in backup. Only the proven daily page is
            # rendered into the concise cumulative reading view.
            s.set_state('reading:actual-brief-days',plan['actual_days'])
            report=s.get_state('last_run',{})
            for day in plan['actual_days']:daily_brief(c,s,report,day=day,actual=False)
            refresh_latest(c,s)
            for m in plan['moves']:
                src,dst=Path(m['from']),Path(m['to'])
                if not dst.exists():raise ValueError('目标缺失，保留原件')
                if m['kind']=='formal-pdf' and digest(dst.read_bytes())!=m['sha256']:raise ValueError('双语PDF哈希改变')
                if src.exists():
                    if digest(src.read_bytes())!=m['sha256']:raise ValueError('原件已变化，拒绝移走')
                    if not src.resolve().is_relative_to(v.resolve()):raise ValueError('源路径越界')
                    src.unlink()
            # Remove empty managed directories only, no recursive deletion.
            for directory in ('10_论文','10_论文笔记','20_附件','00_每日简报'):
                base=v/directory
                if base.exists():
                    for d in sorted((x for x in base.rglob('*') if x.is_dir()),key=lambda x:len(x.parts),reverse=True):
                        if not any(d.iterdir()):d.rmdir()
                    if directory in ('10_论文笔记','20_附件') and not any(base.iterdir()):base.rmdir()
            progress.update(finished=True,ended=now());checkpoint()
            print(json.dumps({'first_formal_day':plan['first_formal_day'],'counts':{k:sum(m['kind']==k for m in plan['moves']) for k in ('daily','formal-note','formal-pdf','candidate','report','legacy')},'plan':str(planpath)},ensure_ascii=False,indent=2))
            return plan
        finally:s.db.close()


if __name__=='__main__':migrate()
