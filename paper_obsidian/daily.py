"""Daily local pipeline on the existing SQLite/source/evidence/cache foundation."""
from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

from .core import ROOT, Store, atomic_write, digest, local_path, now, run_lock, safe_error, today
from .daily_model import Client, DailyError, ServiceBlocked, FIELDS, all_claims
from .analysis import parse_pdf
from .eligibility import reading_classify as classify, load_whitelist
from .journals import JournalService, with_header, patch_note, existing_note
from .evidence import attach_catalog, publishable
from .sources import HTTP, discover, query_signature, relevance, window
from .proceedings import verify_neurips, verify_cvpr
from .vault import START, END, managed_write, link, check_links, text
from .library import portable, reading_path, candidate_path, note_for_id, naming_meta, rewrite_links, rewrite_text, sync_formal_path
from .briefs import daily_brief
from .api_accounting import RunAccounting, guarded as accounting_guard


def read_detail(s, pid, stage):
    row = s.stage(pid, stage)
    return json.loads(row['detail']) if row and row['status'] == 'success' else None


def backup(c, store):
    folder = c['paths']['data'] / 'backups' / 'daily-upgrade'
    if (folder/'complete.json').exists(): return
    folder.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(folder/'papers-before.sqlite3') as out:
        store.db.backup(out)
    manifest={}
    with zipfile.ZipFile(folder/'vault-before.zip','w',zipfile.ZIP_DEFLATED) as z:
        for p in c['paths']['vault'].rglob('*'):
            if p.is_file() and '.obsidian' not in p.parts:
                rel=p.relative_to(c['paths']['vault']).as_posix()
                manifest[rel]=digest(p.read_bytes()); z.write(p,rel)
    for name in ('config.toml','README.md','scripts/task.ps1'):
        if (ROOT/name).exists(): atomic_write(folder/Path(name).name,(ROOT/name).read_bytes())
    atomic_write(folder/'complete.json',json.dumps({'time':now(),'manifest':manifest},ensure_ascii=False,indent=2))


def get_pdf(p,c,s,http):
    old=read_detail(s,p['id'],'pdf')
    if old and portable(old['path']).exists():
        unchanged_url=not old.get('source_url') or old['source_url']==p.get('pdf_url')
        unchanged_revision=(p.get('publication',{}).get('status') in ('published','accepted') or
            not old.get('preprint_updated') or old['preprint_updated']==p.get('dates',{}).get('preprint_updated'))
        if unchanged_url and unchanged_revision:return portable(old['path'])
    if not p.get('pdf_url'): raise DailyError('待补全文：没有开放原文PDF')
    raw=http.get(p['pdf_url'],binary=True)
    target=c['paths']['data']/'pdf'/(p['id']+'-'+digest(raw)[:12]+'.pdf')
    atomic_write(target,raw)
    s.record(p['id'],'pdf',digest(raw),'success',{'path':str(target),'sha256':digest(raw),'source_url':p['pdf_url'],
        'preprint_updated':p.get('dates',{}).get('preprint_updated')})
    return target


def parse_original(path,c,folder):
    source_hash=digest(path.read_bytes())
    cache=folder/'parsed.json'
    if cache.exists():
        parsed=json.loads(cache.read_text('utf-8'))
        if parsed.get('source_sha256')==source_hash:
            return rotation_supplement(path,parsed,cache,freeze=(folder/'reviewed.json').exists())
    pc=copy.deepcopy(c)
    pc['limits']['input_chars_per_call']=10**9  # actual call budgets live in Client
    try:
        parsed=parse_pdf(path,pc)
    except Exception:
        # Alternate parser for rotated/sparse pages, with optional local OCR.
        import pymupdf as fitz
        pages=[]; missing=[]; methods=[]
        with fitz.open(path) as doc:
            if len(doc)>c['limits']['pdf_max_pages']: raise DailyError('原文页数超过配置')
            for i,page in enumerate(doc,1):
                content=page.get_text(sort=True)
                method='pymupdf-sorted'
                if len(re.sub(r'\s','',content))<100:
                    try:
                        tp=page.get_textpage_ocr(language='eng',dpi=160,full=True)
                        content=page.get_text(textpage=tp); method='tesseract-ocr'
                    except Exception: pass
                if len(re.sub(r'\s','',content))<100: missing.append(i)
                pages.append(content); methods.append(method)
        # An unreadable non-core appendix can be delivered with scoped coverage.
        appendix=next((i for i,v in enumerate(pages,1) if re.search(r'(?im)^\s*(?:Appendix|Supplementary Material)\b',v)),None)
        if any(not appendix or n<appendix for n in missing):
            raise DailyError('核心正文存在未可靠读取页，待补解析；不发布确定性结论')
        parsed=attach_catalog({'pages':pages,'page_count':len(pages),'parser':methods,'missing_pages':missing,
            'warning':f'逐页备用文本层/OCR解析；未可靠读取的附录物理页：{missing or "无"}。图内标注未做全面视觉识别；关键事实以原页核对。'})
    parsed['source_sha256']=source_hash
    parsed=rotation_supplement(path,parsed,cache)
    atomic_write(cache,json.dumps(parsed,ensure_ascii=False,indent=2))
    return parsed


def rotation_supplement(path,parsed,cache,freeze=False):
    """Read rotated figure labels with the alternate parser, retain real pages.

    Existing successful reviews remain bound to their original evidence catalog;
    new generation receives the extra original blocks directly.
    """
    if parsed.get('rotation_checked'):return parsed
    import pymupdf as fitz
    supplements=[]
    with fitz.open(path) as doc:
        for n,page in enumerate(doc,1):
            lines=[line for block in page.get_text('dict')['blocks'] for line in block.get('lines',[]) if abs(line['dir'][1])>.1]
            for i,line in enumerate(lines,1):
                value=''.join(span['text'] for span in line['spans'])
                supplements.append({'id':f'p{n:03d}-r{i:03d}','physical_page':n,'text':value,'raw_text':value,'line_start':None,'parser':'pymupdf-rotated-supplement'})
    if supplements and not freeze:
        for b in supplements:
            eid=b['id'];parsed['evidence_catalog'][eid]={k:v for k,v in b.items() if k!='id'}
        parsed['evidence_text']='\n'.join(f"[{eid}]\n{b['text']}" for eid,b in parsed['evidence_catalog'].items())
        parsed['catalog_sha256']=digest(parsed['evidence_catalog'])
    parsed['rotation_checked']=True
    parsed['rotation_supplement']=supplements
    if supplements:
        parsed['warning']='正文按物理页解析；旋转图标注已由备用解析提取并保存'+('，旧审校不声称读取这些补充标注。' if freeze else '，作为原文补充证据输入。')+'图像本身及复杂图表未全面视觉识别；原始PDF是最终依据。'
    atomic_write(cache,json.dumps(parsed,ensure_ascii=False,indent=2))
    return parsed


