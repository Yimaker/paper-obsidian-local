"""Deterministic reading-area retention. Uses the existing Store, never a second DB.

All mutations run under the same project lock as daily. Migration journals are
write-ahead and roll back on interruption before another writer may continue.
"""
from __future__ import annotations

import functools
import inspect
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

from .core import Store, atomic_write, digest, now, run_lock
from .library import meta, portable, rewrite_text, reading_path, fit_name

VERSION = 'retention-v1'
AREAS = {'candidate': '90_候选与待核验', 'formal': '10_论文', 'cross': '20_交叉领域论文'}
ARCHIVES = {'candidate': '80_归档/候选归档', 'formal': '80_归档/论文归档', 'cross': '80_归档/交叉领域归档'}


def limits(c):
    cfg = c.get('capacity', {})
    if cfg.get('rule_version', VERSION) != VERSION:
        raise ValueError('未知容量排序版本')
    result = {k: cfg.get(k + '_max', n) for k, n in [('candidate', 200), ('formal', 100), ('cross', 50)]}
    if any(type(n) is not int or n < 1 for n in result.values()):
        raise ValueError('容量必须为正整数')
    return result


def archived(s, pid):
    return s.get_state('capacity:archive:' + pid)


def archive_note(c, s, pid, formal=None):
    a = archived(s, pid)
    if a and (formal is None or (a['area'] in ('formal','cross')) == formal):
        p = c['paths']['vault'] / a['note']
        if p.is_file() and meta(p).get('paper_id') == pid:
            return p
        raise ValueError('归档索引与文件冲突，拒绝重建')
    return None


def evidence(p, c, s, metadata):
    """Positive facts about suitability/readiness, not an invented quality score."""
    text = (p.get('title', '') + ' ' + p.get('abstract', '')).lower()
    search = c.get('search', {})
    from .branches import primary
    if primary(s,p['id'])=='cross': search=dict(search,keywords=c.get('cross',{}).get('keywords',[]),exclude=c.get('cross',{}).get('exclude',[]))
    hits = sorted(k for k in search.get('keywords', []) if k.lower() in text)
    excluded = sorted(k for k in search.get('exclude', []) if k.lower() in text)
    gate = {'eligible': False, 'evidence': []}
    if c['paths'].get('whitelist'):
        from .eligibility import reading_classify as classify, load_whitelist
        gate = classify(p, load_whitelist(c['paths']['whitelist']))
    pub = p.get('publication', {})
    verified = bool(pub.get('checked_at') and pub.get('source') and pub.get('status') in ('accepted', 'published'))
    stages = {k: s.stage(p['id'], k) for k in ('pdf', 'fulltext', 'daily_summary', 'auto_review', 'translation', 'delivery')}
    ready = []
    for k, row in stages.items():
        if not row or row['status'] != 'success': continue
        d = json.loads(row['detail'])
        if k in ('pdf', 'translation') and not (d.get('path') and portable(d['path']).is_file()): continue
        ready.append(k)
    value = metadata.get('capacity_reading_value')
    reason = metadata.get('capacity_reading_reason')
    # Only an explicit, explained user preference affects retention.
    interest = value == 'high' and isinstance(reason, str) and bool(reason.strip())
    resources = sorted(k for k in ('code_url', 'data_url', 'project_url')
                       if str(p.get(k, '')).startswith('https://'))
    dates = p.get('dates', {})
    date = dates.get('official_citation_publication_date') or dates.get('preprint_published')
    if not date and p.get('source') == 'arxiv': date = p.get('date')
    if not date and pub.get('proceedings_year'): date = str(pub['proceedings_year'])
    # Compare at year precision only; never invent January 1 for year-only records.
    date = str(date)[:4] if date and re.match(r'^(19|20|21)\d{2}(?:$|-)', str(date)) else None
    return {'topic_matches': hits, 'excluded_matches': excluded,
            'relevance': 0 if excluded else (2 if hits else 1),
            'user_reading_value': 'high' if interest else 'unspecified',
            'user_reading_reason': reason if interest else None,
            'eligible': bool(gate['eligible']), 'publication_evidence_present': verified,
            'ready_stages': ready, 'resource_fields': resources, 'publication_date': date,
            'quality_assessment': '未新增质量评价；缺失资料为未知',
            'evidence_digest': digest(scrub(gate.get('evidence', [])))}


