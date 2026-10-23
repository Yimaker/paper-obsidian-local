"""Non-interactive Windows translation worker, called by the WSL daily entry.

Reuse pinned PDFMathTranslate/BabelDOC and its metered request/cache boundary.
No source-specific repairs from the historical Attention paper are applied.
"""
from __future__ import annotations
import asyncio
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.translate_one import Meter, render
from paper_obsidian.core import atomic_write, digest, now, run_lock, safe_error, secrets


class DailyMeter(Meter):
    def __init__(self, cfg, env, folder):
        super().__init__(cfg, env, folder)

    def wrap(self, client):
        super().wrap(client)
        wrapped = client.chat.completions.create
        def create(**kwargs):
            if (self.folder.parents[1] / 'STOP').exists():
                raise RuntimeError('Cooperative stop requested; caches saved')
            # Authentication/balance/permissions: circuit break all later calls.
            if any(r.get('http_status') in (401, 402, 403) for r in self.rows if r['started'][:10] == now()[:10]):
                raise RuntimeError('Translation service authentication/balance error; stopped for today')
            return wrapped(**kwargs)
        client.chat.completions.create = create


async def run_cooperatively(cfg,env,folder,meter):
    task=asyncio.create_task(render(cfg,env,folder,meter,False))
    try:
        while not task.done():
            await asyncio.sleep(1)
            if (folder.parents[1]/'STOP').exists() and not meter.lock.locked():
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
                raise RuntimeError('Cooperative stop completed at request boundary; cache saved')
        return await task
    finally:
        if not task.done():task.cancel()


def restore_original_region(dst,src,original,number,rect):
    import pymupdf as fitz
    rect=fitz.Rect(rect)&src.rect
    if rect.is_empty:return
    dst.add_redact_annot(rect,fill=(1,1,1))
    dst.apply_redactions(images=0,graphics=0)
    dst.draw_rect(rect,color=None,fill=(1,1,1),overlay=True)
    dst.show_pdf_page(rect,original,number,clip=rect)


def protect_numeric_tables_and_references(src,dst,original,n,seen_references):
    """Offline preservation, driven by source geometry and numeric checks."""
    import pymupdf as fitz
    repairs=[]
    lines=[line for block in src.get_text('dict')['blocks'] for line in block.get('lines',[])]
    value=lambda line:''.join(span['text'] for span in line['spans']).strip()
    heading=next((fitz.Rect(line['bbox']) for line in lines if value(line)=='References'),None)
    stop=next((fitz.Rect(line['bbox']) for line in lines if re.match(r'^(?:NeurIPS Paper Checklist|Appendix|Supplementary Material)\b',value(line))),None)
    two_columns=sum(fitz.Rect(line['bbox']).x0>src.rect.width*.5 and fitz.Rect(line['bbox']).width>100 for line in lines)>=8
    mid=src.rect.width/2
    regions=[]
    if heading:
        if not two_columns:regions=[fitz.Rect(0,heading.y0-2,src.rect.width,src.rect.height)]
        elif heading.x0<mid:regions=[fitz.Rect(0,heading.y0-2,mid-4,src.rect.height),fitz.Rect(mid+4,0,src.rect.width,src.rect.height)]
        else:regions=[fitz.Rect(mid+4,heading.y0-2,src.rect.width,src.rect.height)]
        seen_references=True
    elif seen_references:
        if not stop:regions=[src.rect]
        elif not two_columns:regions=[fitz.Rect(0,0,src.rect.width,stop.y0-2)]
        elif stop.x0<mid:regions=[fitz.Rect(0,0,mid-4,stop.y0-2)]
        else:regions=[fitz.Rect(0,0,mid-4,src.rect.height),fitz.Rect(mid+4,0,src.rect.width,stop.y0-2)]
    if stop:seen_references=False
    for rect in regions:
        restore_original_region(dst,src,original,n,rect)
        repairs.append({'kind':'preserve_original_reference_metadata','rect':list(rect)})
    pure_reference=any(abs(r.height-src.rect.height)<2 and abs(r.width-src.rect.width)<2 for r in regions)
    if not pure_reference:
        # Source table ruling lines remain at their original geometry. Restore a
        # table only when a decimal token is missing/malformed in that region.
        for table in src.find_tables().tables:
            box=fitz.Rect(table.bbox)
            if table.header and table.header.bbox:box=box|fitz.Rect(table.header.bbox)
            rect=fitz.Rect(box.x0-3,box.y0-2,box.x1+3,box.y1+8)&src.rect
            numbers=lambda page:set(re.findall(r'(?<!\d)\d+\.\d+(?!\d)',page.get_text(clip=rect)))
            expected=numbers(src);missing=expected-numbers(dst)
            if len(expected)>=5 and missing:
                restore_original_region(dst,src,original,n,rect)
                if not expected.issubset(numbers(dst)):raise ValueError('Numeric table preservation failed')
                repairs.append({'kind':'restore_original_numeric_table','rect':list(rect),'missing_before':sorted(missing)})
    return seen_references,pure_reference,repairs