def translate(p,source,c,s,accounting=None):
    old=read_detail(s,p['id'],'translation')
    if old and not old.get('source_sha256'):
        historical=ROOT/'data/translation-single'/p['id']/'generated.json'
        if historical.exists():old['source_sha256']=json.loads(historical.read_text('utf-8')).get('source_sha256')
    if old and old.get('source_sha256')==digest(source.read_bytes()) and portable(old['path']).exists() and ('daily-translation' not in str(old['path']) or old.get('qa_version')==3):
        return dict(old,path=str(portable(old['path'])),cache_hit=True)
    rel='data/production/daily-translation/'+p['id']+'-'+digest(source.read_bytes())[:12]
    folder=ROOT/rel
    folder.mkdir(parents=True,exist_ok=True)
    # Worker uses a Windows path to the same project, configured with no keys in args.
    job=folder/'job.json'
    atomic_write(job,json.dumps({'source':source.relative_to(ROOT).as_posix(),'folder':rel,
                                'run_id':accounting.run['run_id'] if accounting else None}))
    interpreter=ROOT/'.venv-translation-win/Scripts/python.exe'
    if not interpreter.exists(): raise DailyError('隔离翻译环境缺失')
    def winpath(path):
        if os.name=='nt': return str(path)
        return subprocess.check_output(['wslpath','-w',str(path)],text=True).strip()
    log=folder/'worker.log'
    launch=accounting.track(lambda:accounting.translation_started(rel)) if accounting else None
    with log.open('ab') as f:
        completed=subprocess.run([str(interpreter),'-X','utf8',winpath(ROOT/'tools/translate_daily.py'),winpath(job)],
            cwd=str(ROOT),stdout=f,stderr=f,timeout=c['translation']['timeout_seconds']+120)
    result=folder/'result.json'
    if not result.exists(): raise DailyError('翻译子程序未产出回执，见worker.log')
    detail=json.loads(result.read_text('utf-8'))
    if accounting and launch:
        accounting.track(lambda:accounting.translation_finished(launch,detail))
    if completed.returncode or detail.get('status')!='success':
        raise DailyError(detail.get('message','双语PDF尚未完成，缓存保留'))
    detail['path']=str(portable(detail['path']))
    detail['source_sha256']=digest(source.read_bytes())
    s.record(p['id'],'translation',digest(source.read_bytes()),'success',detail)
    return detail


def urls_from_source(parsed):
    raw='\n'.join(parsed['pages'])
    # Only literal URLs from original, no guessed repository slugs.
    return sorted(set(re.findall(r'https?://(?:github\.com|gitlab\.com)/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',raw)))


def note_body(p,gate,saved,parsed,translation,source,legacy=None):
    result=saved['result'] if saved else legacy
    review=saved.get('review',{}) if saved else (result.get('semantic_review') or result.get('editorial_review') or {})
    is_codex=review.get('reviewer_type')=='codex' or (legacy and legacy.get('editorial_review'))
    label='经Codex原文校对，用户未复核' if is_codex else '自动生成并经AI原文审校，用户未复核'
    page_count=parsed['page_count']
    body=[f"# {text(result['title_zh'])}",'',f"{text(p['title'])}",'','## 1. 基本信息','',
        f"- 作者：{text('；'.join(p.get('authors',[])))}",
        f"- 会议／期刊：{text(p.get('venue',''))}；论文版本年份：{p.get('publication',{}).get('proceedings_year',str(p.get('date',''))[:4])}；正式状态：{gate['publication_status']}",
        '- 资格：'+text(gate['reason'])+'；'+ '；'.join(f"CCF {x.get('ccf_year')}目录 {x.get('grade')}类" for x in gate['evidence'] if x.get('ccf_year')),
        '- 资格仅用于个人阅读筛选；学校成果认定：未核验。历史分区／目录年份不冒充论文发表年份。',
        f"- [官方原文记录]({p.get('publication',{}).get('source') or p.get('url','')}) · [原始英文 PDF]({p.get('pdf_url','')})",
        '- [打开中英对照 PDF](%E4%B8%AD%E8%8B%B1%E5%AF%B9%E7%85%A7.pdf)',
        '- 校对状态：**'+label+'**',
        f"- 处理范围：{text(parsed.get('warning','全文文本层；图像未全面视觉识别'))}",
        '- PDF检查：英文原页与中文译页交替；已作逐页字体、非空、边界及英文原页一致性检查，复杂译文未逐句人工核验。',
        '- 翻译范围：图内文字、部分表体、作者信息、参考文献题录及公式可能保留英文；不能把本文标为全部文字已译。',
        '- 同服务商／模型的AI审校仍可能遗漏错误，不是独立专家验证。' if not is_codex else '- 已有Codex原文校对结论复用；新增译文未宣称经Codex逐句校对。','']
    links=urls_from_source(parsed)
    body += ['代码链接（原文明确给出；未验证可复现）：'+ '、'.join(f'[{u}]({u})' for u in links) if links else '代码链接：原文未找到明确可确认的仓库URL；不补造。','',
        '<details>','<summary>来源、资格与日期</summary>','']
    for evidence in gate['evidence']:
        if evidence.get('source'): body += [f"- [资格依据]({evidence['source']})；核验 {evidence.get('checked_at','未记录')}；{text(evidence.get('evidence') or evidence.get('verification',''))}"]
    for k,v in p.get('dates',{}).items(): body += [f'- {k}：{text(v)}']
    body += ['- 本机发现：'+str(p.get('_discovered_at','见来源记录')),'</details>','','## 2. 一句话理解','',text(result['summary']),'']
    if result.get('schema_version')=='daily-reading-v1':
        keys=[('problem','3. 研究问题与背景'),('method','4. 核心方法'),('innovations','5. 创新点'),('experiments','6. 关键实验与结果'),('results',None),('limitations','7. 局限与问题'),('resources','8. 复现与阅读价值'),('reading','9. 原文定位与阅读建议')]
        from .branches import FIELDS as CROSS_FIELDS
        keys += [(k,v) for k,v in CROSS_FIELDS.items() if k in result['sections']]
        for key,heading in keys:
            if heading: body += ['## '+heading,'']
            for item in result['sections'][key]:
                pages=sorted(set(parsed['evidence_catalog'][eid]['physical_page'] for eid in item['evidence_ids']))
                body += [text(item['text'])+(' （原文物理页 '+ '、'.join(map(str,pages))+'）' if pages else ''),'']
    elif result.get('schema_version')=='concise-evidence-v2':
        # Reuse corrected CLCR text exactly; no model re-generation.
        for key,heading in [('problem','3. 研究问题与背景'),('method','4. 核心方法'),('experiments','6. 关键实验与结果'),('results',None),('limitations','7. 局限与问题'),('resources','8. 复现与阅读价值')]:
            if key=='experiments': body+=['## 5. 创新点','','下述核心模块属于作者提出的方法；文中比较支持的改进见实验结果。不据此断言领域首创。','']
            if heading: body+=['## '+heading,'']
            for item in result['sections'][key]:
                pages=sorted(set(result['evidence_catalog'][eid]['physical_page'] for eid in item['evidence_ids']))
                body += [text(item['text'])+(' （原文物理页 '+ '、'.join(map(str,pages))+'）' if pages else ''),'']
        if result.get('ai_analysis'):
            body += ['基于原文的阅读疑问（AI推断）：','']+[text(x['text'])+'\n' for x in result['ai_analysis']]
        body+=['## 9. 原文定位与阅读建议','','先读摘要和核心方法，再对照主要结果表；详细原文页码见各段定位。','']
    else:
        names={'problem':'3. 研究问题与背景','method':'4. 核心方法','experiments':'6. 关键实验与结果','results':None,'limitations':'7. 局限与问题','resources':'8. 复现与阅读价值','conclusions':'原有总结的关键结论（沿用已校对内容）','inference':'阅读补充（原有AI推断）'}
        for key,heading in names.items():
            section=result.get('sections',{}).get(key)
            if not section: continue
            if key=='experiments': body+=['## 5. 创新点','','作者方法与可支持的改进分别见核心方法及实验比较，未宣称领域首创。','']
            if heading: body+=['## '+heading,'']
            body += [text(section.get('text','')),'']
        body+=['## 9. 原文定位与阅读建议','','保留已有历史论文全文总结及校对结论；建议优先读方法与结果表。','']
    body += [f'[打开双语PDF](%E4%B8%AD%E8%8B%B1%E5%AF%B9%E7%85%A7.pdf)。原文共{page_count}物理页；原文第 p 页对应双语第 2p−1 页（英文）和第 2p 页（中文）。出版印刷页码不能直接作PDF页数组下标。','',
             '维护回执、原文、解析、证据和校对差异保存在项目data目录；此阅读目录只放笔记和双语PDF。']
    if translation.get('skipped'):
        body = [line for line in body if not any(token in line for token in (
            '原始英文 PDF','打开中英对照 PDF','英文原页与中文译页交替','翻译范围：','新增译文未宣称',
            '打开双语PDF','此阅读目录只放笔记和双语PDF'))]
        body += ['', '- [原始中文 PDF](%E5%8E%9F%E5%A7%8B%E4%B8%AD%E6%96%87.pdf)',
                 '- 原文语言：中文（完整文本层检测）。跳过翻译：'+translation['reason'],
                 '- 原文完整性、中文总结与原文审核仍按原流程执行；图表解析限制见处理范围。']
    return '\n'.join(body),label