def scrub(value):
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()
                if not any(x in k for x in ('checked_at', 'retrieved', 'updated_at', 'seen'))}
    if isinstance(value, list): return [scrub(v) for v in value]
    return value


def fingerprint(p, c, s, m):
    e = evidence(p, c, s, m)
    content = {k: p.get(k) for k in ('title', 'abstract', 'pdf_url')}
    content.update({k:p.get(k) or {} for k in ('publication','dates')})
    return digest([VERSION, scrub(content), e, m.get('capacity_pin',False),
                   [(k, (s.stage(p['id'], k) or {}).get('version'))
                    for k in ('pdf', 'fulltext', 'daily_summary', 'translation')]])


def inventory(c, s):
    items, problems = [], []
    v = c['paths']['vault']
    papers = {p['id']: p for p in s.papers() if p.get('id')}
    for is_archive, areas in ((False, AREAS), (True, ARCHIVES)):
        for area, folder in areas.items():
            for note in sorted((v / folder).rglob('*.md')):
                m = meta(note)
                if not isinstance(m, dict) or not m.get('paper_id'):
                    # Supplementary notes are not papers. Unidentified primary notes need inspection.
                    if note.name == '阅读笔记.md' or (area == 'candidate' and note.parent == v/folder):
                        problems.append({'path': str(note), 'area': area, 'reason': '主笔记缺少有效 paper_id'})
                    continue
                if m.get('kind') == 'evidence': continue
                pid = m['paper_id']
                if pid not in papers:
                    problems.append({'id': pid, 'area': area, 'reason': 'ID 不在当前数据库或已合并'}); continue
                if area in ('formal','cross') and note.name != '阅读笔记.md': continue
                pin = m.get('capacity_pin', False)
                if type(pin) is not bool:
                    problems.append({'id': pid, 'area': area, 'reason': 'capacity_pin 必须是 YAML 布尔值'}); continue
                state = archived(s, pid)
                if is_archive and (not state or state.get('note') != note.relative_to(v).as_posix()):
                    problems.append({'id':pid,'area':area,'reason':'归档路径与数据库索引不一致；跳过'})
                p = papers[pid]
                items.append({'id': pid, 'area': area, 'archived': is_archive, 'note': str(note),
                              'pin': pin, 'title': p.get('title'), 'evidence': evidence(p, c, s, m),
                              'fingerprint': fingerprint(p, c, s, m)})
    for pid in {x['id'] for x in items}:
        duplicates = [x for x in items if x['id'] == pid]
        if len(duplicates) > 1:
            for x in duplicates: problems.append({'id': pid, 'area': x['area'], 'reason': '同一 paper_id 多份主笔记'})
    bad = {x.get('id') for x in problems}
    # Count duplicates by ID, but never move them automatically.
    for x in items: x['conflict'] = x['id'] in bad
    return items, problems


