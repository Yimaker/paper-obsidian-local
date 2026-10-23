"""Durable, cumulative daily reading briefs; offline audits never advance latest."""
from __future__ import annotations

import json
from datetime import datetime

from .core import now, today
from .library import brief_path, day_of, portable, meta
from .vault import managed_write, link, text
from .api_accounting import render as render_accounting


def delivery_events(s):
    """First completed delivery per stable ID, sourced from publication receipts.

    The event survives retries, newer versions and failed runs. No file mtimes.
    """
    events=s.get_state('reading:delivery-events',{})
    receipts={}
    for row in s.db.execute('SELECT id,report FROM runs WHERE report IS NOT NULL ORDER BY id'):
        r=json.loads(row['report'])
        if r.get('entry')!='daily':continue
        for d in r.get('delivered',[]):
            if d.get('id') and day_of(d.get('completed_at')):
                receipts.setdefault(d['id'],[]).append((d['completed_at'],f'runs:{row["id"]}:delivered'))
    for p in s.papers():
        pid=p['id'];row=s.stage(pid,'delivery')
        if not row or row['status']!='success':continue
        d=json.loads(row['detail'])
        dates=receipts.get(pid,[])+[(x.get('completed_at'),'delivery.version_history') for x in d.get('version_history',[])]
        dates += [(d.get('first_completed_at') or d.get('completed_at'),'delivery.stage')]
        known=[(a,b) for a,b in dates if day_of(a)]
        if not known:continue
        first,evidence=min(known,key=lambda item:datetime.fromisoformat(item[0]))
        if pid not in events:
            kind=d.get('delivery_kind') or ('新论文' if d.get('review_status','').startswith('自动生成') else '历史成果迁移／补齐')
            created=s.db.execute('SELECT created FROM papers WHERE id=?',(pid,)).fetchone()
            events[pid]={'first_completed_at':first,'day':day_of(first),'kind':kind,'evidence':evidence,
                         'first_discovered_at':created[0] if created else None}
    s.set_state('reading:delivery-events',events)
    return events


def refresh_latest(c,s):
    # Only actual scheduled/manual daily runs register these dates. Migration
    # registers a date only when a formal delivery receipt proves it existed.
    days=s.get_state('reading:actual-brief-days',[])
    days=[d for d in days if brief_path(c,d).exists()]
    if not days:return
    day=max(days);path=brief_path(c,day)
    latest=c['paths']['vault']/'00_每日简报'/'最新简报.md'
    managed_write(latest,{'kind':'latest-brief','date':day,'timezone':'Asia/Shanghai'},
        '# 最新简报\n\n'+link(latest,path,day+' 每日简报')+'\n\n完整论文位于 `10_论文`。',c)
    s.set_state('reading:latest-day',day)


