"""One naming and link policy for migration, candidates and production delivery.

Identity and content caches never depend on these display paths.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, unquote

import yaml

from .core import TZ, atomic_write, local_path

MAX_PATH_UNITS = 240


def portable(value):
    value = str(value)
    if os.name == 'nt' and value.startswith('/mnt/'):
        value = value[5].upper() + ':' + value[6:]
    return local_path(value)


def day_of(value):
    """Delivery timestamps are instants, never publication dates or mtimes."""
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            return None  # old date-only evidence is insufficient
        return stamp.astimezone(TZ).strftime('%Y.%m.%d')
    except ValueError:
        return None


def paper_year(p):
    pub, dates = p.get('publication', {}), p.get('dates', {})
    def year(v):
        m = re.match(r'^(19\d{2}|20\d{2}|21\d{2})(?:\D|$)', str(v or ''))
        return m[1] if m else None
    if pub.get('status') in ('published', 'accepted'):
        for value, source in ((pub.get('proceedings_year'), 'official_proceedings_year'),
                              (dates.get('proceedings_year'), 'official_proceedings_year'),
                              (dates.get('official_citation_publication_date'), 'official_citation_publication_date')):
            if year(value):
                return year(value), source
    if year(dates.get('preprint_published')):
        return year(dates['preprint_published']), 'preprint_first_published'
    if (pub.get('status') == 'published' and p.get('venue_kind') == 'journal' and p.get('doi')
            and pub.get('version') == 'publishedVersion' and year(dates.get('openalex_publication_date'))):
        # Filename metadata only; this never establishes SCIE/CAS eligibility.
        return year(dates['openalex_publication_date']), 'openalex_published_journal_metadata'
    # Original arXiv adapter stores Atom <published> in date, not retrieval time.
    if (p.get('source') == 'arxiv' or 'arxiv.org' in pub.get('source', '')) and year(p.get('date')):
        return year(p['date']), 'arxiv_atom_published'
    return '年份待核实', 'unknown'


def clean(value):
    value = unicodedata.normalize('NFC', str(value or ''))
    value = re.sub(r'[<>:"/\\|?*\[\]#^\x00-\x1f]', '', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value):
        value = '_' + value
    return value or '标题待补充'


def units(value):
    return len(str(value).encode('utf-16-le')) // 2


def fit_name(parent, name, suffix='', tail=''):
    budget = MAX_PATH_UNITS - units(parent.resolve()) - 1 - units(suffix) - units(tail)
    if budget < 20:
        raise ValueError('根路径过长，无法安全生成可读文件名')
    name = clean(name)
    while units(name) > budget:
        name = name[:-1]
    return name.rstrip(' .') + suffix


def meta(path):
    try:
        raw = path.read_text('utf-8')
        return (yaml.safe_load(raw.split('---\n', 2)[1]) or {}) if raw.startswith('---\n') else {}
    except (OSError, ValueError, yaml.YAMLError):
        return {}


def owner(path, formal=False):
    return meta(path/'阅读笔记.md' if formal else path).get('paper_id')


def note_for_id(c, s, pid, formal=False):
    from .capacity import archive_note
    saved = archive_note(c, s, pid, formal)
    if saved: return saved
    vault = c['paths']['vault']
    key = ('delivery-folder:' if formal else 'candidate-path:') + pid
    known = s.get_state(key)
    if known:
        p = vault/known
        if formal: p /= '阅读笔记.md'
        if p.exists() and owner(p) == pid:
            return p
    from .branches import primary
    base = vault/(('20_交叉领域论文' if primary(s,pid)=='cross' else '10_论文') if formal else '90_候选与待核验')
    found = [q for q in (base.glob('*/阅读笔记.md') if formal else base.glob('*.md')) if owner(q) == pid]
    legacy = vault/'10_论文笔记'/(pid+'.md')
    if formal and legacy.exists(): found.append(legacy)
    if len(found) > 1:
        raise ValueError('同一稳定ID存在多份笔记，拒绝覆盖')
    return found[0] if found else None


def reading_path(p, c, s, formal=False, reserve=True, active=False):
    """Collision allocation is case-insensitive on all operating systems.

    A readable venue precedes a deterministic integer; never merge by name.
    Persisted claims also reserve a path before a file is created.
    """
    pid = p['id']; vault = c['paths']['vault']
    if not active:
        from .capacity import archive_note
        saved = archive_note(c, s, pid, formal)
        if saved: return saved
    year, _ = paper_year(p)
    title = clean(p.get('title') or '标题待补充')
    if title == '标题待补充':
        row = s.db.execute('SELECT rowid FROM papers WHERE id=?', (pid,)).fetchone()
        title += '_' + str(row[0] if row else int.from_bytes(pid.encode(), 'big'))
    venue = clean(p.get('venue') or '来源待核实')
    from .branches import primary
    parent = vault/(('20_交叉领域论文' if primary(s,pid)=='cross' else '10_论文') if formal else '90_候选与待核验')
    prefix = f'{year}_{venue}_' if formal else f'{year}_'
    base = prefix + title
    key = ('delivery-folder:' if formal else 'candidate-path:') + pid
    claims = {}
    for r in s.db.execute("SELECT key,value FROM state WHERE key LIKE 'delivery-folder:%' OR key LIKE 'candidate-path:%'"):
        claims[str(json.loads(r['value'])).casefold()] = r['key'].split(':', 1)[1]
    disk = {q.name.casefold(): q for q in parent.iterdir()} if parent.exists() else {}
    for n in range(1, 100000):
        extra = '' if n == 1 else ('_'+venue+('' if n == 2 else '_'+str(n-1)) if not formal else '_'+str(n))
        suffix = '' if formal else '.md'
        # Preserve collision suffix even when truncation is required.
        name = fit_name(parent, base, extra+suffix, '/阅读笔记.md' if formal else '')
        path = parent/name; rel = path.relative_to(vault).as_posix()
        occupied = disk.get(name.casefold())
        claimed = claims.get(rel.casefold())
        if claimed not in (None, pid): continue
        if occupied is not None and owner(occupied, formal) != pid: continue
        if reserve: s.set_state(key, rel)
        return path/'阅读笔记.md' if formal else path
    raise ValueError('文件名冲突序号已用尽')


def naming_meta(p):
    year, source = paper_year(p)
    return {'filename_year': year, 'filename_year_source': source,
            'doi': p.get('doi', ''), 'arxiv_id': p.get('arxiv_id', ''),
            'identifiers': p.get('aliases', [])}


MD_LINK = re.compile(r'(?P<head>\]\()(?P<target><[^>\n]+>|[^\n)]+)(?P<end>\))')
WIKI_LINK = re.compile(r'\[\[([^\]\n]+)\]\]')


def rewrite_text(raw, origin, destination, mapping, vault):
    """Rebase moved notes and preserve labels, PDF pages, anchors and embeds."""
    original_mapping = {str(Path(a).resolve()): Path(b).resolve() for a,b in mapping.items()}
    mapping = {a.casefold(): b for a,b in original_mapping.items()}
    def mapped(path): return mapping.get(str(path.resolve()).casefold(), path.resolve())
    def md(m):
        target = m['target']; angle = target.startswith('<')
        target = target[1:-1] if angle else target
        # Optional Markdown title (not part of the file path).
        mt = re.match(r'^(.*?)(\s+["\'].*["\'])$', target)
        target, caption = (mt[1], mt[2]) if mt else (target, '')
        if re.match(r'^(?:[a-z][a-z0-9+.-]*://|mailto:|#)', target, re.I): return m[0]
        base, sep, anchor = target.partition('#')
        if not base:return m[0]
        old = (origin.parent/unquote(base)).resolve()
        new = mapped(old)
        if old == new and origin.parent == destination.parent:return m[0]
        relative = os.path.relpath(new, destination.parent).replace(os.sep, '/')
        value = quote(relative, safe='/.-_') + (sep+anchor if sep else '')
        return '](' + ('<'+value+'>' if angle else value) + caption + ')'
    raw = MD_LINK.sub(md, raw)
    def wiki(m):
        target, bar, label = m[1].partition('|')
        base, sharp, anchor = target.partition('#')
        if not base:return m[0]
        decoded = unquote(base)
        candidates = []
        # Prefer a resolvable local/vault path over basename matching. Many
        # formal folders contain a note called 阅读笔记.md; moving one must not
        # steal a local link belonging to a different paper.
        resolved = None
        for root in (origin.parent, vault):
            probe = (root/decoded).resolve()
            for option in (probe, Path(str(probe)+'.md')):
                if str(option).casefold() in mapping or option.is_file():
                    resolved = option; break
            if resolved is not None: break
        if resolved is not None:
            new = mapped(resolved)
            if new == resolved and origin.parent == destination.parent: return m[0]
            try: value = new.relative_to(vault.resolve()).as_posix().removesuffix('.md')
            except ValueError:
                value=quote(os.path.relpath(new,destination.parent).replace(os.sep,'/'),safe='/.-_')
                return '['+(label if bar else base)+']('+value+(sharp+anchor if sharp else '')+')'
            return '[['+value+(sharp+anchor if sharp else '')+(bar+label if bar else '')+']]'
        for old, new in original_mapping.items():
            old = Path(old)
            # Compare paths case-insensitively, while retaining real destination case.
            variants = [str(old), old.name, old.stem, os.path.relpath(old, origin.parent).replace(os.sep,'/')]
            try: variants.append(old.relative_to(vault.resolve()).as_posix())
            except ValueError: pass
            if any(decoded.casefold() in (v.casefold(), v.removesuffix('.md').casefold()) for v in variants):
                candidates.append(new)
        if len(set(candidates)) == 1:
            new = candidates[0]
            try: value = new.relative_to(vault.resolve()).as_posix()
            except ValueError:
                # Obsidian wiki links cannot escape a vault. Convert to a local Markdown link.
                value=quote(os.path.relpath(new,destination.parent).replace(os.sep,'/'),safe='/.-_')
                return '['+(label if bar else base)+']('+value+(sharp+anchor if sharp else '')+')'
            value = value.removesuffix('.md')
            return '[['+value+(sharp+anchor if sharp else '')+(bar+label if bar else '')+']]'
        # Rebase explicitly relative wiki links even when the target did not move.
        if decoded.startswith('.') and origin.parent != destination.parent:
            old=(origin.parent/decoded).resolve();new=mapped(old)
            value=os.path.relpath(new,destination.parent).replace(os.sep,'/')
            return '[['+value+(sharp+anchor if sharp else '')+(bar+label if bar else '')+']]'
        return m[0]
    return WIKI_LINK.sub(wiki, raw)


def rewrite_links(vault, mapping, c, origins=None, extra=()):
    origins = origins or {}
    for note in list(vault.rglob('*.md')) + list(extra):
        if '.obsidian' in note.parts: continue
        if str(note.resolve()) in mapping: continue  # untouched archive source
        old = note.read_text('utf-8')
        origin = origins.get(str(note.resolve()), note)
        new = rewrite_text(old, origin, note, mapping, vault)
        if new != old: atomic_write(note, new, c['paths']['data']/'backups')


def move_note(source, destination, c):
    if source.resolve() == destination.resolve(): return
    if destination.exists(): raise ValueError('目标已存在，拒绝覆盖笔记')
    raw=source.read_bytes()
    atomic_write(c['paths']['data']/'backups'/'readable-names'/source.name, raw)
    destination.parent.mkdir(parents=True,exist_ok=True)
    source.replace(destination)
    rewrite_links(c['paths']['vault'], {str(source.resolve()): destination}, c,
                  {str(destination.resolve()):source})


def candidate_path(p,c,s):
    path=reading_path(p,c,s)
    old=note_for_id(c,s,p['id'])
    if old and old != path: move_note(old,path,c)
    return path


def sync_formal_path(p,c,s):
    """Metadata changes rename a delivery without creating a second paper."""
    old=note_for_id(c,s,p['id'],True)
    if not old or old.parent.parent.name!='10_论文':return old
    new=reading_path(p,c,s,True)
    if old==new:return old
    if new.parent.exists():raise ValueError('正式目录目标冲突，保留原件')
    files=list(old.parent.rglob('*'))
    mapping={str(f.resolve()):new.parent/f.relative_to(old.parent) for f in files if f.is_file()}
    origins={str(dst.resolve()):Path(src) for src,dst in mapping.items() if Path(src).suffix=='.md'}
    atomic_write(c['paths']['data']/'backups'/'readable-names'/(p['id']+'.md'),old.read_bytes())
    old.parent.replace(new.parent)
    rewrite_links(c['paths']['vault'],mapping,c,origins)
    def update(value):
        if isinstance(value,dict):return {k:update(v) for k,v in value.items()}
        if isinstance(value,list):return [update(v) for v in value]
        if isinstance(value,str):
            if value==old.parent.relative_to(c['paths']['vault']).as_posix():
                return new.parent.relative_to(c['paths']['vault']).as_posix()
            if value.startswith(('/mnt/','/','C:','c:')):
                for src,dst in mapping.items():
                    if portable(value).resolve()==Path(src):return str(dst)
        return value
    with s.db:
        for r in s.db.execute('SELECT paper_id,stage,detail FROM stages').fetchall():
            value=json.loads(r['detail']);changed=update(value)
            if value!=changed:s.db.execute('UPDATE stages SET detail=? WHERE paper_id=? AND stage=?',(json.dumps(changed,ensure_ascii=False),r['paper_id'],r['stage']))
        for r in s.db.execute('SELECT key,value FROM state').fetchall():
            value=json.loads(r['value']);changed=update(value)
            if value!=changed:s.db.execute('UPDATE state SET value=? WHERE key=?',(json.dumps(changed,ensure_ascii=False),r['key']))
    return new


def brief_path(c, day):
    return c['paths']['vault']/'00_每日简报'/day/'每日简报.md'