def preview(c, s, *, incumbents=None, restore=None, proposed=None):
    items, problems = inventory(c, s)
    if proposed:
        incumbents={(x["area"],x["id"]) for x in items if not x["archived"]}
        items.append(proposed)
    caps = limits(c)
    result = {'rule_version': VERSION, 'limits': caps, 'counts': {}, 'problems': problems,
              'actions': [], 'decisions': [], 'historical_delivered': s.db.execute(
                  "SELECT count(DISTINCT paper_id) FROM stages WHERE stage='delivery' AND status='success'").fetchone()[0]}
    pending = c['paths']['data']/'capacity'/'pending.json'
    if pending.exists(): result['problems'].append({'reason': '未完成迁移；下次执行先自动恢复', 'area': 'all'})
    for area in AREAS:
        rows = [x for x in items if x['area'] == area]
        active = [x for x in rows if not x['archived']]
        result['counts'][area] = {'active': len({x['id'] for x in active}),
            'archived': len({x['id'] for x in rows if x['archived']}),
            'pinned': len({x['id'] for x in active if x['pin']}),
            'archived_pinned': len({x['id'] for x in rows if x['archived'] and x['pin']})}
        if result['counts'][area]['pinned'] > caps[area]:
            result['problems'].append({'area': area, 'reason': '固定数量超过上限；暂停此区域全部自动迁移'})
        # Fail closed for the affected area: unknown ownership could overwrite personal work.
        if any(x.get('area') in (area, 'all') for x in result['problems']): continue
        contenders = list(active)
        for x in rows:
            if not x['archived']: continue
            old = archived(s, x['id'])
            if restore == x['id'] or (old and old['fingerprint'] != x['fingerprint']):
                if area in ('formal','cross') and not restorable_formal(c, s, x):
                    result['decisions'].append(dict(x, decision='archive', reason='正式恢复资格或现有完整交付检查未通过'))
                    continue
                contenders.append(x)
        # Unknown dates are not imputed as old/zero. Disable this final tie-break
        # for the whole comparison set if any date is unknown (total ordering).
        use_date = all(x['evidence']['publication_date'] for x in contenders)
        def key(x):
            e = x['evidence']
            existing = not x['archived'] and (incumbents is None or (area, x['id']) in incumbents)
            readiness = len(e['ready_stages']) + len(e['resource_fields'])
            rank = (int(x['pin']), e['relevance'], int(e['user_reading_value'] == 'high'),
                    int(e['eligible']), int(e['publication_evidence_present']), readiness,
                    e['publication_date'] if use_date else '', int(existing))
            x['rank'] = list(rank)
            return tuple(-a if isinstance(a, int) else a for a in rank[:6]) + (
                -int(rank[6].replace('-', '')) if rank[6] else 0, -rank[7], x['id'])
        contenders.sort(key=key)
        if sum(x['pin'] for x in contenders) > caps[area]:
            result['problems'].append({'area': area, 'reason': '恢复后的固定数量超过上限；暂停此区域迁移'}); continue
        keep = {x['id'] for x in contenders[:caps[area]]}
        cutoff = contenders[min(caps[area], len(contenders))-1]['rank'] if contenders else None
        for x in contenders:
            retain = x['id'] in keep
            reason = ('用户固定保留' if x['pin'] else '按主题、用户阅读价值、资格证据、资料准备程度、可用日期、在区优先、稳定ID排序')
            reason += '; ' + ('容量内保留' if retain else '未超过本区保留门槛')
            d = dict(x, decision='retain' if retain else 'archive', reason=reason,
                     cutoff=cutoff, date_tiebreak=use_date)
            result['decisions'].append(d)
            if retain == x['archived'] and x is not proposed:
                result['actions'].append(dict(d, action='restore' if retain else 'archive'))
    if restore and not any(x['id'] == restore and x['archived'] for x in items):
        result['problems'].append({'id': restore, 'reason': '未找到唯一归档论文'})
    for x in result['actions']:
        try:
            target = destination(c,s,x); x['destination'] = str(target)
            if target.exists(): raise ValueError('目标已存在；暂停此区域迁移')
        except (OSError,ValueError) as exc:
            result['problems'].append({'id':x['id'],'area':x['area'],'reason':str(exc)})
    return result


def restorable_formal(c, s, x):
    from .daily import completed
    return x['evidence']['eligible'] and bool(completed(s.get(x['id']), c, s))


def destination(c, s, x):
    v = c['paths']['vault']; old = Path(x['note'])
    if x['action'] == 'restore':
        # Ignore archived routing while allocating the canonical active path.
        return reading_path(s.get(x['id']), c, s, x['area'] in ('formal','cross'), reserve=False, active=True)
    base = v/ARCHIVES[x['area']]
    name = fit_name(base, old.parent.name if x['area'] in ('formal','cross') else old.stem,
                    '_' + x['id'] + ('' if x['area'] in ('formal','cross') else '.md'),
                    '/阅读笔记.md' if x['area'] in ('formal','cross') else '')
    return base/name/'阅读笔记.md' if x['area'] in ('formal','cross') else base/name