def archive_legacy_attachments(c,s):
    """Move only program-owned legacy assets, including still-pending papers."""
    vault=c['paths']['vault'];moves=[]
    for source in (vault/'20_附件').glob('*'):
        if not source.is_file() or not re.fullmatch(r'[a-f0-9]{20}-(?:original.*\.pdf|bilingual.*\.pdf|evidence\.md)',source.name):continue
        pid=source.name[:20];target=c['paths']['data']/'archive'/pid/source.name
        if target.exists() and digest(target.read_bytes())!=digest(source.read_bytes()):target=target.with_name(target.stem+'-'+digest(source.read_bytes())[:12]+target.suffix)
        atomic_write(target,source.read_bytes());moves.append((source,target))
    if not moves:return
    rewrite_links(vault,{str(a.resolve()):b for a,b in moves},c)
    for p in s.papers():
        for name in ('pdf','translation'):
            row=s.stage(p['id'],name)
            if not row or row['status']!='success':continue
            detail=json.loads(row['detail'])
            moved=next((b for a,b in moves if portable(detail.get('path','')).resolve()==a.resolve()),None)
            if moved:
                detail['path']=str(moved);s.record(p['id'],name,row['version'],'success',detail)
    for source,target in moves:
        if not source.resolve().is_relative_to(vault.resolve()) or digest(source.read_bytes())!=digest(target.read_bytes()):raise DailyError('旧附件归档验证失败，保留原件')
        source.unlink()
    atomic_write(c['paths']['data']/'archive'/'legacy-asset-migration.json',json.dumps({'time':now(),'moves':[{'from':str(a),'to':str(b)} for a,b in moves]},ensure_ascii=False,indent=2))


from .capacity import guarded, apply as apply_capacity, dormant, archived, inventory


