"""Read-only DeepSeek snapshots and run-scoped estimates from existing receipts.

No model calls, retries, currency conversion, or inferred account aggregation.
Rendering and offline replay only read saved data.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from .core import atomic_write, digest, now, ROOT, TZ

BALANCE_URL = 'https://api.deepseek.com/user/balance'
BALANCE_DOC = 'https://api-docs.deepseek.com/api/get-user-balance/'


def official(base):
    try:
        u = urlparse(base)
        return (u.scheme == 'https' and u.hostname == 'api.deepseek.com'
                and u.port in (None, 443) and not u.username and not u.password
                and u.path.rstrip('/') in ('', '/v1', '/beta')
                and not u.query and not u.fragment)
    except ValueError:
        return False


def source_id(env, prefix=''):
    base, key = env.get(prefix+'API_BASE_URL', ''), env.get(prefix+'API_KEY', '')
    return digest(['deepseek-official' if official(base) else base, key])[:24]


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('nonfinite amount')
    return result


def query_balance(key, transport=None):
    snapshot = {'queried_at': now(), 'status': 'pending'}
    try:
        # Explicit no redirects, no automatic retries, short wall/read limits.
        from .daily_model import wall_deadline
        with wall_deadline(6):
            with httpx.Client(timeout=httpx.Timeout(4, connect=2),
                              follow_redirects=False, transport=transport) as client:
                response = client.get(BALANCE_URL, headers={
                    'Authorization': 'Bearer '+key, 'Accept': 'application/json'})
                response.raise_for_status()
                raw = json.loads(response.text, parse_float=Decimal)
        if type(raw.get('is_available')) is not bool or not raw.get('balance_infos'):
            raise ValueError('invalid balance schema')
        balances = []
        for item in raw['balance_infos']:
            if item['currency'] not in ('CNY', 'USD'):
                raise ValueError('unknown currency')
            balances.append(dict(currency=item['currency'], **{
                k: str(number(item[k])) for k in
                ('total_balance', 'granted_balance', 'topped_up_balance')}))
        if len({b['currency'] for b in balances}) != len(balances):
            raise ValueError('ambiguous currencies')
        snapshot.update(status='success', is_available=raw['is_available'], balance_infos=balances)
    except httpx.HTTPStatusError as exc:
        snapshot.update(status='http_error', http_status=exc.response.status_code)
    except (httpx.TimeoutException, TimeoutError):
        snapshot['status'] = 'timeout'
    except Exception as exc:
        # Never retain response bodies, exception messages, keys or headers.
        snapshot.update(status='query_failed', error_type=type(exc).__name__)
    return snapshot


def changes(before, after):
    if not before or not after or before['status'] != 'success' or after['status'] != 'success':
        return []
    old = {b['currency']: b for b in before['balance_infos']}
    new = {b['currency']: b for b in after['balance_infos']}
    result = []
    for currency in sorted(set(old) | set(new)):
        if currency not in old or currency not in new:
            result.append({'currency': currency, 'direction': '不可用', 'amount': None,
                           'before': old.get(currency, {}).get('total_balance'),
                           'after': new.get(currency, {}).get('total_balance')})
            continue
        b = new[currency]
        delta = number(b['total_balance']) - number(old[b['currency']]['total_balance'])
        result.append({'currency': b['currency'], 'direction': '增加' if delta > 0 else '减少' if delta < 0 else '不变',
                       'amount': str(abs(delta)), 'before': old[b['currency']]['total_balance'],
                       'after': b['total_balance']})
    return result


def receipt_rows(c, run_id):
    """Only newly attempted requests belong to this run; cache hits add no row."""
    found, seen = [], set()
    for kind, pattern in (('summary', 'daily-api/*/attempts.json'),
                          ('translation', 'daily-translation/*/requests.json')):
        for path in c['paths']['data'].glob(pattern):
            for index, row in enumerate(json.loads(path.read_text('utf-8'))):
                if str(row.get('run_id')) != str(run_id):
                    continue
                identity = row.get('request_id') or f'{path.parent.name}:{kind}:{index}'
                if identity in seen:
                    continue
                seen.add(identity)
                found.append(dict(row, request_id=identity, operation=row.get('kind'), kind=kind,
                                  receipt=path.relative_to(c['paths']['data']).as_posix()))
    return found


def estimate(c, run, rows):
    """Explicit, time-bounded prices only; unknown requests invalidate the total.

    Price rows are source/model/currency specific, with cache hit/miss/output
    prices per million and an evidence URL + checked_at + validity interval.
    No default remembered tariff or holiday/peak assumption is applied.
    """
    rows=list({r["request_id"]:r for r in rows}.values())
    reasons, totals, evidence = [], {}, []
    multiple=len({r.get("source_id") for r in rows})>1
    expected = run.get('model_requests')
    if expected is None or expected != sum(r['kind'] == 'summary' for r in rows):
        reasons.append('总结请求数与运行回执未完整核对')
    translations = [r for r in rows if r['kind'] == 'translation']
    launches = run.get('translation_launches', [])
    for launch in launches:
        if launch.get('status') != 'finished' or launch.get('network_requests') is None:
            reasons.append('翻译执行未完整收尾，可能存在未回执费用')
    expected_tr = sum(x.get('network_requests') or 0 for x in launches)
    if expected_tr != len(translations):
        reasons.append('翻译请求统计不完整')
    for row in rows:
        record = {k: row.get(k) for k in ('request_id', 'kind', 'operation', 'receipt', 'source_id', 'requested_model',
                                         'response_model', 'status', 'usage', 'started', 'ended', 'finished')}
        u = row.get('usage') or {}
        keys = ('prompt_cache_hit_tokens', 'prompt_cache_miss_tokens', 'completion_tokens', 'prompt_tokens')
        if (row.get('status') in ('pending', 'uncertain') or
                any(type(u.get(k)) is not int or u[k] < 0 for k in keys) or
                u.get('prompt_tokens') != u.get('prompt_cache_hit_tokens', 0)+u.get('prompt_cache_miss_tokens', 0)):
            reasons.append('请求用量缺失／结果未知（可能已计费）')
            record['cost_status'] = 'usage_unknown'
            evidence.append(record)
            continue
        rates = []
        for original in c.get('api_accounting', {}).get('prices', []):
            price=copy.deepcopy(original)
            try:
                start = datetime.fromisoformat(row['started'])
                end = datetime.fromisoformat(row.get('ended') or row.get('finished'))
                if price.get('tariff')=='beijing-offpeak-v1':
                    def offpeak(stamp):
                        local=stamp.astimezone(TZ)
                        if local.weekday()>=5 or not (9<=local.hour<12 or 14<=local.hour<18):return True
                        calendar=price.get('calendar',{})
                        day=local.date().isoformat()
                        if not calendar.get('source') or not (calendar.get('valid_from','9999')<=day<calendar.get('valid_until','')):return None
                        return day in calendar.get('holidays',[])
                    # Holiday peak exceptions require a verified calendar; unknown stays unknown.
                    from datetime import timedelta
                    if end<start or end-start>timedelta(days=1):continue
                    points=[start,end];point=start
                    while point<end:
                        points.append(point);point+=timedelta(minutes=1)
                    periods={offpeak(x) for x in points}
                    if None in periods or len(periods)!=1:continue
                    selected=periods.pop()
                    if selected:
                        for k in ('cache_hit','cache_miss','output'):price[k]=str(number(price[k])/2)
                    price['selected_period']='offpeak' if selected else 'peak'
                valid_from = datetime.fromisoformat(price['valid_from'])
                valid_until = datetime.fromisoformat(price['valid_until'])
                if (price.get('source_id') == row.get('source_id') and
                        price.get('model') == row.get('requested_model') and
                        price.get('source') and price.get('checked_at') and
                        price.get('currency') in ('CNY', 'USD') and
                        valid_from <= start <= end < valid_until and
                        all(number(price[k]) >= 0 for k in ('cache_hit', 'cache_miss', 'output'))):
                    rates.append(price)
            except (ValueError, TypeError, KeyError, ArithmeticError):
                continue
        if len(rates) != 1:
            reasons.append('缺少适用模型／时段的明确单价')
            record['cost_status'] = 'price_unknown'
        else:
            price = rates[0]
            amount = (number(u['prompt_cache_hit_tokens'])*number(price['cache_hit']) +
                      number(u['prompt_cache_miss_tokens'])*number(price['cache_miss']) +
                      number(u['completion_tokens'])*number(price['output']))/Decimal(1000000)
            currency = price['currency']
            group=(str(row.get('source_id'))+'/'+currency) if multiple else currency
            totals[group] = totals.get(group, Decimal(0))+amount
            record.update(cost_status='estimated', amount=str(amount), currency=currency, price=copy.deepcopy(price))
        evidence.append(record)
    if not rows and not reasons:
        # Proven no network attempts, rather than assuming failed requests free.
        return {'status': 'complete', 'amounts': {}, 'no_paid_requests': True, 'requests': [], 'reasons': []}
    return {'status': 'incomplete' if reasons else 'complete',
            'amounts': {} if reasons else {k: str(v) for k, v in totals.items()},
            'known_subtotals': {k: str(v) for k, v in totals.items()},
            'requests': evidence, 'reasons': sorted(set(reasons))}


class RunAccounting:
    def __init__(self, c, env, store, run_id, started, transport=None):
        c=copy.deepcopy(c)
        price_file=c.get("api_accounting",{}).get("price_file")
        if price_file:
            c.setdefault("api_accounting",{})["prices"]=json.loads((ROOT/price_file).read_text("utf-8"))["prices"]
        self.c, self.env, self.store, self.transport = c, env, store, transport
        self.path = c['paths']['data']/'daily-runs'/f'{run_id}.api.json'
        self.run = {'run_id': str(run_id), 'started': started, 'ended': None, 'status': 'running',
                    'balance_documentation': BALANCE_DOC, 'sources': [], 'translation_launches': [],
                    'estimate': {'status': 'incomplete', 'reasons': ['运行尚未结束']}}
        for prefix, label in (('', '总结配置 API_BASE_URL / API_KEY'),
                              ('TRANSLATION_', '翻译配置 TRANSLATION_API_BASE_URL / TRANSLATION_API_KEY')):
            sid = source_id(env, prefix)
            existing = next((s for s in self.run['sources'] if s['source_id'] == sid), None)
            if existing:
                existing['labels'].append(label)
                continue
            self.run['sources'].append({'source_id': sid, 'labels': [label], 'prefix': prefix,
                'official': official(env.get(prefix+'API_BASE_URL', '')),
                'credential_configured': bool(env.get(prefix+'API_KEY'))})

    def save(self):
        atomic_write(self.path, json.dumps(self.run, ensure_ascii=False, indent=2))

    def snapshot(self, phase):
        for source in self.run['sources']:
            if phase in source:
                continue  # at most once per phase/source
            if source['official'] and source['credential_configured']:
                snap = query_balance(self.env[source['prefix']+'API_KEY'], self.transport)
            else:
                snap = {'status': 'not_official' if not source['official'] else 'not_configured', 'queried_at': None}
            source[phase] = snap
            state_key = 'api-balance:'+source['source_id']
            if snap['status'] == 'success':
                self.store.set_state(state_key, snap)
            else:
                source['historical'] = self.store.get_state(state_key)
            self.save()

    def begin(self):
        self.save()
        self.snapshot('before')

    def translation_started(self, folder):
        launch = {'folder': folder, 'status': 'running', 'started': now()}
        self.run['translation_launches'].append(launch)
        self.save()
        return launch

    def track(self, action):
        try:
            return action()
        except Exception as exc:
            self.run.setdefault('warnings', []).append(type(exc).__name__)

    def translation_finished(self, launch, detail):
        launch.update(status='finished', ended=now(),
                      network_requests=detail.get('usage', {}).get('network_attempts_this_run'))
        self.save()

    def finish(self, report):
        self.snapshot('after')
        self.run.update(ended=now(), status='partial' if report.get('failures') or report.get('stopped') else 'finished',
                        model_requests=report['model_requests'])
        for source in self.run['sources']:
            source['changes'] = changes(source.get('before'), source.get('after'))
        self.run['estimate'] = estimate(self.c, self.run, receipt_rows(self.c, self.run['run_id']))
        if report.get('accounting_warnings') or self.run.get('warnings'):
            self.run['estimate'].update(status='incomplete', amounts={})
            self.run['estimate'].setdefault('reasons', []).append('余额／费用记录过程出现异常，统计未完整核对')
        self.save()
        self.run['day_estimate']=day_estimate(self.c,self.run)
        self.save()
        report['api_accounting'] = copy.deepcopy(self.run)


def guarded(report, action):
    """Ancillary accounting must never replace a paper-processing exception."""
    try:
        return action()
    except Exception as exc:
        report.setdefault('accounting_warnings', []).append(type(exc).__name__)
        report['api_accounting'] = dict(report.get('api_accounting', {}),
            estimate={'status': 'incomplete', 'reasons': ['余额／费用回执处理失败']})


def money(amount, currency):
    return {'CNY': '¥', 'USD': '$'}.get(currency, currency+' ')+str(amount)


def day_estimate(c, current):
    day=(current.get('ended') or current['started'])[:10]; requests={}; reasons=[]
    runs={}
    for path in (c['paths']['data']/'daily-runs').glob('*.api.json'):
        r=json.loads(path.read_text('utf-8'))
        if r.get('started','')[:10]<=day and (r.get('ended') or day)[:10]>=day:runs[str(r['run_id'])]=r
    runs[str(current['run_id'])]=current
    for r in runs.values():
        est=r.get('estimate',{})
        if est.get('status')!='complete':reasons+=est.get('reasons') or ['存在未收尾运行']
        for row in est.get('requests',[]):
            stamp=row.get('started')
            if not stamp:reasons.append('请求时间未知');continue
            if datetime.fromisoformat(stamp).astimezone(TZ).date().isoformat()==day:
                requests.setdefault(row['request_id'],row)
    for path in (c['paths']['data']/'daily-runs').glob('*.json'):
        if path.name.endswith('.api.json'):continue
        r=json.loads(path.read_text('utf-8'))
        if r.get('started','')[:10]==day and str(r.get('run_id')) not in runs:
            reasons.append('存在缺少费用回执的运行')
    totals={}
    for row in requests.values():
        if row.get('cost_status')!='estimated':continue
        key=str(row.get('source_id'))+'/'+row['currency']
        totals[key]=totals.get(key,Decimal(0))+number(row['amount'])
    return dict(status='incomplete' if reasons else 'complete',amounts={k:str(v) for k,v in totals.items()},
                reasons=sorted(set(reasons)),requests=len(requests),run_ids=sorted(runs))


def render(run, *, historical=False):
    run=run or {}; balances=[]; changes_text=[]
    for source in run.get('sources',[]):
        snap=source.get('observation') or source.get('after') or {}
        old=False
        if snap.get('status')!='success':
            snap=source.get('historical') or source.get('before') or {};old=True
        label=source.get('source_id','来源')[:8]
        value='未查询'
        if snap.get('status')=='success':
            value=' / '.join(money(b['total_balance'],b['currency']) for b in snap['balance_infos'])
            value+=('（历史快照，查询失败）' if old else '')+' @ '+str(snap.get('queried_at','时间未知'))
        balances.append(label+'：'+value)
        for item in source.get('changes',[]):
            changes_text.append(label+'：'+item['direction']+' '+(money(item['amount'],item['currency']) if item.get('amount') is not None else '未知'))
    def cost(est):
        values=est.get('amounts') or est.get('known_subtotals') or {}
        text=' / '.join(money(v,k) for k,v in values.items())
        if est.get('status')!='complete':return (text+'（已知部分）；' if text else '')+'统计不完整，未知部分未填零'
        return (text or '0（已确认无新增付费请求）')+'；统计完整'
    return ['1. 最新账户余额：'+('；'.join(balances) or '未查询'),
            '2. 最近运行估算：'+cost(run.get('estimate',{})),
            '3. 当天累计估算：'+cost(run.get('day_estimate',{})),
            '4. 账户余额变化（独立参考）：'+('；'.join(changes_text) or '未知'),'']