def daily_brief(c,s,report,*,day=None,actual=True):
    events=delivery_events(s)
    if day is None:
        day=day_of(report.get('ended')) or day_of(report.get('started')) or today().replace('-','.')
        if actual:
            # A long run may deliver papers on both sides of Beijing midnight.
            # Write each real delivery date, and the end-of-run date, once.
            touched={day_of(d.get('completed_at')) for d in report.get('delivered',[])}-{None,day}
            for other in sorted(touched):daily_brief(c,s,report,day=other,actual=True)
    path=brief_path(c,day)
    if not actual and not path.exists():
        return str(path)  # an audit cannot create a fictitious daily run
    key='reading:brief:'+day
    state=s.get_state(key,{'cached':[],'pending':{},'sources':{}})
    state['cached']=sorted(set(state['cached'])|set(report.get('cached',[])))
    for entry in report.get('pending',[])+report.get('failures',[]):
        state['pending'][entry.get('id') or entry.get('title','运行')]=entry
    state['sources'].update(report.get('sources',{}))
    if not state['sources']:
        state['sources'].update(s.get_state('daily-source-status:'+day.replace('.','-'),{}))
    state['updated']=now()
    if actual:
        state['status']='已停止，成功成果保留' if report.get('stopped') else ('部分待处理' if report.get('failures') or report.get('pending') else '运行完成')
        state['fetch_performed']=report.get('fetch_performed',False)
    else:state.setdefault('status','已有交付；离线路径整理完成')
    state['usage']=report.get('usage',state.get('usage',{}))
    if actual:
        state['api_accounting']=report.get('api_accounting')
        # Old/pre-feature receipts are not assigned invented snapshots or costs.
        if state['api_accounting'] is None:
            state['api_accounting']={'run_id':report.get('run_id'), 'started':report.get('started'),
                'ended':report.get('ended'), 'sources':[], 'estimate':{'status':'incomplete',
                'reasons':['本次运行未记录余额与费用回执']}}
    s.set_state(key,state)
    delivered=[]
    for p in s.papers():
        row=s.stage(p['id'],'delivery')
        if row and row['status']=='success' and events.get(p['id'],{}).get('day')==day:
            delivered.append((p,json.loads(row['detail'])))
    new=[x for x in delivered if events[x[0]['id']]['kind']=='新论文']
    historical=[x for x in delivered if x not in new]
    body=[f'# {day} AI／机器学习论文简报','']+render_accounting(state.get('api_accounting'),historical=not actual)+[
          f"状态：{state['status']}。更新时间：{state['updated']}（北京时间）。",'',
          f'今日累计完整交付 {len(delivered)} 篇：新交付 {len(new)} 篇，历史成果迁移／补齐 {len(historical)} 篇。',
          '本次无新增；已完成内容与手写区域继续保留。' if not report.get('delivered') else '本次完成的论文已按稳定标识累计去重。','']
    from .eligibility import delivery_qualification_counts
    qcounts=delivery_qualification_counts(delivered)
    body += [f"资格交付统计（稳定ID去重）：当前期刊资格 {qcounts['current']}；发表时资格已核验 {qcounts['historical']}；两者交集 {qcounts['intersection']}；JCR 阅读资格 {qcounts['jcr']}（新字段未记录 {qcounts['jcr_unrecorded']}）；CCF A {qcounts['ccf_a']}；旧回执未记录 {qcounts['unknown']}。子项不可直接相加，总交付 {qcounts['total']}。",'']
    jcr_counts={'通过':0,'待审':0,'不符合':0}
    for paper in s.papers():
        jr=s.stage(paper['id'],'jcr_auto')
        if not jr:continue
        final=s.stage(paper['id'],'jcr_final_review')
        status=('通过' if final and final['version']==jr['version'] and final['status']=='reviewed' else '不符合' if jr['status']=='unrelated' else '待审')
        jcr_counts[status]+=1
    body += ['自动学科审核（当前论文级记录）：通过 '+str(jcr_counts['通过'])+'；待审 '+str(jcr_counts['待审'])+'；不符合 '+str(jcr_counts['不符合'])+'。通过指AI原文审校完成，用户未复核；不等于已完整交付。','']
    def items(rows):
        result=[]
        for p,d in rows:
            note=portable(d['note']);pdf=portable(d['bilingual']);m=meta(note)
            title=m.get('title_zh') or p['title']
            reason=(d.get('summary') or '可结合原文与笔记阅读。').split('。')[0].rstrip('；')+'。'
            url=p.get('publication',{}).get('source') or p.get('url','')
            source=f"[{text(p.get('venue') or '来源记录')}]({url})" if url else text(p.get('venue') or '来源未记录')
            result += [f'- **{text(title)}** · {source}',
                       '  '+text(reason),
                       '  '+link(path,note,'阅读笔记')+' · '+link(path,pdf,'原始中文 PDF' if d.get('translation_skipped') else '中英对照 PDF'),'']
        return result or ['无。','']
    from .cnki import daily_stats
    cnki = daily_stats(s,day)
    from .cnki_browser import stats as browser_stats
    try:
        browser = browser_stats(s,day)
    except Exception:
        browser = {'browser':'状态读取失败；其他来源继续','official_api':'未接通',
                   'download_success':0,'manual_pending':0,'sessions':[],'export_files':0,'repeated_files':0,
                   'acceptance_cost_upper_cny':'未知'}
    body += ['## 知网来源','',f"题录导入可用；浏览器检索：{browser['browser']}；官方接口：{browser['official_api']}。今日导入文件 {cnki['files']}，新增 {cnki['new']}，补全 {cnki['updated']}，重复记录 {cnki['duplicate']}，失败 {cnki['failed']}；知网关联实体待全文 {cnki['pending_fulltext']}；今日下载成功 {browser['download_success']}，待人工处理 {browser['manual_pending']}。题录不计完整交付。",'']
    for session in browser['sessions']:
        body += [f"- 浏览器会话 {session['id']}：{session['status']}／{text(session.get('reason',''))}"]
    body += ['']
    body += [f"浏览器今日导出文件 {browser['export_files']}，重复文件 {browser['repeated_files']}；本次验收累计模型费用上界 {browser['acceptance_cost_upper_cny']} CNY／总上限 1 CNY。每日付费浏览器未授权，保持关闭。",'']
    if report.get('cnki'):
        body += ['本次导入：'+text(json.dumps({k:v for k,v in report['cnki'].items() if k!='files'},ensure_ascii=False)),'']
    from .branches import primary
    cross=[x for x in new if primary(s,x[0]['id'])=='cross']
    body+=['## 今日新交付','']+items([x for x in new if x not in cross])
    body+=['## 今日交叉领域论文','']+items(cross)
    body+=['未有合格完整交付时不凑数；具体资格、材料、配额或来源原因见待处理和来源回执。','']
    body+=['容量概况：'+json.dumps(report.get('capacity',{}).get('after_counts',{}),ensure_ascii=False),'']
    from .branches import matches
    linked=[]
    for p in s.papers():
        row=s.stage(p['id'],'delivery')
        if row and row['status']=='success' and primary(s,p['id'])=='ordinary' and matches(p):
            d=json.loads(row['detail']);linked.append('- '+link(path,portable(d['note']),p['title']))
    body+=['<details>','<summary>普通区相关论文链接（候选主题匹配，不重复计交叉收录）</summary>','']+linked+['','</details>','']
    body+=['## 历史成果迁移／补齐','']+items(historical)
    if historical:body+=['历史成果不计为近期新论文；首次完整日常交付时间与论文发表年份分别记录。','']
    body+=['## 已缓存复用','',f"今日运行累计复用 {len(state['cached'])} 篇；复用不重复计为新交付。",'']
    body+=['## 待处理／失败','']
    pending=[x for pid,x in state['pending'].items() if pid not in events]
    if not pending:body+=['无新增待处理记录。','']
    urgent=[x for x in pending if any(t in (x.get('status','')+x.get('message','')) for t in ('冲突','用量未知','未确认','失败','待补全文'))]
    body += [f'仍有 {len(pending)} 篇待核验或排队；以下突出需处理的问题，完整列表可展开。',''] if pending else []
    for x in urgent:
        body+=['- '+text(x.get('title','运行'))+'：'+text(x.get('status') or x.get('message') or x.get('error','待处理'))]
    if pending:
        body+=['','<details>','<summary>完整待处理列表</summary>','']
        for x in pending:
            body+=['- '+text(x.get('title','运行'))+'：'+text(x.get('status') or x.get('message') or x.get('error','待处理'))]
        body+=['','</details>']
    body+=['','<details>','<summary>来源状态与用量</summary>','',
           '期刊按当前与发表时双口径逐篇核验，缺证留待核验；官方会议接入以已有 NeurIPS／CVPR 为主，来源可能失败或截断。',
           '以下状态来自已保存回执；离线整理没有重测来源。','']
    for name,status in state['sources'].items():
        body+=['- '+name+'：'+text(json.dumps(status,ensure_ascii=False))]
    body+=['','用量：','```json',json.dumps(state.get('usage',{}),ensure_ascii=False,indent=2),'```',
           '以上为当天请求用量；本次费用口径见正文开头。不确定请求未计作零费用。','</details>','',
           '自动生成并经 AI 原文审校的笔记仍需用户复核；历史 Codex 校对标签沿用真实记录。']
    managed_write(path,{'date':day,'kind':'daily-brief','timezone':'Asia/Shanghai'},'\n'.join(body),c)
    if actual:
        days=s.get_state('reading:actual-brief-days',[])
        s.set_state('reading:actual-brief-days',sorted(set(days+[day])))
        refresh_latest(c,s)
    return str(path)