def remap(value, mapping, vault):
    if isinstance(value, dict): return {k: remap(v, mapping, vault) for k, v in value.items()}
    if isinstance(value, list): return [remap(v, mapping, vault) for v in value]
    if isinstance(value, str):
        for a, b in mapping.items():
            if value == Path(a).relative_to(vault).as_posix(): return b.relative_to(vault).as_posix()
            if value.startswith(('/', 'C:', 'c:')) and portable(value).resolve() == Path(a): return str(b)
    return value


def recover(c, s):
    marker = c['paths']['data']/'capacity'/'pending.json'
    if not marker.exists(): return None
    journal = json.loads(marker.read_text('utf-8')); backup = Path(journal['backup'])
    # Restore by verified rename and byte backups; refuse unrelated destination changes.
    for entry in journal.get('expected', []):
        available = [Path(p) for p in (entry['source'],entry['destination']) if Path(p).is_file()]
        if not available or any(digest(p.read_bytes()) not in entry['hashes'] for p in set(available)):
            raise ValueError('中断后文件缺失或被外部修改；暂停恢复，原件与备份保留：'+entry['source'])
    for a, b in reversed(journal['moves']):
        a, b = Path(a), Path(b)
        if b.exists() and not a.exists():
            a.parent.mkdir(parents=True, exist_ok=True); b.replace(a)
        elif b.exists() and a.exists(): raise ValueError('恢复发现源目标同时存在，请人工检查回执')
    for entry in journal['markdown']:
        atomic_write(Path(entry['path']), (backup/entry['backup']).read_bytes())
    source = sqlite3.connect(backup/'papers.sqlite3')
    try: source.backup(s.db)
    finally: source.close()
    journal.update(status='rolled_back', recovered_at=now())
    atomic_write(backup/'receipt.json', json.dumps(journal, ensure_ascii=False, indent=2))
    marker.unlink()
    return str(backup)


