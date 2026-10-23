"""Audited local JIF reading channel. No network or model calls."""
from datetime import date
from .core import digest, local_path, now


def artifact(r, prefix='evidence'):
    f = local_path(r.get(prefix+'_file', ''))
    if not f.is_file() or digest(f.read_bytes()) != r.get(prefix+'_sha256'):
        raise ValueError('原件缺失或哈希不符')
    return f


def validate(r):
    from .eligibility import official, stamp
    from .journals import issns
    if not r.get('name') or not issns(r.get('issns')):
        raise ValueError('缺期刊身份')
    if r.get('identity_review') != 'exact' or r.get('conflict'):
        raise ValueError('身份或来源冲突')
    if not official(r.get('source'), ['clarivate.com']) or not stamp(r.get('checked_at')):
        raise ValueError('缺官方来源或查询时间')
    artifact(r)
    if r.get('reviewer_type') not in ('codex','user','expert') or not r.get('semantic_support') or type(r.get('user_reviewed')) is not bool or (r['reviewer_type']=='codex' and r['user_reviewed']):
        raise ValueError('缺逐字段审校或审校身份不实')
    for key in ('jcr_year','metric_year'):
        if type(r.get(key)) is not int or not 1900 <= r[key] <= int(now()[:4]):
            raise ValueError('年份缺失或无效')
    if r.get('sci_index') not in ('SCIE','not_SCIE') or r.get('quartile_type') != 'JIF':
        raise ValueError('须有明确SCIE及JIF分区；JCI不能代替')
    if not r.get('latest_available_basis') or not r.get('review_due'):
        raise ValueError('缺版本复核依据')
    date.fromisoformat(r['review_due'])
    cats=r.get('categories',[])
    if r.get('all_categories_verified') is not True or not cats or len({x['category'] for x in cats}) != len(cats):
        raise ValueError('全部学科尚未核齐')
    if any(not x.get('category') or x.get('quartile') not in ('Q1','Q2','Q3','Q4') or not x.get('rank') for x in cats):
        raise ValueError('学科JIF分区或排名不完整')


def classify(p,w):
    q={'status':'pending','reason':'缺官方JCR证据','evidence':[], 'content_relations':[],
       'adopted_categories':[], 'fulltext_verification_allowed':False,'checked_at':now()}
    if p.get('venue_kind') != 'journal':return q
    records=[r for r in w.get('journals',[]) if r.get('qualification_schema')==3 and set(p.get('issns',[])) & set(r.get('issns',[]))]
    if not records:return q
    version=lambda r:(r.get('metric_year') if type(r.get('metric_year')) is int else 99999,r.get('checked_at',''))
    latest=max(version(r) for r in records)
    same_year=[r for r in records if version(r)[0]==latest[0]]
    records=[r for r in records if version(r)==latest]
    q['evidence']=records
    try:
        for r in records:validate(r)
        if len({digest(r) for r in records}) != 1:raise ValueError('同版本证据冲突')
        r=records[0]
        if any((x.get('categories'),x.get('sci_index')) != (r.get('categories'),r.get('sci_index')) and digest(x) not in r.get('supersedes',[]) for x in same_year):raise ValueError('同年来源冲突，须明确复核并记录supersedes')
        q.update(metric_year=r['metric_year'],jcr_year=r['jcr_year'],report_year=r.get('report_year'),categories=r['categories'],reviewer_type=r['reviewer_type'],user_reviewed=r['user_reviewed'])
        if not set(p.get('issns',[])) <= set(r['issns']) or p.get('venue','').casefold() not in [n.casefold() for n in [r['name']]+r.get('verified_names',[])]:
            raise ValueError('ISSN集合或刊名冲突；保留原题录待核验')
        if r['review_due'] < now()[:10] or r.get('source_status') in ('changed','refresh_failed'):
            raise ValueError('证据到期或来源变化，进入待复核清单')
        if r['sci_index']!='SCIE' or not any(x['quartile'] in ('Q1','Q2') for x in r['categories']):
            q.update(status='failed',reason='本JCR通道无SCIE＋JIF Q1/Q2支持');return q
        if p.get('publication',{}).get('status')!='published' or not p.get('doi'):raise ValueError('正式发表身份待核验')
        q['fulltext_verification_allowed']=True
        reviews=[x for x in w.get('jcr_content_reviews',[]) if x.get('paper_id')==p.get('id')]
        from .jcr_auto import read_record
        auto=read_record(w,p)
        if auto:
            from .jcr_auto import RULE,PROMPT
            if auto.get('source_file'):
                source=local_path(auto['source_file'])
                if not source.is_file() or digest(source.read_bytes())!=auto.get('source_sha256'):raise ValueError('自动审核全文变化或缺失，待重新审核')
            if auto.get('rule')!=RULE or auto.get('prompt_sha256')!=digest(PROMPT):raise ValueError('自动审核规则变化，待重新审核')
            if {x.get('category') for x in auto.get('relations',[])} != {x['category'] for x in r['categories']}:raise ValueError('自动审核学科列表变化，待重新审核')
            if reviews and any(x.get('relations') != auto.get('relations') for x in reviews):raise ValueError('手动与自动学科审核冲突，待明确复核')
            if not reviews:reviews=[auto]
        if not reviews:raise ValueError('允许获取全文核验；学科相关性尚未审定，不具正式接纳资格')
        review=max(reviews,key=lambda x:x.get('checked_at',''))
        from .eligibility import stamp
        if not stamp(review.get('checked_at')) or review.get('doi')!=p.get('doi') or review.get('reviewer_type') not in ('codex','user','expert','ai_api') or type(review.get('user_reviewed')) is not bool or (review['reviewer_type'] in ('codex','ai_api') and review['user_reviewed']):raise ValueError('论文审核身份、时间或DOI不符')
        f=artifact(review); content=f.read_text('utf-8')
        if review.get('basis')!='fulltext' or review.get('complete') is not True:raise ValueError('题录/摘要初筛不能替代全文内容审核')
        if review.get('status','reviewed')!='reviewed':raise ValueError('内容审核已撤回或待更新')
        relations=review.get('relations',[]);q['content_relations']=relations;q['content_review']=review
        if {x.get('category') for x in relations}!={x['category'] for x in r['categories']} or len(relations)!=len(r['categories']):raise ValueError('须保留全部学科关系')
        for x in relations:
            if type(x.get('related')) is not bool or not x.get('reason') or not x.get('location') or not x.get('quote') or x['quote'] not in content:raise ValueError('学科匹配缺原文依据/位置，待审核')
        adopted=[c for c in r['categories'] if c['quartile'] in ('Q1','Q2') and any(x['category']==c['category'] and x['related'] is True for x in relations)]
        q.update(status='passed' if adopted else 'failed',adopted_categories=adopted,reason='相关学科JIF Q1/Q2及SCIE已审核；当前指标不回填发表年份' if adopted else '有依据的相关学科不满足JIF Q1/Q2')
    except (ValueError,TypeError,KeyError,OSError) as e:q['reason']=str(e)
    return q


def label(q):
    if q['status']!='passed':return q['reason']
    return '按 JCR 阅读资格推荐｜'+str(q['metric_year'])+' 指标｜采用学科：'+'；'.join(x['category']+'｜JIF '+x['quartile'] for x in q['adopted_categories'])