@guarded
def publish(p,gate,c,s,source,parsed,translation,saved=None,legacy=None):
    if not gate['eligible']:
        raise DailyError('正式发表资格未通过，禁止晋升')
    pid=p['id'];vault=c['paths']['vault']
    source_hash=digest(source.read_bytes())
    chinese = bool(translation.get('skipped'))
    if chinese:
        from .language import detect
        summary = s.stage(pid,'daily_summary'); review = s.stage(pid,'auto_review')
        if (detect(parsed)['language'] != 'zh' or parsed.get('source_sha256') != source_hash
                or not summary or summary['status'] != 'success' or summary['version'] != source_hash
                or not review or review['status'] != 'success' or not saved
                or json.loads(summary['detail']) != saved or json.loads(review['detail']) != saved.get('review')):
            raise DailyError('中文跳过翻译仍须完整原文、总结与原文审核回执')
    pdf_name = '原始中文.pdf' if chinese else '中英对照.pdf'
    folder_state='delivery-folder:'+pid
    note=reading_path(p,c,s,formal=True)
    target=note.parent
    relative=target.relative_to(vault).as_posix()
    previous=read_detail(s,pid,'delivery') or {}
    from .branches import primary
    if primary(s,pid)=='cross' and (not saved or saved['result'].get('branch_schema')!='cross-v1' or
            saved.get('review',{}).get('cross_admission',{}).get('eligible') is not True):
        raise DailyError('交叉交付缺少全文领域证据审校；保留候选')
    candidate=note_for_id(c,s,pid)
    old_notes=[q for q in (vault/'10_论文笔记'/f'{pid}.md',candidate) if q and q.exists()]
    old_notes=list(dict.fromkeys(old_notes))
    if len(old_notes)>1:raise DailyError('存在同ID多份旧笔记，拒绝覆盖手写内容')
    body,label=note_body(p,gate,saved,parsed,translation,source,legacy)
    meta={'paper_id':pid,'title':p['title'],'title_zh':(saved['result'] if saved else legacy)['title_zh'],
          'authors':p.get('authors',[]),'venue':p.get('venue'),'publication_status':gate['publication_status'],
          'primary_branch':s.get_state('branch:'+pid,{}).get('primary','ordinary'),'summary_review_status':label,'library_status':'正式库','user_reviewed':False,
          'processing':{'pdf':'success','fulltext':'success','translation':'success','delivery':'complete'},
          'dates':p.get('dates',{}),'eligibility_evidence':gate['evidence'],'school_recognition':'未核验（不影响个人阅读会议通道）'}
    meta['qualification'] = gate
    meta.update(naming_meta(p))
    if chinese:
        meta.update(fulltext_language='zh',translation_skip_reason=translation['reason'])
        meta['processing']['translation']='skipped'
    meta.update(first_discovered_at=p.get('_discovered_at'),first_delivered_at=previous.get('first_completed_at') or previous.get('completed_at') or now())
    stage=c['paths']['data']/'publish-staging'/pid
    stage.mkdir(parents=True,exist_ok=True)
    staged_note=stage/'阅读笔记.md'
    if note.exists():atomic_write(staged_note,note.read_bytes())
    elif old_notes:
        old_path=old_notes[0]
        old_content=old_path.read_text('utf-8')
        old_content=rewrite_text(old_content,old_path,note,{str(old_path.resolve()):note},vault)
        atomic_write(staged_note,old_content)
    managed_write(staged_note,meta,with_header(body,p,gate,s),c)
    # Existing manual regions (outside auto markers) remain byte-preserved;
    # new files use a clearly named, independent personal region.
    if not note.exists() and not old_notes:
        raw=staged_note.read_text('utf-8').replace('## 我的手写笔记','## 10. 我的笔记')
        atomic_write(staged_note,raw)
    atomic_write(stage/pdf_name,portable(translation['path']).read_bytes())
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True)
        staging=vault/('.paper-staging-'+pid)
        staging.mkdir(parents=True,exist_ok=True)
        atomic_write(staging/pdf_name,(stage/pdf_name).read_bytes())
        atomic_write(staging/'阅读笔记.md',staged_note.read_bytes())
        os.replace(staging,target)
    else:
        atomic_write(target/pdf_name,(stage/pdf_name).read_bytes(),c['paths']['data']/'backups')
        atomic_write(note,staged_note.read_bytes(),c['paths']['data']/'backups')
    mapping={str(q.resolve()):note for q in old_notes}
    archive=c['paths']['data']/'archive'/pid
    archive.mkdir(parents=True,exist_ok=True)
    # Preserve historical auto bodies, handwritten regions, evidence and all PDFs.
    migrations=[]
    for q in old_notes+list((vault/'20_附件').glob(pid+'*')):
        destination=archive/q.name
        if destination.exists() and digest(destination.read_bytes())!=digest(q.read_bytes()):
            destination=archive/(q.stem+'-'+digest(q.read_bytes())[:12]+q.suffix)
        atomic_write(destination,q.read_bytes())
        if q.suffix=='.pdf' and 'bilingual' in q.name: mapping[str(q.resolve())]=target/'中英对照.pdf'
        elif q not in old_notes: mapping[str(q.resolve())]=destination
        migrations.append((q,destination))
    rewrite_links(vault,mapping,c)
    for q,destination in migrations:
        if not q.resolve().is_relative_to(vault.resolve()):raise DailyError('归档路径越界')
        if digest(q.read_bytes())!=digest(destination.read_bytes()):raise DailyError('归档校验失败，保留原件')
        q.unlink()  # verified archive exists; this is a move, not deletion of assets
    for k in ('pdf','translation'):
        row=s.stage(pid,k)
        if row and row['status']=='success':
            detail=json.loads(row['detail']); path=portable(detail['path'])
            moved=next((dst for src,dst in migrations if src.resolve()==path.resolve()),None)
            if moved:
                detail['path']=str(moved);s.record(pid,k,row['version'],'success',detail)
    s.set_state(folder_state,relative)
    with s.db:s.db.execute('DELETE FROM state WHERE key=?',('candidate-path:'+pid,))
    version=digest([source_hash,digest((saved or {}).get('result',legacy)),translation.get('output_sha256')])
    detail={'primary_branch':s.get_state('branch:'+pid,{}).get('primary','ordinary'),'note':str(note),'folder':relative,'bilingual':str(target/pdf_name),'completed_at':now(),
            'first_completed_at':previous.get('first_completed_at') or previous.get('completed_at') or now(),
            'summary':(saved['result'] if saved else legacy)['summary'],'review_status':label,
            'delivery_kind':'已有笔记迁移／补齐译文' if legacy else '新论文',
            'source_sha256':source_hash,'bilingual_sha256':digest((target/pdf_name).read_bytes()),
            'metadata_version':digest([p.get('pdf_url'),p.get('dates',{}).get('preprint_updated')]),
            'version_history':((read_detail(s,pid,'delivery') or {}).get('version_history',[])+
                ([{k:v for k,v in (read_detail(s,pid,'delivery') or {}).items() if k in ('source_sha256','bilingual_sha256','completed_at')}] if read_detail(s,pid,'delivery') else []))}
    if chinese:
        detail.update(fulltext_language='zh',translation_skipped=True,translation_skip_reason=translation['reason'])
    detail['qualification'] = gate
    s.record(pid,'delivery',version,'success',detail)
    s.record(pid,'note',version,'success',{'path':str(note)})
    old_archive = archived(s, pid)
    if old_archive and old_archive['area'] == 'candidate':
        with s.db: s.db.execute('DELETE FROM state WHERE key=?', ('capacity:archive:'+pid,))
    return detail