def offline_replay(c,*,maintenance=False):
    """Same completed/naming/brief code as production, with no service objects.

    Does not run eligibility upgrades, downloads, generation or translations;
    does not insert a production run or spend quotas.
    """
    from .core import Store, run_lock, atomic_write
    from .daily import completed
    from .library import reading_path, note_for_id
    from .vault import check_links
    with run_lock(c['paths']['data'],maintenance=maintenance):
        s=Store(c['paths']['data'])
        try:
            cached=[];checked=0
            for p in s.papers():
                if completed(p,c,s):
                    current=note_for_id(c,s,p['id'],True)
                    assert current==reading_path(p,c,s,True), '正式路径不符合集中规则'
                    cached.append(p['id']);checked+=1
                elif (current:=note_for_id(c,s,p['id'])):
                    assert current==reading_path(p,c,s), '候选路径不符合集中规则'
                    checked+=1
            report={'started':now(),'entry':'offline-layout-replay','sources':{},'cached':cached,
                    'delivered':[],'pending':[],'failures':[],'network_requests':0,
                    'model_requests':0,'translation_requests':0,'paths_checked':checked}
            day=day_of(now())
            # Update only an already proven daily page, keeping its date and latest.
            daily_brief(c,s,report,day=day,actual=False)
            report['links']=check_links(c['paths']['vault'])
            atomic_write(c['paths']['data']/'layout-replay.json',json.dumps(report,ensure_ascii=False,indent=2))
            return report
        finally:s.db.close()