def migrate(c, s, plan):
    actions = plan['actions']; v = c['paths']['vault'].resolve()
    moves, mapping = [], {}
    for x in actions:
        src, dst = Path(x['note']), destination(c, s, x)
        a, b = (src.parent, dst.parent) if x['area'] in ('formal','cross') else (src, dst)
        if not a.resolve().is_relative_to(v) or not b.resolve().is_relative_to(v): raise ValueError('路径越界')
        if b.exists() or a.is_symlink() or any(f.is_symlink() for f in a.rglob('*')):
            raise ValueError('目标冲突或符号链接，拒绝迁移')
        moves.append((str(a), str(b)))
        mapping[str(a.resolve())] = b.resolve()
        for f in a.rglob('*') if a.is_dir() else [a]:
            if f.is_file(): mapping[str(f.resolve())] = (b/f.relative_to(a) if a.is_dir() else b).resolve()
        x['destination'] = str(dst)
    base = c['paths']['data']/'capacity'/('operation-' + uuid.uuid4().hex)
    base.mkdir(parents=True)
    db = sqlite3.connect(base/'papers.sqlite3')
    try: s.db.backup(db)
    finally: db.close()
    assets = []
    for index, (a, b) in enumerate(moves):
        source = Path(a); target = base/'assets'/str(index)
        if source.is_dir(): shutil.copytree(source, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(source, target)
        files = list(source.rglob('*')) if source.is_dir() else [source]
        for f in files:
            if f.is_file(): assets.append({'source':str(f), 'sha256':digest(f.read_bytes())})
    markdown = []
    # External historical reports may contain links into the Vault too.
    extra = c['paths']['data'].parent.parent/'docs'
    notes = list(v.rglob('*.md')) + (list(extra.rglob('*.md')) if extra.exists() else [])
    edits = []
    expected = {asset['source']:{'source':asset['source'],
                'destination':str(mapping[str(Path(asset['source']).resolve())]),
                'hashes':[asset['sha256']]} for asset in assets}
    for note in notes:
        if '.obsidian' in note.parts: continue
        newpath = mapping.get(str(note.resolve()), note)
        raw = note.read_bytes(); text = raw.decode('utf-8')
        revised = rewrite_text(text, note, newpath, mapping, v)
        if revised != text or newpath != note:
            name = 'md/' + str(len(markdown)) + '.md'
            atomic_write(base/name, raw); markdown.append({'path': str(note), 'backup': name})
            edits.append((newpath, revised))
            expected[str(note)] = {'source':str(note),'destination':str(newpath),
                                   'hashes':[digest(raw),digest(revised)]}
    journal = {'status': 'pending', 'backup': str(base), 'time': now(), 'moves': moves,
               'markdown': markdown, 'assets': assets, 'expected':list(expected.values()), 'plan': plan}
    marker = c['paths']['data']/'capacity'/'pending.json'
    atomic_write(base/'receipt.json', json.dumps(journal, ensure_ascii=False, indent=2))
    atomic_write(marker, json.dumps(journal, ensure_ascii=False, indent=2))
    try:
        for a, b in moves:
            Path(b).parent.mkdir(parents=True, exist_ok=True); Path(a).replace(b)
        for path, raw in edits: atomic_write(path, raw)
        for asset in assets:
            path = mapping[ str(Path(asset['source']).resolve()) ]
            if path.suffix != '.md' and digest(path.read_bytes()) != asset['sha256']:
                raise ValueError('附件迁移哈希检查失败')
        with s.db:
            for table, col, identity in [('stages', 'detail', ['paper_id', 'stage']), ('state', 'value', ['key']), ('runs','report',['id'])]:
                for row in s.db.execute('SELECT * FROM ' + table).fetchall():
                    if not row[col]: continue
                    old = json.loads(row[col]); new = remap(old, mapping, v)
                    if old != new:
                        s.db.execute('UPDATE '+table+' SET '+col+'=? WHERE '+' AND '.join(k+'=?' for k in identity),
                                     [json.dumps(new, ensure_ascii=False)]+[row[k] for k in identity])
        for x in actions:
            pid = x['id']; rel = Path(x['destination']).relative_to(v).as_posix()
            if x['action'] == 'archive':
                s.set_state('capacity:archive:'+pid, {'area': x['area'], 'note': rel,
                    'fingerprint': x['fingerprint'], 'rule_version': VERSION, 'reason': x['reason']})
            else:
                with s.db: s.db.execute('DELETE FROM state WHERE key=?', ('capacity:archive:'+pid,))
            key = ('delivery-folder:' if x['area'] in ('formal','cross') else 'candidate-path:')+pid
            s.set_state(key, str(Path(rel).parent).replace('\\', '/') if x['area'] in ('formal','cross') else rel)
        journal['status'] = 'complete'; journal['ended'] = now()
        atomic_write(base/'receipt.json', json.dumps(journal, ensure_ascii=False, indent=2))
        marker.unlink()
    except Exception:
        recover(c, s)
        raise
    return str(base/'receipt.json')


def apply(c, s, *, incumbents=None, restore=None):
    with run_lock(c['paths']['data'], reentrant=True):
        return _apply_locked(c,s,incumbents=incumbents,restore=restore)


def _apply_locked(c, s, *, incumbents=None, restore=None):
    recover(c, s)
    admissions = s.db.execute("SELECT key,value FROM state WHERE key LIKE 'capacity:admission:%'").fetchall()
    for row in admissions:
        saved = {tuple(x) for x in json.loads(row['value'])}
        incumbents = saved if incumbents is None else incumbents & saved
    plan = preview(c, s, incumbents=incumbents, restore=restore)
    # Destination conflicts are reported before any object is touched.
    safe = []
    for x in plan['actions']:
        try:
            dst = destination(c, s, x)
            if dst.exists(): raise ValueError('目标已存在')
            safe.append(x)
        except (ValueError, OSError) as exc:
            plan['problems'].append({'id': x['id'], 'area': x['area'], 'reason': str(exc)})
    # A failed eviction must never allow its paired restore to overfill the area.
    bad = {x.get('area') for x in plan['problems']}
    plan['actions'] = [x for x in safe if x['area'] not in bad]
    if plan['actions']: plan['receipt'] = migrate(c, s, plan)
    # Persist evaluated fingerprints even when an archive loses again; no cycling.
    for x in plan['decisions']:
        if x['area'] in bad: continue
        a = archived(s, x['id'])
        if a:
            a['fingerprint'] = x['fingerprint']; s.set_state('capacity:archive:'+x['id'], a)
    plan['after_counts'] = preview(c,s)['counts']
    receipt = c['paths']['data']/'capacity'/('decision-'+uuid.uuid4().hex+'.json')
    atomic_write(receipt, json.dumps(dict(plan, time=now()), ensure_ascii=False, indent=2))
    with s.db:
        for row in admissions: s.db.execute('DELETE FROM state WHERE key=?',(row['key'],))
    plan['decision_receipt'] = str(receipt)
    return plan


def dormant(p, c, s):
    a = archived(s, p['id'])
    if not a: return False
    path = archive_note(c, s, p['id'])
    return a['fingerprint'] == fingerprint(p, c, s, meta(path))


def guarded(function):
    """Wrap actual publication/annotation boundaries, including direct Python use."""
    signature = inspect.signature(function)
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs).arguments
        c = bound['c']; s = bound.get('store', bound.get('s'))
        with run_lock(c['paths']['data'], reentrant=True):
            recover(c, s)
            before, _ = inventory(c, s)
            incumbents = {(x['area'], x['id']) for x in before if not x['archived']}
            p = bound.get('p')
            if p:
                if any(x['conflict'] for x in before if x['id']==p['id']):
                    raise ValueError('ID 冲突；跳过受影响论文')
                s.set_state('capacity:admission:'+p['id'], sorted(incumbents))
            try:
                result = function(*args, **kwargs)
            finally:
                report = apply(c, s, incumbents=incumbents)
            # Return the current location, never a stale pre-eviction path.
            if p and isinstance(result, Path):
                from .library import note_for_id
                return note_for_id(c,s,p['id'],True) or note_for_id(c,s,p['id']) or result
            if p and isinstance(result, dict) and 'bilingual' in result:
                return json.loads(s.stage(p['id'], 'delivery')['detail'])
            if isinstance(result, dict): result['capacity'] = report
            return result
    return wrapped