def check_and_assemble(raw, source, folder):
    import pymupdf as fitz
    checked = []
    with fitz.open(source) as original, fitz.open(raw) as translated:
        if len(translated) != 2 * len(original): raise ValueError('Alternating page count invalid')
        final = fitz.open()
        seen_references=False
        for n, src in enumerate(original):
            dst = translated[2*n+1]
            seen_references,pure_reference,repairs=protect_numeric_tables_and_references(src,dst,original,n,seen_references)
            chinese = len(re.findall(r'[\u4e00-\u9fff]', dst.get_text()))
            # Sparse reference/diagram pages may intentionally remain in English.
            source_text=src.get_text()
            prose = len(re.findall(r'[A-Za-z]{3,}', source_text))
            heading_references=bool(re.search(r'(?im)^\s*References\s*$',source_text))
            section_break=bool(re.search(r'(?im)^\s*(?:Appendix|Supplementary Material|NeurIPS Paper Checklist)\b',source_text))
            reference_page=pure_reference or ((bool(re.match(r'\s*\[\d+\]',source_text)) or seen_references) and len(re.findall(r'\[\d+\]',source_text))>=8 and not section_break)
            if heading_references:seen_references=True
            if section_break:seen_references=False
            reference_restored=reference_page and chinese<40
            if reference_restored:
                # Citation metadata/URLs should not be rewritten by translation.
                # Keep this largely English reference page as original vectors.
                dst=src;chinese=0
            if prose > 180 and chinese < 25 and not reference_page: raise ValueError(f'Translated page {n+1} appears blank/untranslated')
            if abs(dst.rect.width-src.rect.width)>2 or abs(dst.rect.height-src.rect.height)>2:
                raise ValueError('Page dimensions changed')
            if chinese and not any('SourceHan' in str(f) or 'Noto' in str(f) or 'CJK' in str(f) for f in dst.get_fonts(full=True)):
                raise ValueError('Chinese font missing')
            pix = dst.get_pixmap(matrix=fitz.Matrix(.3,.3), alpha=False)
            if min(pix.samples) > 245: raise ValueError('Blank translated render')
            offpage = []
            for b in dst.get_text('dict')['blocks']:
                for line in b.get('lines', []):
                    for span in line['spans']:
                        box=fitz.Rect(span['bbox'])
                        if box.x0 < -3 or box.y0 < -3 or box.x1 > dst.rect.width+3 or box.y1 > dst.rect.height+3:
                            offpage.append(span['text'][:40])
            if len(offpage)>3: raise ValueError('Severe text outside page boundaries')
            checked.append({'physical_page':n+1,'chinese_chars':chinese,'original_words':prose,'outside_spans':offpage,
                            'reference_metadata_page':reference_page,
                            'reference_original_restored':reference_restored,
                            'offline_preservation_repairs':repairs,
                            'source_images':len(src.get_images()),'translated_images':len(dst.get_images())})
            # Always copy actual English PDF pages, not regenerated text.
            final.insert_pdf(original, from_page=n, to_page=n)
            if reference_restored:final.insert_pdf(original,from_page=n,to_page=n)
            else:final.insert_pdf(translated, from_page=2*n+1, to_page=2*n+1)
        path = folder / 'bilingual.pdf'
        tmp = folder / 'bilingual.tmp.pdf'
        final.save(tmp, garbage=4, deflate=True)
        final.close()
        render_differences=[]
        # Use a freshly opened source for QA, independent of the table detector's
        # in-memory render/cache state during geometry analysis.
        with fitz.open(source) as pristine,fitz.open(tmp) as out:
            for n, src in enumerate(pristine):
                dest=out[2*n]
                before,after=src.get_pixmap().samples,dest.get_pixmap().samples
                if before != after:
                    # Copying a PDF may produce tiny rasterization differences
                    # in transparent thumbnail edges. Require unchanged drawing
                    # instructions, original text, image/mask streams and fonts;
                    # never accept a rewritten English page via visual tolerance.
                    content=lambda doc,page:b''.join(doc.xref_stream(x) for x in page.get_contents())
                    images=lambda doc,page:sorted((i[7],digest(doc.xref_stream(i[0])),digest(doc.xref_stream(i[1])) if i[1] else None) for i in page.get_images())
                    fonts=lambda doc,page:sorted(digest(doc.extract_font(f[0])[-1]) for f in page.get_fonts())
                    changed=sum(a!=b for a,b in zip(before,after))
                    ratio=changed/max(1,len(before))
                    if (len(before)!=len(after) or ratio>.005 or content(pristine,src)!=content(out,dest) or
                        src.get_text()!=dest.get_text() or images(pristine,src)!=images(out,dest) or fonts(pristine,src)!=fonts(out,dest)):
                        raise ValueError('English original content/render preservation failed')
                    render_differences.append({'physical_page':n+1,'changed_color_channels':changed,'fraction':ratio,
                        'original_content_image_mask_font_streams_unchanged':True})
        tmp.replace(path)
    # Contact sheet and selected full-resolution pages are maintenance assets.
    from PIL import Image, ImageDraw
    with fitz.open(path) as doc:
        thumbs=[]
        for n in range(1,len(doc),2):
            pix=doc[n].get_pixmap(matrix=fitz.Matrix(.45,.45),alpha=False)
            img=Image.frombytes('RGB',[pix.width,pix.height],pix.samples)
            tile=Image.new('RGB',(300,420),'#dddddd'); img.thumbnail((290,390)); tile.paste(img,((300-img.width)//2,20))
            ImageDraw.Draw(tile).text((8,4),f'CN physical {n//2+1}',fill='black'); thumbs.append(tile)
        sheet=Image.new('RGB',(300*4,420*((len(thumbs)+3)//4)),'white')
        for i,img in enumerate(thumbs): sheet.paste(img,((i%4)*300,(i//4)*420))
        sheet.save(folder/'contact-sheet.jpg')
        for n in sorted({1,min(5,len(doc)-1),min(11,len(doc)-1),min(15,len(doc)-1)}):
            doc[n].get_pixmap(matrix=fitz.Matrix(1.3,1.3)).save(folder/f'spot-page-{n+1}.png')
    return path, {'pages':len(checked)*2,'source_pages':len(checked),'alternating':True,'original_pixels_identical':not render_differences,
                  'original_content_preserved':True,'minor_render_differences':render_differences,
                  'per_page':checked,'visual_review':'自动逐页渲染与边界/字体检查；未逐句人工审校',
                  'translation_scope':'公式、表格和图像尽量保留；图内标注、部分表体及参考文献题录可能保留英文，复杂内容以原页核对'}


def main():
    job = json.loads(Path(sys.argv[1]).read_text('utf-8'))
    folder = ROOT / job['folder']
    folder.mkdir(parents=True,exist_ok=True)
    env=secrets()
    cfg=dict(source=str(ROOT/job['source']), workers=1,qps=1,thinking='disabled',max_attempts_per_request=2,
             max_requests=None,max_total_tokens=None,max_output_tokens=8192,timeout_seconds=7200,
             run_id=job.get('run_id'))
    binding=digest([digest(Path(cfg['source']).read_bytes()),env['TRANSLATION_API_BASE_URL'],env['TRANSLATION_MODEL'],'daily-alternating-v1'])
    marker=folder/'complete.json'
    result={'started':now(),'binding':binding}
    with run_lock(folder):
        meter=DailyMeter(cfg,env,folder)
        try:
            if marker.exists():
                old=json.loads(marker.read_text('utf-8'))
                if old['binding']==binding and old.get('qa_version')==3 and (folder/'bilingual.pdf').exists() and digest((folder/'bilingual.pdf').read_bytes())==old['output_sha256']:
                    old.update(cache_hit=True, usage=meter.usage())
                    atomic_write(folder/'result.json',json.dumps(old,ensure_ascii=False,indent=2));return 0
            if not all(env.get(k) for k in ('TRANSLATION_API_BASE_URL','TRANSLATION_API_KEY','TRANSLATION_MODEL')):
                raise ValueError('Translation configuration missing')
            previous=json.loads((folder/'result.json').read_text('utf-8')) if (folder/'result.json').exists() else {}
            raw_candidates=list((folder/'generated').glob('*zh-CN.dual.pdf'))
            if previous.get('binding')==binding and len(raw_candidates)==1 and meter.rows and all(r['status']=='success' for r in meter.rows):
                # A downstream QA/publication repair never triggers translation again.
                path=raw_candidates[0]
                result['raw_output_reused']=True
            else:
                event=asyncio.run(asyncio.wait_for(run_cooperatively(cfg,env,folder,meter),cfg['timeout_seconds']))
                path=getattr(event.get('translate_result'),'dual_pdf_path',None)
            if not path:
                found=list((folder/'generated').glob('*zh-CN.dual.pdf'))
                if len(found)!=1: raise ValueError('Expected one translated result')
                path=found[0]
            unresolved=[r for r in meter.rows if r['status']!='success' and not any(s['key']==r['key'] and s['status']=='success' for s in meter.rows)]
            if unresolved: raise ValueError('Unresolved translation requests')
            pdf,check=check_and_assemble(path,cfg['source'],folder)
            result.update(status='success',path=pdf.relative_to(ROOT).as_posix(),output_sha256=digest(pdf.read_bytes()),check=check,cache_hit=False,qa_version=3)
            atomic_write(marker,json.dumps(result,ensure_ascii=False,indent=2))
        except Exception as exc:
            result.update(status='failed',error=safe_error(exc),message=str(exc) if type(exc) is ValueError else '翻译失败，保留请求缓存与进度，详见本目录progress.json')
        result.update(ended=now(),usage=meter.usage())
        atomic_write(folder/'result.json',json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['status']=='success' else 2

if __name__=='__main__': raise SystemExit(main())
