"""One local paper only. Bounded, receipted requests; never regenerates summaries.

Uses the recommended pdf2zh_next streaming API (debug/in-process mode).
The version-pinned adapter sets BabelDOC's documented parse-only/working-dir
options and wraps the actual OpenAI request boundary, including all fallbacks.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import tomllib
import uuid
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper_obsidian.core import Store, atomic_write, digest, load_config, now, run_lock, secrets
from paper_obsidian.vault import START, END, link, check_links
from paper_obsidian.library import portable, note_for_id, candidate_path


def existing_reading_delivery(cfg):
    """Legacy command reuses a formal delivery without republishing old assets."""
    from paper_obsidian.daily import completed
    c=load_config()
    with run_lock(c['paths']['data']):
        s=Store(c['paths']['data'])
        try:
            p=s.get(cfg['paper_id'])
            if not p:return None
            stage=s.stage(p['id'],'delivery')
            if stage:
                d=completed(p,c,s)
                if not d:raise ValueError('已有正式交付需更新，请使用daily恢复，旧单篇命令不覆盖版本')
                return {'status':'success','cache_hit':True,'output':str(portable(d['bilingual'])),
                        'note':str(portable(d['note'])),'output_sha256':d['bilingual_sha256'],
                        'network_attempts_this_run':0,'existing_formal_delivery_reused':True}
            row=s.stage(p['id'],'pdf')
            if row and row['status']=='success':cfg['source']=str(portable(json.loads(row['detail'])['path']))
            note=note_for_id(c,s,p['id'])
            if note:cfg['note']=str(note)
            cfg['output']=str(c['paths']['data']/'archive'/p['id']/(p['id']+'-bilingual.pdf'))
            return None
        finally:s.db.close()


class LimitError(RuntimeError):
    pass


class Meter:
    def __init__(self, cfg, env, folder, offline=False):
        self.cfg, self.env, self.folder, self.offline = cfg, env, folder, offline
        self.path = folder / 'requests.json'
        self.rows = json.loads(self.path.read_text('utf-8')) if self.path.exists() else []
        self.lock = threading.Lock()
        source=Path(cfg.get('source',''))
        self.source_sha256=digest(source.read_bytes()) if source.is_file() else None
        self.network = 0
        self.hits = 0

    def save(self):
        atomic_write(self.path, json.dumps(self.rows, ensure_ascii=False, indent=2))

    def usage(self):
        keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
        received = [r for r in self.rows if r.get('usage')]
        return dict(requests=len(self.rows), network_attempts_this_run=self.network,
                    response_cache_hits_this_run=self.hits,
                    **{k: sum(r['usage'][k] for r in received) if received and all(isinstance(r['usage'].get(k), int) for r in received) else None for k in keys},
                    unreceipted_requests=sum(not bool(r.get('usage')) for r in self.rows),
                    cost='unknown')

    def wrap(self, client):
        original = client.chat.completions.create
        client.max_retries = 0
        def create(**kwargs):
            with self.lock:
                if self.offline:
                    raise LimitError('Model requests are prohibited in preflight')
                kwargs.update(max_tokens=self.cfg['max_output_tokens'])
                kwargs['extra_body'] = {'thinking': {'type': self.cfg['thinking']}}
                kwargs.pop('reasoning_effort', None)
                key = digest([self.env['TRANSLATION_API_BASE_URL'], kwargs])
                cache = self.folder / 'responses' / (key + '.json')
                if cache.exists():
                    from openai.types.chat import ChatCompletion
                    raw=json.loads(cache.read_text('utf-8'))
                    matching=[r for r in self.rows if r['key']==key]
                    if matching and matching[-1]['status']!='success':
                        matching[-1].update(status='success',usage=raw.get('usage') or {})
                        self.save()
                    self.hits += 1
                    return ChatCompletion.model_validate(raw)
                attempts = [r for r in self.rows if r['key'] == key]
                # Recover response saved before a crash in cache/ledger commit.
                if attempts:
                    index = self.rows.index(attempts[-1]) + 1
                    receipt = self.folder / 'receipts' / (str(index).zfill(4) + '.json')
                    if receipt.exists():
                        raw = json.loads(receipt.read_text('utf-8'))
                        choices = raw.get('choices', [])
                        if choices and choices[0].get('finish_reason') == 'stop' and choices[0].get('message', {}).get('content'):
                            from openai.types.chat import ChatCompletion
                            response = ChatCompletion.model_validate(raw)
                            attempts[-1].update(status='success', usage=raw.get('usage') or {})
                            self.save()
                            atomic_write(cache, json.dumps(raw, ensure_ascii=False))
                            self.hits += 1
                            return response
                # Library fallbacks can change messages/model; they must not
                # bypass an unresolved request for this translation job.
                if any(r['key'] != key and (r['status'] in ('pending','uncertain','invalid') or r.get('previous_status') in ('pending','uncertain','invalid')) for r in {r['key']:r for r in self.rows}.values()):
                    raise LimitError('Another translation request is unresolved; fallback prohibited')
                if self.source_sha256:
                    for base in (ROOT/'data/production/daily-translation',ROOT/'data/translation-single'):
                        for ledger in base.glob('*/requests.json'):
                            if ledger.resolve()==self.path.resolve(): continue
                            rows=json.loads(ledger.read_text('utf-8'))
                            job=ledger.parent/'job.json'
                            old_hash=None
                            if job.exists():
                                source=ROOT/json.loads(job.read_text('utf-8'))['source']
                                if source.is_file():old_hash=digest(source.read_bytes())
                            if any((r.get('source_sha256') or old_hash)==self.source_sha256 and (r['status'] in ('pending','uncertain','invalid') or r.get('previous_status') in ('pending','uncertain','invalid')) for r in {r['key']:r for r in rows}.values()):
                                raise LimitError('Same PDF has unresolved translation in another entry; resume original ledger')
                authorized = False
                # Never automatically replay a request whose outcome is unknown.
                if attempts and (attempts[-1]['status'] in ('pending', 'uncertain', 'invalid') or attempts[-1].get('previous_status') in ('pending','uncertain','invalid')):
                    from paper_obsidian.paid_retry import consume
                    authorized = consume(self.folder, attempts[-1])
                    if not authorized: raise LimitError('Uncertain or invalid earlier request requires review')
                if sum(r['status']!='prepared' for r in attempts) >= self.cfg['max_attempts_per_request'] and not authorized:
                    raise LimitError('Per-request attempts exhausted')
                if self.cfg.get('max_requests') is not None and len(self.rows) >= self.cfg['max_requests']:
                    raise LimitError('Cumulative request limit reached')
                # UTF-8 byte count is a conservative input token reservation.
                reserve = len(json.dumps(kwargs, ensure_ascii=False).encode('utf-8')) + 256 + self.cfg['max_output_tokens']
                spent = sum(r.get('usage', {}).get('total_tokens') or r['reservation'] for r in self.rows)
                if self.cfg.get('max_total_tokens') is not None and spent + reserve > self.cfg['max_total_tokens']:
                    raise LimitError('Cumulative token reservation limit reached')
                from paper_obsidian.api_accounting import source_id
                row = dict(key=key, status='prepared', source_sha256=self.source_sha256, started=now(), reservation=reserve,
                           run_id=self.cfg.get('run_id'), request_id=uuid.uuid4().hex,
                           source_id=source_id(self.env, 'TRANSLATION_'),
                           requested_model=kwargs.get('model') or self.env.get('TRANSLATION_MODEL'))
                self.rows.append(row)
                self.save()  # Persist reservation before any network activity.
                row['status']='pending'
                self.save()
                self.network += 1
                try:
                    response = original(**kwargs)
                    raw = response.model_dump(mode='json')
                    row.update(response_id=response.id, response_model=response.model,
                               usage=raw.get('usage') or {}, finished=now())
                    atomic_write(self.folder / 'receipts' / (str(len(self.rows)).zfill(4) + '.json'), json.dumps(raw, ensure_ascii=False, indent=2))
                    choice = response.choices[0]
                    text = choice.message.content or ''
                    if choice.finish_reason != 'stop' or not text.strip() or re.search(r'<think>', text):
                        row['status'] = 'invalid'
                        raise ValueError('Empty, truncated or thinking-only response')
                    row['status'] = 'success'
                    atomic_write(cache, json.dumps(raw, ensure_ascii=False))
                    return response
                except Exception as exc:
                    if row['status'] != 'invalid':
                        status = getattr(exc, 'status_code', None)
                        row.update(status='retryable' if status == 429 else 'uncertain', error=type(exc).__name__, http_status=status)
                    raise
                finally:
                    self.save()
        client.chat.completions.create = create


def snapshot(cfg):
    source, note = Path(cfg['source']), Path(cfg['note'])
    raw = note.read_bytes()
    text = raw.decode('utf-8')
    if text.count(START) != 1 or text.count(END) != 1:
        raise ValueError('Missing unique managed region')
    before, rest = text.split(START)
    body, after = rest.split(END)
    return dict(source_sha256=digest(source.read_bytes()), note_sha256=digest(raw),
                outside_sha256=digest([before, after]), managed_body_sha256=digest(body))


async def render(cfg, env, folder, meter, preflight):
    import pdf2zh_next.high_level as high
    from pdf2zh_next.config.model import SettingsModel
    from tenacity import stop_after_attempt
    from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator
    for method in (OpenAITranslator.do_translate, OpenAITranslator.do_llm_translate):
        method.retry.stop = stop_after_attempt(cfg['max_attempts_per_request'])
    settings = SettingsModel.model_validate({
        'basic': {'debug': True},
        'translate_engine_settings': {
            'translate_engine_type': 'OpenAI',
            'openai_model': env['TRANSLATION_MODEL'],
            'openai_api_key': env['TRANSLATION_API_KEY'],
            'openai_base_url': env['TRANSLATION_API_BASE_URL'],
            'openai_timeout': '120',
        },
        'translation': {'lang_in': 'en', 'lang_out': 'zh-CN',
                        'output': str(folder / ('preflight-output' if preflight else 'generated')),
                        'qps': cfg['qps'], 'pool_max_workers': cfg['workers'],
                        'no_auto_extract_glossary': True},
        'pdf': {'use_alternating_pages_dual': True, 'no_mono': True,
                'watermark_output_mode': 'no_watermark'},
    })
    factory = high.create_babeldoc_config
    def bounded_factory(settings, file):
        config = factory(settings, file)
        config.only_parse_generate_pdf = preflight
        # In-process mode is selected by Next's debug flag, but the deliverable
        # must not contain BabelDOC debug boxes or decompressed debug siblings.
        config.debug = False
        config.cleanup_temp_files = lambda: None  # Keep recoverable local progress.
        work = folder / ('preflight-work' if preflight else 'work')
        work.mkdir(parents=True, exist_ok=True)
        config.working_dir = work
        config.translator.options['extra_body'] = {'thinking': {'type': cfg['thinking']}}
        config.translator.add_cache_impact_parameters('single-paper-policy', 'deepseek-disabled-v1')
        meter.wrap(config.translator.client)
        return config
    high.create_babeldoc_config = bounded_factory
    finished = None
    async for event in high.do_translate_async_stream(settings, cfg['source']):
        kind = event.get('type')
        if kind in ('progress_start', 'progress_end', 'finish', 'error'):
            atomic_write(folder / 'progress.json', json.dumps(event, default=str, ensure_ascii=False, indent=2))
        if kind == 'error':
            raise RuntimeError('Translation pipeline failed; see progress.json')
        if kind == 'finish':
            finished = event
    if not finished:
        raise RuntimeError('Pipeline did not report completion')
    return finished


def validate_pdf(path, source, preflight=False):
    import pymupdf
    with pymupdf.open(source) as original, pymupdf.open(path) as translated:
        if len(translated) != 2 * len(original):
            raise ValueError('Expected alternating original and translated pages')
        counts = []
        for i, page in enumerate(original):
            if translated[2*i].get_text() != page.get_text():
                raise ValueError('English original page text changed')
            text = translated[2*i+1].get_text()
            count = len(re.findall(r'[\u4e00-\u9fff]', text))
            counts.append(count)
            if not preflight and i < 9 and count < 30:
                raise ValueError('Missing Chinese translation on body page')
        return dict(pages=len(translated), source_pages=len(original), chinese_chars_per_translation_page=counts,
                    original_page_text_preserved=True)


def refine_pdf(raw, source, folder):
    """Deterministic offline repairs checked against this exact source version.

    Keep original vector tables and displayed equations on translated pages;
    table captions and surrounding prose remain Chinese. Reuse original author
    metadata to avoid broken affiliations. Never modify the raw paid output.
    """
    import pymupdf
    original, translated = pymupdf.open(source), pymupdf.open(raw)
    clips = {
        1: [(108,178,505,312)],
        4: [(210,394,506,430)],
        5: [(180,85,432,126), (220,500,506,519)],
        6: [(118,108,495,190), (220,282,392,322)],
        7: [(157,559,506,581)],
        8: [(129,94,483,246)],
        9: [(107,128,510,390)],
    }
    repairs = []
    for n, regions in clips.items():
        page = translated[2*n-1]
        for coords in regions:
            rect = pymupdf.Rect(coords)
            page.add_redact_annot(rect, fill=(1,1,1))
            page.apply_redactions(images=0, graphics=0)
            page.show_pdf_page(rect, original, n-1, clip=rect)
            repairs.append(dict(source_page=n, rect=coords, action='restore_original_vector_region'))
    # An original inline fraction rule was left at its old position after text
    # reflow; it is outside the relocated fraction and must not remain as '='.
    translated[7].draw_rect(pymupdf.Rect(439,546,457,552), color=None, fill=(1,1,1), overlay=True)
    # Correct an inspected semantic attachment: "without scaling" modifies
    # dot-product attention, not additive attention (source p4, section 3.2.1).
    cnfont = Path.home()/'.cache/babeldoc/fonts/SourceHanSerifCN-Regular.ttf'
    rect = pymupdf.Rect(107,506,509,559)
    page = translated[7]
    page.add_redact_annot(rect,fill=(1,1,1))
    page.apply_redactions(images=0,graphics=0)
    corrected = '当 d_k 较小时，两种机制表现相近；当 d_k 较大时，加性注意力优于未作缩放的点积注意力 [3]。我们推测，较大的 d_k 会使点积的幅值增大，从而将 softmax 推入梯度极小的区域（见脚注4）。为抵消该影响，我们将点积除以 d_k 的平方根。'
    if page.insert_textbox(rect,corrected,fontname='reviewcn',fontfile=str(cnfont),fontsize=9.2,lineheight=1.25) < 0:
        raise ValueError('Corrected scaling paragraph did not fit')
    repairs.append(dict(source_page=4,action='correct_without_scaling_modifier_against_original',extra_model_requests=0))
    # Footnote 5: P100 had been split into P1/00 by the paragraph parser.
    page = translated[15]
    rect = pymupdf.Rect(108,708,505,730)
    page.add_redact_annot(rect,fill=(1,1,1))
    page.apply_redactions(images=0,graphics=0)
    font = Path.home()/'.cache/babeldoc/fonts/SourceHanSerifCN-Regular.ttf'
    remaining = page.insert_textbox(rect, '5 对 K80、K40、M40 和 P100，我们分别使用 2.8、3.7、6.0 和 9.5 TFLOPS 的数值。',
                                    fontname='reviewcn',fontfile=str(font),fontsize=7.3)
    if remaining < 0:
        raise ValueError('Corrected footnote did not fit')
    repairs.append(dict(source_page=8, action='repair_P100_footnote', extra_model_requests=0))
    final = pymupdf.open()
    for n in range(len(original)):
        final.insert_pdf(original,from_page=n,to_page=n)
        final.insert_pdf(translated,from_page=2*n+1,to_page=2*n+1)
    path = folder/'reviewed-bilingual.pdf'
    final.save(path,garbage=4,deflate=True)
    final.close(); translated.close(); original.close()
    atomic_write(folder/'offline-repairs.json', json.dumps(repairs,ensure_ascii=False,indent=2))
    return path


def publish(cfg, folder, marker):
    src, target, note = Path(marker['cached_pdf']), Path(cfg['output']), Path(cfg['note'])
    if digest(src.read_bytes()) != marker['output_sha256']:
        raise ValueError('Cached PDF hash mismatch')
    atomic_write(target, src.read_bytes(), folder / 'backups')
    old = note.read_bytes()
    text = old.decode('utf-8')
    before, rest = text.split(START)
    body, after = rest.split(END)
    attachment_link = link(note, target, '中英双语 PDF')
    if attachment_link not in body:
        original_link = link(note, Path(cfg['source']), '原文 PDF')
        if original_link not in body:
            raise ValueError('Original attachment link not found; do not rewrite note')
        body = body.replace(original_link, original_link + '\n\n' + attachment_link, 1)
    # Only append a dedicated owned block; keep all prior summary/corrections byte-for-byte.
    bs, be = '<!-- TRANSLATION:START -->', '<!-- TRANSLATION:END -->'
    check = marker.get('visual_review', {})
    if check and check.get('pdf_sha256') != marker['output_sha256']:
        raise ValueError('Visual review is stale for this PDF')
    status = check.get('status', '已生成；未完成视觉抽查')
    block = bs + '\n\n双语翻译状态：' + status + '。原文 11 页，英中交替共 22 页；保留原始英文页。\n\n作者信息、表格表体、图内标注及陈列公式保留原文，以免改写身份、数值与符号；正文与图表说明已译。\n\n' + be
    if bs in body:
        body = re.sub(re.escape(bs) + r'.*?' + re.escape(be), lambda _: block, body, flags=re.S)
    else:
        body = body.rstrip() + '\n\n' + block + '\n'
    # This one YAML processing field is program-owned. Preserve the rest of
    # the YAML verbatim, including any custom fields, and all handwritten text.
    field = r'(^processing:\n(?:  [^\n]*\n)*?  translation: )[^\n]*'
    updated_before, changed = re.subn(field, lambda m: m[1]+'success', before, count=1, flags=re.M)
    if changed != 1:
        raise ValueError('Expected one owned translation processing field')
    updated = updated_before + START + body + END + after
    normalized = lambda s: re.sub(field, lambda m: m[1]+'<owned>', s, count=1, flags=re.M)
    if digest([normalized(updated.split(START)[0]), updated.split(END)[1]]) != digest([normalized(before), after]):
        raise ValueError('Manual region changed')
    atomic_write(note, updated, folder / 'backups')
    # Record only this paper's translation stage so a later summary-cache
    # publication also retains its attachment link. Relative path works in
    # both the existing WSL pipeline and this isolated Windows runtime.
    cached_relative = Path(marker['cached_pdf']).relative_to(ROOT).as_posix()
    for config_name in ('config.toml', 'config.test.toml'):
        c = load_config(config_name)
        if not (c['paths']['data']/'papers.sqlite3').exists():
            continue
        with run_lock(c['paths']['data']):
            store = Store(c['paths']['data'])
            try:
                if not store.get(cfg['paper_id']):
                    raise ValueError('Expected existing paper missing from database')
                version = store.stage(cfg['paper_id'],'pdf')['version']
                store.record(cfg['paper_id'],'translation',version,'success',
                             dict(path=cached_relative, output_sha256=marker['output_sha256'],
                                  visual_review=check, usage=Meter(cfg,{},folder).usage()))
            finally:
                store.db.close()
    return dict(output=str(target), note=str(note), output_sha256=digest(target.read_bytes()),
                manual_regions_unchanged=True, previous_note_backed_up=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--preflight', action='store_true')
    args = ap.parse_args()
    cfg = tomllib.loads((ROOT / 'translation.single.toml').read_text('utf-8'))
    existing=existing_reading_delivery(cfg)
    if existing:
        print(json.dumps(existing,ensure_ascii=False,indent=2))
        return
    if cfg['paper_id'] != '80c6ca8c01157eb19816' or cfg['workers'] != 1 or cfg['qps'] != 1:
        raise ValueError('Only this one paper with one worker is allowed')
    if cfg['thinking'] != 'disabled' or not 1 <= cfg['max_attempts_per_request'] <= 2:
        raise ValueError('This accepted provider policy requires disabled thinking and at most two attempts')
    env = secrets()
    if not all(env.get(k) for k in ('TRANSLATION_API_BASE_URL','TRANSLATION_API_KEY','TRANSLATION_MODEL')):
        raise ValueError('Independent translation configuration is incomplete')
    if urlparse(env['TRANSLATION_API_BASE_URL']).hostname != 'api.deepseek.com' or env['TRANSLATION_MODEL'] != 'deepseek-flash':
        raise ValueError('Thinking policy must be reviewed for this provider/model')
    folder = ROOT / cfg['data']
    folder.mkdir(parents=True, exist_ok=True)
    with run_lock(folder):
        meter = Meter(cfg, env, folder, args.preflight)
        initial = snapshot(cfg)
        if initial['source_sha256'] != 'd87d482d5ae7960e2e43d7dd6d21377e60e73e8fce1bf2a01aff7aca8a08c537':
            raise ValueError('Unexpected source version')
        input_path = folder / ('preflight-input.json' if args.preflight else 'input.json')
        first_input = folder / 'first-input.json'
        if not args.preflight and not first_input.exists():
            atomic_write(first_input, input_path.read_bytes() if input_path.exists() else json.dumps(initial, indent=2))
        atomic_write(input_path, json.dumps(initial, indent=2))
        marker_path = folder / 'generated.json'
        report = dict(started=now(), paper_id=cfg['paper_id'], preflight=args.preflight, limits=cfg,
                      provider='api.deepseek.com', model=env['TRANSLATION_MODEL'], thinking='disabled',
                      versions={x: importlib.metadata.version(x) for x in ('pdf2zh-next','babeldoc','bitstring')})
        try:
            if not args.preflight and marker_path.exists():
                marker = json.loads(marker_path.read_text('utf-8'))
                if marker['source_sha256'] != initial['source_sha256'] or marker['model'] != env['TRANSLATION_MODEL']:
                    raise ValueError('Cache version mismatch')
                report.update(cache_hit=True, **publish(cfg, folder, marker))
            else:
                if not args.preflight:
                    preflight_path = folder / 'preflight-report.json'
                    if not preflight_path.exists() or json.loads(preflight_path.read_text('utf-8')).get('status') != 'preflight_passed':
                        raise ValueError('Run --preflight successfully first')
                result = asyncio.run(asyncio.wait_for(render(cfg, env, folder, meter, args.preflight), cfg['timeout_seconds']))
                outfolder = folder / ('preflight-output' if args.preflight else 'generated')
                result_path = getattr(result.get('translate_result'), 'dual_pdf_path', None)
                candidates = [Path(result_path)] if result_path else list(outfolder.glob('*zh-CN.dual.pdf'))
                if len(candidates) != 1:
                    raise ValueError('Expected one bilingual PDF')
                if any(r['status'] != 'success' and not any(s['key'] == r['key'] and s['status'] == 'success' for s in meter.rows) for r in meter.rows):
                    raise ValueError('Unresolved failed requests; output is not accepted')
                candidate = candidates[0] if args.preflight else refine_pdf(candidates[0], cfg['source'], folder)
                checked = validate_pdf(candidate, cfg['source'], args.preflight)
                report.update(cache_hit=False, structural_check=checked)
                if not args.preflight:
                    marker = dict(cached_pdf=str(candidate), source_sha256=initial['source_sha256'],
                                  output_sha256=digest(candidate.read_bytes()), model=env['TRANSLATION_MODEL'],
                                  thinking='disabled', generated_at=now(), structural_check=checked)
                    atomic_write(marker_path, json.dumps(marker, ensure_ascii=False, indent=2))
                    report.update(publish(cfg, folder, marker))
            if snapshot(cfg)['source_sha256'] != initial['source_sha256']:
                raise ValueError('Source file changed')
            report['status'] = 'preflight_passed' if args.preflight else ('spot_check_passed' if marker.get('visual_review') else 'generated')
            if not args.preflight:
                report['visual_review'] = marker.get('visual_review')
        except Exception as exc:
            report.update(status='failed', error=type(exc).__name__, message=str(exc) if type(exc) in (ValueError, LimitError) else 'See retained local progress and receipts')
        report.update(ended=now(), usage=meter.usage())
        atomic_write(folder / ('preflight-report.json' if args.preflight else 'report.json'), json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report['status'] != 'failed' else 2


if __name__ == '__main__':
    raise SystemExit(main())
