"""Local CNKI EndNote imports. No HTTP, credentials, model or translation calls."""
from __future__ import annotations

import json
import contextlib
import re
import unicodedata
from datetime import date as Date
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from .core import Store, atomic_write, digest, doi_id, now, today, run_lock, safe_error


def folders(c):
    root = c['paths']['data'] / 'imports' / 'cnki'
    for name in ('pending', 'originals', 'receipts', 'fulltext'):
        (root / name).mkdir(parents=True, exist_ok=True)
    return root


def decode(raw):
    encodings = ['utf-16'] if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else ['utf-8-sig', 'gb18030']
    for encoding in encodings:
        try:
            value = raw.decode(encoding)
            if '\x00' in value: raise ValueError('文本含 NUL；请导出带 BOM 的 UTF-16 或 UTF-8')
            return value, encoding
        except UnicodeDecodeError:
            pass
    raise ValueError('编码无法识别；支持 UTF-8、GB18030、带 BOM 的 UTF-16')


def parse(raw):
    text, encoding = decode(raw)
    if not re.search(r'^%0\s+', text, re.M):
        kind = 'RIS' if re.search(r'^TY\s+-', text, re.M) else 'BibTeX' if re.search(r'@\w+\s*\{', text) else 'RefWorks' if re.search(r'^RT\s+', text, re.M) else '未知/格式化引文'
        raise ValueError(f'{kind}不是 EndNote 标记文本；请在知网选择 EndNote 导出')
    records = []
    for block in re.split(r'(?=^%0\s+)', text, flags=re.M):
        if not block.strip(): continue
        fields, errors, key = {}, [], None
        for lineno, line in enumerate(block.splitlines(), 1):
            match = re.match(r'^%([A-Za-z0-9@+!?])(?:\s+(.*)|$)', line)
            if match:
                key = match[1]; fields.setdefault(key, []).append((match[2] or '').strip())
            elif line.strip():
                if key and not line.startswith('%'): fields[key][-1] += '\n' + line.strip()
                else: errors.append(f'记录内第 {lineno} 行无法识别')
        if not fields.get('0') or not fields['0'][0]: errors.append('缺少 %0 类型')
        if len(fields.get('T', [])) != 1 or not fields.get('T', [''])[0]: errors.append('缺少或重复 %T 标题')
        records.append({'fields': fields, 'errors': errors})
    return records, encoding