def completed(p,c,s):
    d=read_detail(s,p['id'],'delivery')
    if not d:return None
    note,pdf=portable(d['note']),portable(d['bilingual'])
    if not note.exists() or not pdf.exists():return None
    if d.get('translation_skipped'):
        summary=s.stage(p['id'],'daily_summary'); review=s.stage(p['id'],'auto_review')
        language=s.stage(p['id'],'fulltext_language')
        if (d.get('fulltext_language')!='zh' or not summary or summary['status']!='success'
                or summary['version']!=d['source_sha256'] or not review or review['status']!='success'
                or not language or language['version']!=d['source_sha256']
                or json.loads(language['detail']).get('language')!='zh'):return None
    translation=read_detail(s,p['id'],'translation')
    if translation and 'daily-translation' in str(translation['path']) and translation.get('qa_version')!=3:return None
    if d.get('metadata_version')!=digest([p.get('pdf_url'),p.get('dates',{}).get('preprint_updated')]):return None
    # Content fingerprint changes mark a new version for processing, not a duplicate paper.
    src=read_detail(s,p['id'],'pdf')
    if src and portable(src['path']).exists() and digest(portable(src['path']).read_bytes())!=d['source_sha256']:return None
    if digest(pdf.read_bytes())!=d['bilingual_sha256']:return None
    return d


def official_upgrade(p,http):
    links=[p.get('url',''),p.get('pdf_url','')]+[x.get('source','') for x in [p.get('publication',{})]]
    for url in links:
        if re.match(r'https://proceedings\.neurips\.cc/(?:paper|paper_files/paper)/\d{4}/hash/',url):return verify_neurips(url,http)
        if re.match(r'https://openaccess\.thecvf\.com/content/CVPR\d{4}/html/',url):return verify_cvpr(url,http)
    return None


def search(c,env,s,http,w,report):
    """One durable page at a time. Replaying an uncommitted page is safe."""
    from datetime import date, timedelta
    remaining=max(0,c['limits']['candidates_per_day']-s.used('daily_search_records'))
    remaining=min(remaining,c.get('_search_budget',remaining))
    for name in c['search']['sources']:
        signature=query_signature(c,name)+':v2:'+digest([c['search']['lookback_days'],w.get('openreview_profiles',[]) if name=='openreview' else []])[:12]
        source_name=name+(':cross' if c.get('_branch')=='cross' else '')
        prior=s.get_state('active-query:'+source_name)
        if prior != signature:
            s.set_state('query-change:'+name+':'+signature,{'previous':prior,'legacy_preserved':query_signature(c,name),'current':signature,'at':now(),'policy':'old cursor preserved; new scoped cursor'})
            s.set_state('active-query:'+source_name,signature)
        status={'status':'success','pages':0,'returned':0,'retained':0,'truncated':True,'windows':[], 'stop_reason':'max_pages','configured_max_pages':c['search']['max_pages']}
        report['sources'][source_name]=status
        if remaining<=0:
            status.update(status='skipped',stop_reason='daily_record_limit');continue
        blocked=s.get_state(signature+':unavailable',{})
        if blocked.get('day')==today():
            status.update(status='failed',stop_reason='source_unavailable_cooldown',error=blocked.get('error'));continue
        # Both modes share the configured page budget. For a one-page budget,
        # alternate days so neither lane starves. Fixed historical start stays.
        page_budget=c.get('_page_budget',c['search']['max_pages'])
        status['allocated_pages']=page_budget
        modes=['incremental','backfill'] if page_budget>1 else (['incremental'] if date.fromisoformat(today()).toordinal()%2 else ['backfill'])
        for slot in range(page_budget):
            if remaining<=0:status['stop_reason']='daily_record_limit';break
            if (c['paths']['data']/'STOP').exists():status['stop_reason']='paused';break
            mode=modes[slot%len(modes)]
            key=signature+':'+mode
            if mode=='backfill' and date.fromisoformat(today()).toordinal()%2:
                queue_key=signature+':incremental-deferred'
                queue=s.get_state(queue_key,[])
                while queue and not s.get_state(queue[0],{}).get('resume'):
                    queue.pop(0);s.set_state(queue_key,queue)
                if queue:
                    key=queue[0];mode='incremental-recovery'
            state=s.get_state(key,{})
            end=c['search'].get('end_date') or today()
            if mode=='incremental' and state.get('resume',{}).get('end',end)<end and 'batch' not in state:
                deferred_key=key+':deferred:'+state['resume']['end']
                s.set_state(deferred_key,state)
                queue_key=signature+':incremental-deferred'
                queue=s.get_state(queue_key,[])
                s.set_state(queue_key,list(dict.fromkeys(queue+[deferred_key])))
                status.setdefault('deferred_windows',[]).append(dict(state['resume'],reason='new_incremental_probe; unfinished cursor retained'))
                state={} 
            if not state.get('resume'):
                start=c['search']['start_date'] if mode=='backfill' else max(c['search']['start_date'],(date.fromisoformat(end)-timedelta(days=c['search']['lookback_days'])).isoformat())
                if mode=='backfill' and state.get('complete'):
                    status['stop_reason']='backfill_complete';continue
                state['resume']={'start':start,'end':end,'page':0}
                s.set_state(key,state)
            r=state['resume'];start,end=r['start'],r['end']
            sc=copy.deepcopy(c)
            # Page size never changes with remaining daily quota.
            sc['search'].update(max_pages=1,_page_start=r['page'])
            entry={'mode':mode,'window':[start,end],'page':r['page'],'records':0,'saved':0,'replayed':bool(state.get('batch'))}
            status['windows'].append(entry)
            try:
                if 'batch' not in state:
                    papers,truncated=discover(name,sc,http,start,end,w.get('openreview_profiles',[]))
                    state.update(batch=papers,truncated=truncated)
                    s.set_state(key,state)  # WAL-backed save BEFORE cursor advance
                    status['pages']+=1
                batch=state['batch'];entry['records']=len(batch)
                chosen=batch[:remaining]
                for p in chosen:
                    if relevance(p,c)>0:
                        pid=s.ingest(p,c['limits']['candidates_per_day'])
                        if not pid:
                            status['stop_reason']='ingest_deferred';break
                        status['retained']+=1
                        s.spend('daily_retained',2147483647)
                        if not classify(s.get(pid),w)['eligible']:
                            try:
                                upgrade=official_upgrade(p,http)
                                if upgrade:
                                    upgrade['aliases']=sorted(set(upgrade['aliases']+p['aliases']))
                                    s.ingest(upgrade,c['limits']['candidates_per_day'])
                            except Exception:pass
                    # An interrupted commit can replay this record, never skip it.
                    state['batch']=state['batch'][1:]
                    with s.db:
                        s.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)',(key,json.dumps(state)))
                        s.db.execute('INSERT INTO usage VALUES (?,?,1) ON CONFLICT(day,kind) DO UPDATE SET n=n+1',(today(),'daily_search_records'))
                    remaining-=1;status['returned']+=1;entry['saved']+=1
                if state['batch']:
                    status['truncated']=True
                    status['stop_reason']='daily_record_limit' if remaining<=0 else 'ingest_deferred'
                    break
                more=state.pop('truncated');state.pop('batch')
                entry['truncated']=more
                if more:r['page']+=1
                else:
                    state.pop('resume');state.update(last_success=end,complete=mode=='backfill')
                s.set_state(key,state)
                status['truncated']=more
            except Exception as exc:
                status.update(status='failed',error=safe_error(exc),stop_reason='source_failure',truncated=True)
                if '403' in str(exc):
                    s.set_state(signature+':unavailable',{'day':today(),'error':safe_error(exc)})
                    status['stop_reason']='source_unavailable_403'
                break
        checkpoint=s.get_state('daily-source-status:'+today(),{})
        checkpoint[source_name]=status;s.set_state('daily-source-status:'+today(),checkpoint)


