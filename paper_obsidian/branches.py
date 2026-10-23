"""Branch policy over the existing shared Store and source adapters."""
from copy import deepcopy
from .core import today, now

DOMAINS = {
    '医疗健康': ['medical', 'clinical', 'health', 'diagnosis', 'disease'],
    '生物化学': ['protein', 'molecule', 'chemical', 'genomic', 'drug'],
    '材料能源': ['material', 'battery', 'energy', 'solar'],
    '工业制造': ['manufacturing', 'industrial', 'fault detection'],
    '环境农业': ['climate', 'environment', 'agriculture', 'crop'],
    '交通教育': ['traffic', 'autonomous driving', 'education', 'student'],
}
METHODS = ['machine learning', 'neural', 'transformer', 'deep learning', 'reinforcement learning', 'language model', 'computer vision']
FIELDS = {'domain_problem':'行业问题', 'computer_contribution':'计算机贡献', 'cross_value':'交叉价值',
          'domain_evidence':'领域实验依据', 'reproduction':'复现条件', 'application_limits':'应用限制'}

def matches(p):
    text = (p.get('title','')+' '+p.get('abstract','')).lower()
    return [k for k,v in DOMAINS.items() if any(t in text for t in v)] if any(t in text for t in METHODS) else []

def primary(s, pid):
    return s.get_state('branch:'+pid, {}).get('primary','ordinary')

def assign(s, p, c):
    old = s.get_state('branch:'+p['id'])
    if old:return old['primary']
    # Existing discoveries and deliveries never move automatically.
    created = s.db.execute('SELECT created FROM papers WHERE id=?',(p['id'],)).fetchone()
    domains = matches(p)
    cross = c.get('cross',{})
    branch = 'cross' if (cross.get('enabled') and domains and created and created[0] >= cross.get('new_since','9999') and not s.stage(p['id'],'delivery')) else 'ordinary'
    s.set_state('branch:'+p['id'],dict(primary=branch, domains=domains, assigned_at=now()))
    return branch

def used(s, branch):
    from .briefs import delivery_events
    return sum(e['day']==today().replace('-','.') and e['kind']=='新论文' and primary(s,pid)==branch
               for pid,e in delivery_events(s).items())

def search_config(c):
    out=deepcopy(c)
    from datetime import date, timedelta
    # New branch starts a bounded recent window, never historical full backfill.
    out['search'].update(keywords=c['cross']['keywords'], exclude=c['cross'].get('exclude',[]),
        start_date=max(c['cross']['new_since'][:10], (date.fromisoformat(today())-timedelta(days=c['search']['lookback_days'])).isoformat()))
    out['_branch']='cross'
    return out

def admission(c,s,p):
    from .capacity import preview, evidence, fingerprint
    proposed=dict(id=p['id'],area='cross',archived=False,note='',pin=False,title=p['title'],
                  evidence=evidence(p,c,s,{}),fingerprint=fingerprint(p,c,s,{}))
    plan=preview(c,s,proposed=proposed)
    return not any(x.get('area') in ('cross','all') for x in plan['problems']) and any(
        x['id']==p['id'] and x['decision']=='retain' for x in plan['decisions'])