def command(c, action, pid=None):
    if action in ('preview', 'status'):
        # SQLite mode=ro may still update WAL shared-memory read marks. Read a
        # verified stable private snapshot instead; no production file is opened
        # for writing. A concurrent writer causes an explicit retry, not stale work.
        with tempfile.TemporaryDirectory(prefix='paper-capacity-preview-') as temp:
            source = c['paths']['data']/'papers.sqlite3'
            paths = [source, source.with_name(source.name+'-wal')]
            contents = {p.name:p.read_bytes() for p in paths if p.exists()}
            for name, raw in contents.items(): (Path(temp)/name).write_bytes(raw)
            if contents != {p.name:p.read_bytes() for p in paths if p.exists()}:
                raise RuntimeError('数据库正在写入，请稍后重新预览')
            s = Store.__new__(Store); s.data = c['paths']['data']
            s.db = sqlite3.connect(Path(temp)/source.name); s.db.row_factory = sqlite3.Row
            try:
                result = preview(c,s)
                if action == 'status':
                    return {k:result[k] for k in ('rule_version','limits','counts','problems','historical_delivered')}
                return result
            finally: s.db.close()
    with run_lock(c['paths']['data']):
        s = Store(c['paths']['data'])
        try:
            if action in ('pin', 'unpin'):
                recover(c, s)
                rows, problems = inventory(c, s)
                matches = [x for x in rows if x['id'] == pid]
                if len(matches) != 1 or matches[0]['conflict']: raise ValueError('ID 冲突或未找到')
                import yaml
                path = Path(matches[0]['note']); raw = path.read_text('utf-8')
                _, front, rest = raw.split('---\n', 2)
                data = yaml.safe_load(front); data['capacity_pin'] = action == 'pin'
                atomic_write(path, '---\n'+yaml.safe_dump(data, allow_unicode=True, sort_keys=False)+'---\n'+rest,
                             c['paths']['data']/'backups'/'capacity-pins')
            return apply(c, s, restore=pid if action == 'restore' else None)
        finally: s.db.close()