def usage_summary(c):
    totals={'prompt_tokens':0,'completion_tokens':0,'total_tokens':0,'unreceipted_requests':0,'translation_requests':0,'cost':'unknown'}
    for path in (c['paths']['data']/'daily-api').glob('*/attempts.json'):
        for row in json.loads(path.read_text('utf-8')):
            if row['started'][:10]!=today():continue
            u=row.get('usage',{})
            if not u:totals['unreceipted_requests']+=1
            for k in ('prompt_tokens','completion_tokens','total_tokens'):totals[k]+=u.get(k) or 0
    for path in (c['paths']['data']/'daily-translation').glob('*/requests.json'):
        for row in json.loads(path.read_text('utf-8')):
            if row['started'][:10]!=today():continue
            totals['translation_requests']+=1;u=row.get('usage',{})
            if not u:totals['unreceipted_requests']+=1
            for k in ('prompt_tokens','completion_tokens','total_tokens'):totals[k]+=u.get(k) or 0
    return totals


def uncertain_papers(c):
    """Do not replace an interrupted paid request with a differently keyed retry."""
    identifiers=set()
    for path in (c['paths']['data']/'daily-api').glob('*/attempts.json'):
        attempts=json.loads(path.read_text('utf-8'))
        if not attempts or (path.parent/'result.json').exists():continue
        row=attempts[-1]
        if row['status'] not in ('pending','uncertain'):continue
        from .paid_retry import available
        if available(path.parent,row) or (path.parent/f'response-{len(attempts)}.json').exists():continue
        request=json.loads((path.parent/'request.json').read_text('utf-8'))
        if request.get('paper_id'):identifiers.add(request['paper_id'])
        else:
            payload=json.loads(request['body']['messages'][1]['content'])
            if payload.get('title'):identifiers.add(payload['title'])
    return identifiers