def norm(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')).casefold())


def identity(p):
    year = str(p.get('date') or '')[:4]
    values = [norm(p.get('title')), tuple(sorted(norm(a) for a in p.get('authors', []))), year, norm(p.get('venue'))]
    return values if all(values) and re.fullmatch(r'\d{4}', year) else None


def normalize(fields):
    def one(k):
        values = fields.get(k, [])
        if len(values) > 1 and k not in ('U',): raise ValueError(f'字段 %{k} 重复，待确认')
        return values[0] if values else ''
    doi = doi_id(one('R'))
    if doi and not re.fullmatch(r'10\.\d{4,9}/\S+', doi): raise ValueError('DOI 格式无效')
    date = one('D')
    if date and not re.fullmatch(r'\d{4}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?', date): raise ValueError('%D 年份/日期格式无效')
    if date:
        parts = [int(x) for x in re.split('[-/]',date)]
        try: Date(*(parts + [1] * (3-len(parts))))
        except ValueError: raise ValueError('%D 日期不存在') from None
    aliases = ['doi:' + doi] if doi else []
    urls = [u for u in fields.get('U', []) if urlparse(u).scheme in ('http', 'https')]
    warnings = ['%U 存在非 HTTP(S) 来源链接，保留在原始字段但不生成可点击链接'] if len(urls)!=len(fields.get('U', [])) else []
    issns = []
    for value in fields.get('@', []):
        digits=value.replace('-','').upper()
        if (re.fullmatch(r'\d{7}[\dX]',digits) and
                (sum(int(x)*(8-i) for i,x in enumerate(digits[:7]))+(10 if digits[-1]=='X' else int(digits[-1])))%11==0):
            issns.append(digits[:4]+'-'+digits[4:])
        else: warnings.append('%@ 非有效 ISSN；原值保留，不作为期刊资格标识')
    cnki_ids = []
    for url in urls:
        parsed = urlparse(url)
        if parsed.hostname and (parsed.hostname == 'cnki.net' or parsed.hostname.endswith('.cnki.net')):
            q = {k.lower(): v for k, v in parse_qs(parsed.query).items()}
            # Explicit database + filename only; opaque v/id/filename tokens are NOT IDs.
            db, fn = q.get('dbcode', [''])[0], q.get('filename', [''])[0]
            if re.fullmatch(r'[A-Za-z]{4}', db) and re.fullmatch(r'[A-Za-z0-9]{8,40}', fn):
                cnki_ids.append(db.upper() + ':' + fn.upper())
    aliases += ['cnki:' + value for value in cnki_ids]
    language = one('G')
    p = {'source':'cnki', 'sources':['cnki'], 'title':one('T'), 'authors':fields.get('A', []),
         'venue':one('J'), 'venue_kind':'journal' if one('0') == 'Journal Article' else 'unknown',
         'abstract':one('X'), 'doi':doi, 'date':date, 'dates':{'cnki_export_date':date} if date else {},
         'url':urls[0] if urls else '', 'source_urls':urls, 'cnki_ids':cnki_ids,
         'issns':issns, 'import_warnings':warnings,
         'publication':{'status':'unknown'}, 'metadata_language':language or 'unknown',
         'cnki_fields':fields, 'aliases':aliases}
    if not aliases:
        key = identity(p)
        p['aliases'] = ['cnki:metadata:' + digest(key if key else fields)]
    return p


def match(p, s, *, cnki_only=False):
    aliases = p['aliases']
    ids = {r[0] for a in aliases for r in s.db.execute('SELECT paper_id FROM identifiers WHERE alias=?', (a,))}
    if len(ids) > 1: raise ValueError('强标识指向多个实体，禁止自动合并，待确认')
    if ids:
        pid = next(iter(ids)); old = s.get(pid)
        if p.get('doi') and old.get('doi') and p['doi'] != old['doi']:
            raise ValueError('同一来源标识的 DOI 冲突，待确认')
        return pid, []
    exact, related = [], []
    key = identity(p)
    for old in s.papers():
        if cnki_only and 'cnki' not in old.get('sources', []): continue
        same = key is not None and key == identity(old)
        incompatible = p.get('doi') and old.get('doi') and p['doi'] != old['doi']
        incompatible = incompatible or (p.get('cnki_ids') and old.get('cnki_ids') and not set(p['cnki_ids']) & set(old['cnki_ids']))
        if same and not incompatible: exact.append(old['id'])
        elif norm(p['title']) == norm(old['title']) or SequenceMatcher(None, norm(p['title']), norm(old['title'])).ratio() >= .88:
            related.append(old['id'])
    if len(exact) == 1: return exact[0], related
    return None, sorted(set(related + exact))


def import_file(c, s, path):
    root = folders(c); path = Path(path); raw = path.read_bytes(); sha = digest(raw)
    key = 'cnki:file:' + sha
    prior = s.get_state(key)
    if prior and prior.get('finished'):
        return dict(prior, repeated_file=True, new=0, updated=0, duplicate=0,
                    this_import={k:0 for k in ('new','updated','duplicate','failed','deferred')})
    before_counts = {k:(prior or {}).get(k,0) for k in ('new','updated','duplicate','failed')}
    original = root / 'originals' / (sha + '.bin')
    atomic_write(original, raw)
    report = prior or {'sha256':sha, 'filename':path.name, 'original':str(original), 'imported_at':now(),
        'records':0, 'new':0, 'updated':0, 'duplicate':0, 'failed':0, 'deferred':0, 'rows':[], 'finished':False}
    try:
        records, encoding = parse(raw)
        report.update(format='EndNote tagged text', encoding=encoding, records=len(records))
    except ValueError as exc:
        report.update(failed=1, errors=[str(exc)], finished=True)
        records = []
    done = {r['index'] for r in report['rows'] if r['status'] != 'deferred'}
    report['rows'] = [r for r in report['rows'] if r['status'] != 'deferred']; report['deferred'] = 0
    for index, record in enumerate(records, 1):
        if index in done: continue
        row = {'index':index, 'imported_at':now()}
        try:
            if record['errors']: raise ValueError('; '.join(record['errors']))
            p = normalize(record['fields']); pid, related = match(p, s)
            p['cnki_import'] = {'file_sha256':sha, 'record':index}
            old = s.get(pid) if pid else {}
            if pid: p['aliases'] = sorted(set(p['aliases'] + old.get('aliases', [])))
            pid = s.ingest(p, c['limits']['candidates_per_day'])
            if not pid:
                row.update(status='deferred', error='当日候选接纳限额；下一日继续'); report['deferred'] += 1
            else:
                status = 'new' if not old else 'updated' if s.get(pid) != old else 'duplicate'
                # Provenance is durable even if the display/capacity step is interrupted.
                row.update(status=status, paper_id=pid, related_pending_confirmation=related, warnings=p.get('import_warnings',[]))
                report[status] += 1
                if related: s.set_state('cnki:relations:'+pid, {'ids':related, 'status':'待确认，不自动合并'})
                if not s.stage(pid, 'cnki_metadata'):
                    s.record(pid,'cnki_metadata','v1','success',{'fulltext':'待补全文','automatic_search':'未接通'})
        except Exception as exc:
            row.update(status='failed', error=str(exc) if isinstance(exc, ValueError) else safe_error(exc)); report['failed'] += 1
        report['rows'].append(row)
        s.set_state(key, report)
    report['finished'] = not report['deferred']
    # Rendering only imports already committed above; rendering never initiates import.
    from .capacity import archived
    from .eligibility import reading_classify as classify, load_whitelist
    from .vault import render_paper
    w = load_whitelist(c['paths']['whitelist'])
    report['render_errors'] = []
    for pid in sorted({r['paper_id'] for r in report['rows'] if r.get('paper_id')}):
        if archived(s,pid) or s.stage(pid,'delivery'): continue
        try: render_paper(s.get(pid),classify(s.get(pid),w),c,s)
        except Exception as exc: report['render_errors'].append({'paper_id':pid,'error':safe_error(exc)})
    if report['render_errors']: report['finished'] = False
    s.set_state(key, report)
    atomic_write(root/'receipts'/(sha+'.json'), json.dumps(report,ensure_ascii=False,indent=2))
    return dict(report, this_import={**{k:report[k]-before_counts[k] for k in before_counts},'deferred':report['deferred']})


def scan(c, s):
    reports = []
    for path in sorted((folders(c)/'pending').iterdir()):
        if not path.is_file() or path.name.startswith('.'): continue
        try: reports.append(import_file(c,s,path))
        except Exception as exc: reports.append({'filename':path.name,'failed':1,'error':safe_error(exc)})
    return {'status':'题录导入可用；自动检索未接通', 'files':reports,
            'repeated_files':sum(bool(r.get('repeated_file')) for r in reports),
            **{k:sum(r.get('this_import',r).get(k,0) for r in reports if not r.get('repeated_file')) for k in ('new','updated','duplicate','failed','deferred')}}


def daily_stats(s, day):
    rows = [json.loads(r[0]) for r in s.db.execute("SELECT value FROM state WHERE key LIKE 'cnki:file:%'")]
    day = day.replace('.','-')
    files = [r for r in rows if r['imported_at'][:10] == day]
    records = [item for r in rows for item in r.get('rows',[]) if item.get('imported_at',r['imported_at'])[:10]==day]
    papers = [p for p in s.papers() if 'cnki' in p.get('sources', [])]
    counts = {k:sum(item['status']==k for item in records) for k in ('new','updated','duplicate','failed')}
    counts['failed'] += sum(bool(r.get('errors')) for r in files)
    return {'files':len(files), **counts,
            'pending_fulltext':sum(not s.stage(p['id'],'pdf') or s.stage(p['id'],'pdf')['status']!='success' for p in papers)}


def command(c, path=None):
    with run_lock(c['paths']['data']):
        s = Store(c['paths']['data'])
        try: return import_file(c,s,path) if path else scan(c,s)
        finally: s.db.close()


def attach_fulltext(c, pid, path, language='unknown', *, lock=True, expected_title=None):
    """Preserve local original and run existing parser; never generate a summary."""
    from .daily import parse_original
    with run_lock(c['paths']['data']) if lock else contextlib.nullcontext():
        s = Store(c['paths']['data'])
        try:
            if not s.get(pid): raise ValueError('未知 paper_id')
            raw = Path(path).read_bytes(); sha = digest(raw)
            target = folders(c)/'fulltext'/pid/(sha+Path(path).suffix.lower())
            atomic_write(target,raw)
            previous_local=s.stage(pid,'local_fulltext')
            if not expected_title and previous_local and previous_local['version']==sha and previous_local['status']=='success':
                detail=json.loads(previous_local['detail'])
                return dict(detail,repeated_file=True)
            detail = {'path':str(target),'sha256':sha,'filename':Path(path).name,
                      'attached_at':now(),'language_declared':language,'permission':'用户通过本地导入提供'}
            if Path(path).suffix.lower() != '.pdf' or not raw.startswith(b'%PDF-'):
                detail.update(status='待转换/待补全文',failed=1)
                s.record(pid,'local_fulltext',sha,'unsupported',detail)
                return detail
            try:
                if len(raw) > c['limits']['pdf_max_mb']*1024*1024: raise ValueError('PDF 大小超过配置')
                if b'%%EOF' not in raw[-2048:]: raise ValueError('PDF 尾部缺失，待补完整文件')
                import pymupdf
                with pymupdf.open(target) as doc:
                    if doc.is_encrypted or doc.is_repaired: raise ValueError('PDF 加密或结构需要修复，待核验')
                parsed = parse_original(target,c,c['paths']['data']/'daily-papers'/pid/sha[:16])
                if not 1 <= parsed.get('page_count',0) <= c['limits']['pdf_max_pages'] or not any(parsed.get('pages',[])):
                    raise ValueError('PDF 未取得有效原文页，待补解析')
                if expected_title and norm(expected_title) not in norm(''.join(parsed.get('pages',[])[:2])):
                    raise ValueError('PDF 首页题名与目标题录未对应，待人工核验；不附到该论文')
            except Exception as exc:
                detail.update(status='待补解析',failed=1,error=safe_error(exc))
                s.record(pid,'local_fulltext',sha,'parse_incomplete',detail)
                return detail
            previous = s.stage(pid,'pdf')
            from .library import portable
            prior = json.loads(previous['detail']) if previous and previous['status']=='success' else {}
            prior_path = portable(prior['path']) if prior.get('path') else None
            prior_hash = digest(prior_path.read_bytes()) if prior_path and prior_path.exists() else prior.get('sha256')
            if prior and prior_hash != sha:
                detail.update(status='已有不同全文，待确认版本；原阶段未覆盖',failed=1)
                s.record(pid,'local_fulltext',sha,'version_conflict',detail)
                return detail
            from .language import detect
            language_decision=dict(detect(parsed),source_sha256=sha)
            s.record(pid,'fulltext_language',sha,'success',language_decision)
            detail.update(status='全文已取得，待处理：总结与审核仍未执行',page_count=parsed['page_count'],
                          fulltext_language=language_decision['language'])
            s.record(pid,'pdf',sha,'success',detail)
            s.record(pid,'local_fulltext',sha,'success',detail)
            return detail
        finally: s.db.close()