def run_daily(c,env,*,fetch=True,metadata_only=False,only=None,max_complete=None,offline=False):
    if offline:
        from .briefs import offline_replay
        return offline_replay(c,maintenance=(c['paths']['data']/'MAINTENANCE').exists())
    data=c['paths']['data']
    with run_lock(data):
        if (data/'STOP').exists():
            return {'status':'paused','stopped':True,'fetch_performed':False}
        s=Store(data);http=HTTP(c,env);w=load_whitelist(c['paths']['whitelist'])
        apply_capacity(c,s)
        journals=JournalService(c,env,s)
        backup(c,s)
        archive_legacy_attachments(c,s)
        with s.db:
            for old in s.db.execute('SELECT id,started,report FROM runs WHERE ended IS NULL').fetchall():
                interrupted=json.loads(old['report']) if old['report'] else {'started':old['started']}
                interrupted.update(status='interrupted',ended=None,recovery_detected_at=now(),
                    message='此前进程未正常收尾；实际结束时间未知，成功阶段和未知模型回执保留')
                s.db.execute('UPDATE runs SET report=? WHERE id=?',(json.dumps(interrupted,ensure_ascii=False),old['id']))
        client=Client(c,env,s)
        uncertain=uncertain_papers(c)
        with s.db:rid=s.db.execute('INSERT INTO runs(started) VALUES (?)',(now(),)).lastrowid
        report={'started':now(),'run_id':str(rid),'entry':'daily','fetch_performed':fetch,'sources':{},'delivered':[],'cached':[],'failures':[],
                'pending':[],'journal_pending':not bool(w['journals']),'stopped':False}
        client.run_id=str(rid)
        accounting=accounting_guard(report,lambda:RunAccounting(c,env,s,rid,report['started']))
        if accounting:accounting_guard(report,accounting.begin)
        # Durable running checkpoint; a killed process never acquires a fake end.
        with s.db:s.db.execute('UPDATE runs SET report=? WHERE id=?',(json.dumps(report,ensure_ascii=False),rid))
        atomic_write(data/'daily-runs'/f'{rid}.json',json.dumps(report,ensure_ascii=False,indent=2))
        try:
            from .cnki import scan
            try:
                report['cnki'] = scan(c,s)
            except Exception as exc:
                report['cnki'] = {'status':'导入失败；其他来源继续','error':safe_error(exc)}
            # CNKI browser has no recurring paid authorization. Existing scheduler
            # only observes its saved state; local imports and other sources continue.
            try:
                from .cnki_browser import stats as browser_stats
                report['cnki_browser']=browser_stats(s,today())
            except Exception as exc:
                report['cnki_browser']={'status':'读取失败；其他来源继续','error':safe_error(exc)}
            if fetch:
                if c.get('cross',{}).get('enabled'):
                    from .branches import search_config
                    lanes=[copy.deepcopy(c),search_config(c)]
                    if int(today().replace('-',''))%2:lanes.reverse()
                    for index,lane in enumerate(lanes):
                        lane['_page_budget']=(c['search']['max_pages']+int(index==0))//2
                        lane['_search_budget']=max(0,(c['limits']['candidates_per_day']-s.used('daily_search_records'))//(2 if lane is lanes[0] else 1))
                        search(lane,env,s,http,w,report)
                else:search(c,env,s,http,w,report)
            papers=s.papers()
            from .branches import assign, used as branch_used, admission, primary
            for p in papers:
                p['_branch']=assign(s,p,c)
                row=s.db.execute('SELECT created FROM papers WHERE id=?',(p['id'],)).fetchone()
                p['_discovered_at']=row[0] if row else '未记录'
            # Recent methods first, MokA is the initial implementation preference.
            # Finish already-reviewed partial work before paying for another
            # paper; e.g. a successful translation awaiting publication repair.
            papers.sort(key=lambda p:(2 if read_detail(s,p['id'],'daily_summary') else int(p['title'].startswith('MokA:')),
                p.get('date',''),relevance(p,c)),reverse=True)
            # Equal quality/cache/date bands prefer underrepresented domains.
            from .branches import matches
            domain_counts={}
            for old in papers:
                if completed(old,c,s):
                    for domain in matches(old):domain_counts[domain]=domain_counts.get(domain,0)+1
            papers.sort(key=lambda p:(bool(read_detail(s,p['id'],'daily_summary')),p.get('date',''),-min([domain_counts.get(d,0) for d in matches(p)] or [0])),reverse=True)
            if only:papers=[p for p in papers if p['id'] in only]
            quota=max_complete if max_complete is not None else c['daily']['complete_per_day']
            new_today=branch_used(s,'ordinary')
            attempted={"ordinary":0,"cross":0}
            for p in papers:
                if (data/'STOP').exists():report['stopped']=True;break
                if dormant(p,c,s):
                    report['pending'].append({'id':p['id'],'title':p['title'],'status':'已归档；排序依据未变化，复用完成阶段并跳过处理'})
                    continue
                gate=classify(p,w)
                if not gate['eligible']:
                    try:
                        upgrade=official_upgrade(p,http)
                        if upgrade:
                            upgrade['aliases']=sorted(set(upgrade['aliases']+p.get('aliases',[])))
                            s.ingest(upgrade,c['limits']['candidates_per_day'])
                            p=dict(s.get(p['id']),_discovered_at=p['_discovered_at'],_branch=p['_branch'])
                            gate=classify(p,w)
                    except Exception:pass
                # Annotation has its own cache and failure boundary. It cannot
                # grant eligibility, invalidate summaries, or abort PDF work.
                try:
                    if p.get('venue_kind')=='journal':journals.annotate(p,gate)
                    previous_note=existing_note(c,s,p['id'])
                    if previous_note:patch_note(previous_note,p,gate,c,s)
                    elif p.get('venue_kind')=='journal' and not gate['eligible']:
                        from .vault import render_paper
                        render_paper(p,gate,c,s)
                except Exception as exc:
                    report.setdefault('journal_warnings',[]).append({'id':p['id'],'error':safe_error(exc)})
                branch=p['_branch']
                if branch=='cross' and not c.get('cross',{}).get('enabled'):continue
                if any(term.lower() in (p.get('title','')+' '+p.get('abstract','')).lower() for term in (c.get('cross',{}).get('exclude',[]) if branch=='cross' else c['search']['exclude'])):
                    report['pending'].append({'id':p['id'],'title':p['title'],'status':'主题不优先：与当前软件AI方法阅读方向分开保留'})
                    continue
                jcr_pending=None
                if not gate['eligible']:
                    if gate.get('fulltext_verification_allowed') and not metadata_only:
                        try:
                            if p['id'] in uncertain or p['title'] in uncertain:raise DailyError('自动学科审核待审：未知付费请求持续阻断')
                            if (branch=='ordinary' and new_today>=quota) or (branch=='cross' and (branch_used(s,'cross')>=min(1,c['cross']['complete_per_day']) or not admission(c,s,p))):raise DailyError('自动学科审核待审：无当日交付名额')
                            hold=s.get_state('cnki:processing:'+p['id'])
                            if hold and not hold.get('approved'):raise DailyError('自动学科审核待审：知网阅读预算未授权')
                            source=get_pdf(p,c,s,http)
                            source_hash=digest(source.read_bytes())
                            folder=data/'daily-papers'/p['id']/source_hash[:16]
                            folder.mkdir(parents=True,exist_ok=True)
                            parsed=parse_original(source,c,folder)
                            s.record(p['id'],'jcr_fulltext_verification',source_hash,'pending',
                                     {'parsed_file':str(folder/'parsed.json'),'source_sha256':source_hash,
                                      'reason':'全文已解析，进入自动学科审核预检'})
                            from .jcr_auto import run as review_jcr
                            jcr_pending=review_jcr(p,w,gate,parsed,folder,client,s)
                            gate=dict(gate,eligible=True,reason=jcr_pending['reason'])
                        except Exception as exc:
                            report.setdefault('journal_warnings',[]).append({'id':p['id'],'error':safe_error(exc)})
                            gate['reason']=str(exc) if isinstance(exc,DailyError) else '自动学科审核待审：全文或响应处理失败'
                    if not gate['eligible']:
                        report['pending'].append({'id':p['id'],'title':p['title'],'status':gate['reason']});continue
                if branch=='ordinary' and not jcr_pending:sync_formal_path(p,c,s)
                existing=completed(p,c,s)
                if existing:report['cached'].append(p['id']);continue
                cnki_hold=s.get_state('cnki:processing:'+p['id'])
                if cnki_hold and not cnki_hold.get('approved'):
                    report['pending'].append({'id':p['id'],'title':p['title'],
                        'status':'全文已取得，待处理；阅读预算待授权' if read_detail(s,p['id'],'pdf') else '知网题录候选，待全文及阅读预算'})
                    continue
                if p['id'] in uncertain or p['title'] in uncertain:
                    report['pending'].append({'id':p['id'],'title':p['title'],'status':'模型请求未确认返回、用量未知；跨日不自动重发，保留待完成'})
                    continue
                previous=s.stage(p['id'],'daily')
                previous_pdf=read_detail(s,p['id'],'pdf')
                previous_message=json.loads(previous['detail']).get('message','') if previous else ''
                if previous and (previous['status']=='review_required' or previous_message.startswith('有限修正后')) and previous_pdf and portable(previous_pdf['path']).exists() and previous['version'] in (digest(portable(previous_pdf['path']).read_bytes()),digest(p)):
                    report['pending'].append({'id':p['id'],'title':p['title'],'status':'待复核：有限AI修正后仍有关键事实冲突；本轮不重复付费'})
                    continue
                if branch=='cross' and not read_detail(s,p['id'],'delivery'):
                    if branch_used(s,'cross')>=min(1,c['cross']['complete_per_day']) or not admission(c,s,p):
                        report['pending'].append({'id':p['id'],'title':p['title'],'status':'交叉分支当日配额或容量保留门槛未通过；未启动翻译'})
                        continue
                legacy=read_detail(s,p['id'],'fulltext')
                legacy=legacy if branch=='ordinary' and legacy and publishable(legacy) else None
                # Historical/accepted cached notes migrate without occupying new-paper quota.
                if metadata_only or (not legacy and not read_detail(s,p['id'],'delivery') and ((max_complete is None and branch=='ordinary' and new_today>=quota) or attempted[branch]>=max(6,quota*3))):
                    report['pending'].append({'id':p['id'],'title':p['title'],'status':'资格已核验，等待后续批次'});continue
                attempted[branch]+=1
                source_hash=None
                try:
                    source=get_pdf(p,c,s,http)
                    source_hash=digest(source.read_bytes())
                    folder=data/'daily-papers'/p['id']/source_hash[:16]
                    folder.mkdir(parents=True,exist_ok=True)
                    parsed=parse_original(source,c,folder)
                    if 'cnki' in p.get('sources',[]):
                        from .language import detect
                        language=detect(parsed)
                        s.record(p['id'],'fulltext_language',source_hash,'success',dict(language,source_sha256=source_hash))
                        if language['language']=='unknown':raise DailyError('知网全文语言未知，待确认；尚未调用总结或翻译')
                    if legacy:
                        old_catalog=legacy.get('evidence_catalog')
                        if old_catalog:
                            if any(parsed['evidence_catalog'].get(eid)!=block for eid,block in old_catalog.items()):legacy=None
                        else:
                            marker=ROOT/'data/translation-single'/p['id']/'generated.json'
                            known_source=json.loads(marker.read_text('utf-8')).get('source_sha256') if marker.exists() else None
                            if known_source!=source_hash:legacy=None
                    saved=None
                    if not legacy:
                        saved=client.generate_review(p,parsed,folder)
                        s.record(p['id'],'auto_review',digest(saved),'success',saved['review'])
                        s.record(p['id'],'daily_summary',source_hash,'success',saved)
                        s.record(p['id'],'fulltext',source_hash,'success',dict(saved['result'],semantic_review=saved['review'],basis=saved['scope']))
                    if (data/'STOP').exists():report['stopped']=True;break
                    if jcr_pending:
                        from .jcr_auto import finalize
                        finalize(p,w,jcr_pending,parsed,folder,client,s)
                        gate=classify(p,w)
                        if not gate['eligible']:raise DailyError('自动学科审核待审：最终资格未通过')
                    from .language import detect
                    if detect(parsed)['language'] != 'zh' and client.blocked and env.get('API_BASE_URL')==env.get('TRANSLATION_API_BASE_URL') and env.get('API_KEY')==env.get('TRANSLATION_API_KEY'):
                        if not read_detail(s,p['id'],'translation'):raise ServiceBlocked('同一服务认证/余额错误，本次停止新增翻译调用')
                    from .language import prepare
                    tr=prepare(p,source,parsed,c,s,accounting=accounting)
                    if (data/'STOP').exists():report['stopped']=True;break
                    prior_delivery=read_detail(s,p['id'],'delivery')
                    delivery=publish(p,gate,c,s,source,parsed,tr,saved,legacy)
                    s.record(p['id'],'daily',source_hash,'success',{'delivery':delivery['folder'],'completed_at':now()})
                    report['delivered'].append({'id':p['id'],'title':p['title'],**delivery})
                    if not legacy and not prior_delivery:
                        s.spend('daily_cross_complete' if branch=='cross' else 'daily_complete',2147483647)
                        new_today=branch_used(s,'ordinary')
                    if max_complete is not None and len(report['delivered'])>=max_complete:break
                except Exception as exc:
                    message=str(exc) if isinstance(exc,(DailyError,ServiceBlocked)) else safe_error(exc)
                    review_required=message.startswith('有限修正后')
                    s.record(p['id'],'daily',source_hash or digest(p),'review_required' if review_required else 'pending',{'error':safe_error(exc),'message':message,'updated':now()})
                    report['failures'].append({'id':p['id'],'title':p['title'],'error':safe_error(exc),'message':message})
                    if read_detail(s,p['id'],'delivery'):continue
                    candidate=candidate_path(p,c,s)
                    managed_write(candidate,dict(naming_meta(p),paper_id=p['id'],title=p['title'],library_status='待完成',daily_status=message),
                        '# '+text(p['title'])+'\n\n资格：'+text(gate['reason'])+'\n\n当前待完成步骤：'+text(message)+
                        '\n\n原文、成功草稿、审校和翻译缓存已保留在项目数据目录。两份阅读文件完整后才进入10_论文。\n\n'+
                        '[官方原文]('+p.get('url','')+')',c)
                    patch_note(candidate,p,gate,c,s)
                    apply_capacity(c,s,incumbents={(x['area'],x['id']) for x in inventory(c,s)[0] if x['id'] != p['id'] and not x['archived']})
            report['eligible_queued']=sum(classify(p,w)['eligible'] and not completed(p,c,s) for p in s.papers())
        except Exception as exc:
            report['failures'].append({'title':'运行级错误','error':safe_error(exc),'message':safe_error(exc)})
            report['eligible_queued']=0
        finally:
            # KeyboardInterrupt/SystemExit are unfinished, not normal completed runs.
            # Accounting must not mask the active exception or capacity/brief errors.
            if sys.exc_info()[0] is not None:
                journals.close();s.db.close();http.client.close()
            else:
                report['model_requests']=client.network
                if accounting:accounting_guard(report,lambda:accounting.finish(report))
        # Existing finalization remains outside ancillary accounting's error boundary.
        try:
            report['capacity']=apply_capacity(c,s)
            report.update(ended=now(),model_requests=client.network,model_cache_hits=client.hits,usage=usage_summary(c))
            report['status']='paused' if report['stopped'] else ('partial' if report['failures'] or report['pending'] or any(x.get('status')=='failed' for x in report['sources'].values()) else 'complete')
            report['brief']=daily_brief(c,s,report)
            report['links']=check_links(c['paths']['vault'])
            s.set_state('last_run',report)
            with s.db:s.db.execute('UPDATE runs SET ended=?,report=? WHERE id=?',(report['ended'],json.dumps(report,ensure_ascii=False),rid))
            atomic_write(data/'last-run.json',json.dumps(report,ensure_ascii=False,indent=2))
            atomic_write(data/'daily-runs'/f'{rid}.json',json.dumps(report,ensure_ascii=False,indent=2))
        finally:
            journals.close();s.db.close();http.client.close()
        return report
