"""Journal identity and sourced observations; never grants formal eligibility.

Uses the existing SQLite state, HTTP cache, stable paper IDs and atomic backups.
No model, browser cookies, private endpoints or subscription workarounds.
"""
from __future__ import annotations

import copy
import csv
import html
import json
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from .core import atomic_write, digest, local_path, now, safe_error, today, TZ
from .eligibility import official, stamp
from .sources import HTTP

BEGIN, END = '<!-- JOURNAL-INFO:START -->', '<!-- JOURNAL-INFO:END -->'
FIELDS = ('scie', 'cas_major', 'cas_minor', 'jcr', 'jif', 'xinrui')
LABELS = {'scie': 'SCIE', 'cas_major': '中科院大类', 'cas_minor': '中科院小类',
          'jcr': 'JCR学科分区', 'jif': 'Journal Impact Factor（JIF）', 'xinrui': '新锐分区'}
UNKNOWN = '待核验／暂无数据'


class JournalLookupError(ValueError):
    """Only fixed local diagnostics, never arbitrary provider response text."""


def journal_error(exc):
    return str(exc) if isinstance(exc,JournalLookupError) else safe_error(exc)


def issn(value):
    v = re.sub(r'[-\s]', '', str(value)).upper()
    if not re.fullmatch(r'\d{7}[\dX]', v):
        raise ValueError('ISSN格式无效')
    if (sum(int(x)*(8-i) for i, x in enumerate(v[:7])) + (10 if v[-1] == 'X' else int(v[-1]))) % 11:
        raise ValueError('ISSN校验位错误')
    return v[:4] + '-' + v[4:]


def issns(values):
    if isinstance(values, str):
        values = re.split(r'[;,；]', values)
    return sorted({issn(v) for v in (values or []) if str(v).strip()})


def clean(v):
    return html.escape(str(v), quote=False).replace('|', '\\|').replace('\n', ' ').replace('[', '［').replace(']', '］')


def source_link(url, label='来源'):
    p = urlparse(url or '')
    if p.scheme != 'https' or not p.hostname or p.username or p.password:
        return UNKNOWN
    return '[' + label + '](' + str(url).replace('(', '%28').replace(')', '%29').replace(' ', '%20') + ')'


def parse_mjl(markdown, query_issn, url, checked, evidence):
    """Only the single exact result's ISSN and Core Collection block count.

    Filter lists, footers, 'Has JIF', empty results and multi-result pages prove
    nothing. A current snapshot is never a historical indexing conclusion.
    """
    if not re.search(r'Found\s+1\s+results?', markdown, re.I):
        raise JournalLookupError('MJL未返回单个精确结果（空结果或多结果），不能确认收录')
    parts = re.split(r'##\s*Exact Match Found', markdown, flags=re.I)
    if len(parts) != 2:
        raise JournalLookupError('MJL无精确匹配区块')
    block = re.split(r'\n(?:\* \* \*|Items per page:|\*\*Editorial Disclaimer)', parts[1])[0]
    match = re.search(r'ISSN\s*/\s*eISSN:\s*(.*?)\n\s*_?Web of Science', block, re.S | re.I)
    if not match:
        raise JournalLookupError('MJL缺少可核对的ISSN')
    identifiers = issns(re.findall(r'\d{4}-\d{3}[\dXx]', match[1]))
    if query_issn not in identifiers:
        raise JournalLookupError('MJL结果ISSN不匹配')
    core = re.search(r'Core Collection:\s*(.*?)(?:Additional|$)', block, re.S | re.I)
    if not core:
        raise JournalLookupError('MJL未给出Core Collection')
    text = core[1]
    if 'Science Citation Index Expanded' in text:
        value = True
    elif any(x in text for x in ('Emerging Sources Citation Index', 'Social Sciences Citation Index', 'Arts & Humanities Citation Index')):
        value = False
    else:
        raise JournalLookupError('MJL未明确索引类型')
    return {'field': 'scie', 'value': value, 'as_of': checked[:10], 'source': url,
            'checked_at': checked, 'authority': 'official', 'verification': 'verified_official',
            'scope': 'current_snapshot', 'evidence_file': str(evidence),
            'note': 'Clarivate MJL公开精确ISSN结果；仅证明查询日状态，不证明论文发表年份的收录。'}


def validate_metric(row, base):
    r = {k: v for k, v in row.items() if v not in ('', None)}
    field = r.get('field')
    if field not in FIELDS:
        raise ValueError('指标类型须为scie/cas_major/cas_minor/jcr/jif/xinrui')
    if not r.get('source') or source_link(r['source']) == UNKNOWN or not stamp(r.get('checked_at')):
        raise ValueError('每项指标须有HTTPS来源及非未来的带时区查询日期')
    if field == 'scie':
        if isinstance(r.get('value'), str):
            r['value'] = {'true': True, 'false': False}.get(r['value'].lower(), r['value'])
        if type(r.get('value')) is not bool or not r.get('as_of'):
            raise ValueError('SCIE须明确true/false及as_of日期；不能由JIF推断')
        datetime.strptime(r['as_of'], '%Y-%m-%d')
        if r['as_of'] > r['checked_at'][:10]:
            raise ValueError('SCIE状态日期不得晚于查询日期')
        if r.get('index_year'):
            r['index_year']=int(r['index_year'])
            if not 1900 <= r['index_year'] <= int(r['checked_at'][:4]):
                raise ValueError('SCIE索引依据年份无效')
    else:
        year_key = 'edition_year' if field in ('cas_major', 'cas_minor', 'xinrui') else 'metric_year'
        if not str(r.get(year_key, '')).isdigit():
            raise ValueError('指标或分区版本年份缺失')
        r[year_key] = int(r[year_key])
        if not 1900 <= r[year_key] <= int(r['checked_at'][:4]):
            raise ValueError('指标年份无效')
        if field in ('cas_major', 'cas_minor', 'xinrui') and not r.get('edition'):
            raise ValueError('分区须明确版本名称')
        if field != 'jif' and not r.get('category'):
            raise ValueError('分区须明确学科；多个学科分别导入')
        if field in ('cas_major', 'cas_minor', 'xinrui'):
            r['value'] = int(r.get('value', -1))
            if r['value'] not in (1, 2, 3, 4):
                raise ValueError('分区只接受1-4，不以0表示缺失')
        if field == 'jcr' and r.get('value') not in ('Q1', 'Q2', 'Q3', 'Q4'):
            raise ValueError('JCR分区须为Q1-Q4')
        if field == 'jif':
            import math
            if isinstance(r.get('value'), bool):
                raise ValueError('JIF须为数值')
            r['value'] = float(r.get('value', -1))
            if not math.isfinite(r['value']) or r['value'] < 0:
                raise ValueError('JIF须为实际非负数值；缺失则不导入该项')
        if r.get('report_year'):
            r['report_year'] = int(r['report_year'])
            if not r[year_key] <= r['report_year'] <= int(r['checked_at'][:4]):
                raise ValueError('报告发布年无效；不得与指标年混淆')
    domains = ['clarivate.com'] if field in ('scie', 'jcr', 'jif') else ['fenqubiao.com', 'las.ac.cn', 'las.cas.cn'] if field != 'xinrui' else []
    is_official = official(r['source'], domains)
    r['authority'] = 'official' if is_official else 'third_party'
    # A source URL alone cannot assert verification. Keep an auditable artifact.
    if r.get('verification') == 'verified_official':
        if not is_official or not r.get('evidence_file'):
            raise ValueError('官方核验导入须有对应官方来源及本地证据')
    r['verification'] = 'verified_official' if is_official and r.get('verification') == 'verified_official' else ('unverified' if is_official else 'third_party')
    if r.get('evidence_file'):
        path = local_path(r['evidence_file'], base).resolve()
        if not path.is_file():
            raise ValueError('导入证据文件不存在')
        r['evidence_file'] = str(path)
        r['evidence_sha256'] = digest(path.read_bytes())
    r.setdefault('scope', 'reference_only')
    return r


def metric_year(r):
    return int(r.get('index_year') or r.get('edition_year') or r.get('metric_year') or str(r.get('as_of', '0000'))[:4])


def metric_key(r):
    return (r['field'], metric_year(r), r.get('edition', ''), r.get('category', ''),
            r.get('as_of', '') if r['field'] == 'scie' else '')


def conflicts(metrics):
    groups = {}
    for r in metrics:
        groups.setdefault(metric_key(r), set()).add(json.dumps(r['value']))
    return {k for k, values in groups.items() if len(values) > 1}


class JournalService:
    def __init__(self, c, env, store, http=None, mjl_fetch=None):
        self.c, self.env, self.s = c, env, store
        self.opts = c.get('journals', {})
        self.h = http
        self.owns_http = http is None
        self.mjl_fetch = mjl_fetch

    def http(self):
        if self.h is None:
            c = copy.deepcopy(self.c)
            c['network'].update(timeout_seconds=self.opts.get('timeout_seconds', 15), retries=0)
            self.h = HTTP(c, self.env)
        return self.h

    def close(self):
        if self.owns_http and self.h and hasattr(self.h, 'client'):
            self.h.client.close()

    def cached(self, key):
        alias = self.s.get_state('journal:alias:' + key, key)
        return self.s.get_state('journal:cache:' + alias)

    def public_mjl(self, identifier):
        url = 'https://mjl.clarivate.com/search-results?issn=' + identifier
        if self.mjl_fetch:
            return self.mjl_fetch(url), url
        command = self.opts.get('mjl_command', [])
        if not command:
            raise ValueError('未配置公开MJL网页渲染器；可导入官方核验记录')
        # Fixed configured executable + public URL; never shell evaluation/cookies.
        command = list(command)
        command[0] = str(local_path(command[0])) if ':' in command[0] else command[0]
        result = subprocess.run(command + ['scrape', url, '--format', 'markdown', '--proxy', 'basic',
                                '--max-age', '0', '--json'], capture_output=True,
                                timeout=self.opts.get('mjl_timeout_seconds', 45), check=False)
        if result.returncode:
            raise RuntimeError('MJL公开网页获取失败；不重试或绕过')
        data = json.loads(result.stdout.decode('utf-8-sig'))
        metadata = data.get('metadata', {})
        if metadata.get('statusCode') != 200 or metadata.get('proxyUsed', 'basic') != 'basic':
            raise ValueError('MJL访问受限或抓取方式不符')
        final = urlparse(metadata.get('url') or url)
        if final.hostname != 'mjl.clarivate.com' or final.path.rstrip('/') != '/search-results':
            raise ValueError('MJL跳转登录或其他页面；停止')
        markdown = data.get('markdown', '')
        if any(x in markdown.lower() for x in ('verify you are human', 'verifying your browser', 'captcha', 'access denied')):
            raise ValueError('MJL访问限制；停止')
        return markdown, url

    def lookup(self, p, *, offline=False, refresh=False):
        checked = now()
        info = {'name': p.get('venue') or UNKNOWN, 'issns': [], 'metrics': [],
                'identity_status': 'unverified', 'checked_at': checked, 'issues': []}
        if p.get('venue_kind') != 'journal':
            return info
        try:
            ids = issns(p.get('issns'))
        except ValueError:
            info['issues'].append('来源ISSN格式或校验位无效；不按名称强配')
            return info
        info['issns'] = ids
        if not ids:
            info['issues'].append('缺少ISSN/eISSN；名称只作辅助，不强行匹配')
            return info
        cache = self.cached(ids[0])
        compatible = cache and set(ids) <= set(cache.get('issns', []))
        fresh = compatible and cache.get('expires_at', '') > checked
        if compatible and (fresh or offline) and not refresh:
            info = copy.deepcopy(cache)
            info['stale'] = bool(info.get('stale')) or not fresh
        elif offline:
            info['issues'].append('离线缓存未命中；未联网查询')
        else:
            if compatible:
                info = copy.deepcopy(cache)
                info['issues'] = []
                info['stale'] = True
            # Resolve every supplied ISSN; two distinct sources are ambiguous.
            sources = {}
            try:
                for identifier in ids:
                    item = self.http().json('https://api.openalex.org/sources/issn:' + identifier)
                    found = issns(item.get('issn'))
                    if item.get('type') != 'journal' or identifier not in found:
                        raise JournalLookupError('OpenAlex期刊类型或ISSN不一致')
                    sources[item['id']] = item
                if len(sources) != 1:
                    raise JournalLookupError('多个ISSN指向不同期刊；存在更名或同名歧义，拒绝合并')
                item = next(iter(sources.values()))
                all_ids = issns(item.get('issn'))
                key = issn(item.get('issn_l') or all_ids[0])
                # Another paper's eISSN may already have a valid journal cache.
                other = self.cached(key)
                if other and other.get('expires_at', '') > checked and not refresh:
                    info = copy.deepcopy(other)
                else:
                    info.update(name=item['display_name'], issns=all_ids, issn_l=key,
                                identity_status='issn_exact', identity_source=item['id'], identity_checked_at=checked,
                                identity_api='https://api.openalex.org/sources/issn:' + ids[0])
                info['issues'] = list(info.get('issues', []))
            except Exception as exc:
                info['issues'].append('OpenAlex期刊识别未完成：' + journal_error(exc) + '；保留原始题录，不按名称猜测')
                info['identity_status'] = 'unverified'
            key = info.get('issn_l') or ids[0]
            # Official indexing is independent of the identity request.
            official_fresh = any(r['field'] == 'scie' and r.get('verification') == 'verified_official'
                and r.get('checked_at', '') > (datetime.now(TZ)-timedelta(days=self.opts.get('cache_days',30))).isoformat() for r in info['metrics'])
            if (not official_fresh or refresh) and info['identity_status'] == 'issn_exact' and self.opts.get('mjl_enabled', False):
                if self.s.spend('journal_official_lookup', self.opts.get('max_official_per_day', 3)):
                    attempt={'source':'https://mjl.clarivate.com/search-results?issn='+key,'checked_at':checked,'status':'pending'}
                    try:
                        markdown, url = self.public_mjl(key)
                        evidence = self.c['paths']['data']/'journal-evidence'/(digest([url, markdown])+'.md')
                        atomic_write(evidence, markdown)
                        attempt.update(evidence_file=str(evidence),evidence_sha256=digest(markdown))
                        metric = parse_mjl(markdown, key, url, checked, evidence)
                        metric['evidence_sha256'] = digest(markdown)
                        info['metrics'] = merge_metrics(info['metrics'], [metric])
                        attempt['status']='verified'
                    except Exception as exc:
                        attempt.update(status='unverified',reason=journal_error(exc))
                        info['issues'].append('Clarivate公开MJL核验未完成：'+journal_error(exc)+'；不由JIF推断SCIE')
                    info['official_attempt']=attempt
                else:
                    info['issues'].append('已达每日低频MJL查询上限；下次补齐')
            success = info['identity_status'] == 'issn_exact' and not info['issues']
            ttl = timedelta(days=self.opts.get('cache_days', 30)) if success else timedelta(hours=self.opts.get('failure_cache_hours',24))
            info.update(cache_key=key, checked_at=checked, expires_at=(datetime.now(TZ)+ttl).isoformat(timespec='seconds'), stale=bool(compatible and not success))
            self.s.set_state('journal:cache:' + key, info)
            if info['identity_status'] == 'issn_exact':
                for identifier in info['issns']:
                    self.s.set_state('journal:alias:' + identifier, key)
        info = copy.deepcopy(info)
        if info.get('identity_status') == 'issn_exact' and p.get('venue') and p['venue'].casefold() != info['name'].casefold():
            info['issues'].append('论文题录名称与当前来源名称不同（可能为更名/拼写差异）：'+p['venue']+'；不证明历史刊名适用范围')
        imported = []
        for identifier in info['issns']:
            imported += self.s.get_state('journal:import:' + identifier, [])
        info['metrics'] = merge_metrics(info['metrics'], imported)
        return info

    def annotate(self, p, gate, *, offline=False, refresh=False):
        info = self.lookup(p, offline=offline, refresh=refresh)
        # Reuse existing audited whitelist evidence for display. The original
        # classify() remains the only gate and is never populated by this cache.
        observations = []
        for r in gate.get('evidence', []):
            if not r.get('sci_index') or r.get('qualification_schema') in (2,3):continue
            observations += [dict(field='scie', value=True, as_of=r['sci_checked_at'][:10],
                index_year=int(r['sci_year']),scope='whitelist_historical_reference',
                source=r['sci_source'], checked_at=r['sci_checked_at'], authority='official',
                verification='verified_official', evidence_file=r['sci_evidence_file'],
                note='既有白名单证据；核验年份 '+str(r['sci_year'])+'；适用发表日期 '+r['coverage_from']+' 至 '+r['coverage_to']),
                dict(field='cas_major', value=r['cas_major_zone'], category=r['cas_major_category'],
                edition=r['cas_edition'], edition_year=int(r['cas_year']), source=r['cas_source'],
                checked_at=r['cas_checked_at'], authority='official', verification='verified_official',
                evidence_file=r['cas_evidence_file'])]
        info['metrics'] = merge_metrics(info['metrics'],observations)
        self.s.record(p['id'], 'journal_info', digest(info), 'success' if info['identity_status'] == 'issn_exact' else 'pending', info)
        return info


def merge_metrics(old, new):
    # Provenance observations are append-only; identical imports are idempotent.
    values = {digest(r): r for r in old + new}
    return [values[k] for k in sorted(values)]


def import_metrics(path, store):
    path = Path(path)
    if path.suffix.lower() == '.csv':
        with path.open(encoding='utf-8-sig', newline='') as f:
            groups = {}
            for row in csv.DictReader(f):
                identifiers = issns(row.pop('issns', ''))
                key = tuple(identifiers)
                groups.setdefault(key, {'issns': identifiers, 'metrics': []})['metrics'].append(row)
            records = list(groups.values())
    else:
        data = json.loads(path.read_text('utf-8-sig'))
        records = data['journals'] if isinstance(data, dict) else data
    validated = []
    for record in records:
        identifiers = issns(record.get('issns'))
        if not identifiers:
            raise ValueError('指标导入须有ISSN，不接受名称匹配')
        metrics = [validate_metric(r, path.parent) for r in record.get('metrics', [])]
        if not metrics:
            raise ValueError('空指标记录不导入')
        validated.append((identifiers, metrics))
    receipt = {'imported_at': now(), 'input_sha256': digest(path.read_bytes()), 'journals': len(validated),
               'metrics': sum(len(m) for _, m in validated), 'formal_whitelist_changed': False}
    # Validate entire file before a single transaction; reuse Store's SQLite.
    with store.db:
        for identifiers, metrics in validated:
            for identifier in identifiers:
                key = 'journal:import:' + identifier
                merged = merge_metrics(store.get_state(key, []), metrics)
                store.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(merged, ensure_ascii=False)))
    atomic_write(store.data/'journal-imports'/(digest(path.read_bytes())+'.json'),
                 json.dumps({'receipt':receipt,'records':validated}, ensure_ascii=False, indent=2))
    return receipt


def qualification(info, gate):
    if gate.get('admission_basis'):
        return gate['reason']
    if gate['eligible']:
        return '历史资格通道通过（旧回执，双口径尚未补录）'
    metrics = info.get('metrics', [])
    conflict = conflicts(metrics)
    scie = [r for r in metrics if r['field'] == 'scie']
    cas = [r for r in metrics if r['field'] == 'cas_major']
    latest_s = max((r.get('as_of','') for r in scie), default='')
    latest_c = max((metric_year(r) for r in cas), default=0)
    scie = [r for r in scie if r.get('as_of','') == latest_s]
    cas = [r for r in cas if metric_year(r) == latest_c]
    good_s = bool(scie) and all(r['value'] is True and metric_key(r) not in conflict for r in scie)
    good_c = bool(cas) and all(r['value'] in (1, 2) and metric_key(r) not in conflict for r in cas)
    third = any(r.get('authority') == 'third_party' for r in scie+cas)
    if good_s and good_c and third and not conflict and not info.get('stale'):
        return '第三方资料显示符合、待官方核验（按所列年份参考；本篇发表日期适用范围未核验；不计正式合格数量）'
    return '分区或SCIE收录信息未知／资格待核验（不计正式合格数量）'


def header(p, gate, store):
    lines = [BEGIN, '## 期刊／会议信息', '']
    if p.get('venue_kind') == 'conference':
        grades = [f"CCF {r['grade']}（目录{r['ccf_year']}年；{source_link(r.get('source'))}；查询{r.get('checked_at', UNKNOWN)}）"
                  for r in gate['evidence'] if r.get('ccf_year')]
        lines += [f"- 会议：{clean(p.get('venue') or UNKNOWN)}；年份：{clean(p.get('publication',{}).get('proceedings_year') or str(p.get('date',''))[:4] or UNKNOWN)}",
                  '- 等级：' + ('；'.join(grades) or UNKNOWN), '- 会议通道不适用期刊SCIE、分区或JIF。']
    elif p.get('venue_kind') == 'journal':
        stage = store.stage(p['id'], 'journal_info')
        info = json.loads(stage['detail']) if stage else {'name':p.get('venue') or UNKNOWN, 'issns':p.get('issns',[]), 'metrics':[], 'issues':['期刊查询尚未完成；允许后续补齐']}
        metrics = info.get('metrics', [])
        conflict = conflicts(metrics)
        pubyear = str(p.get('date',''))[:4]
        lines += ['- 资格状态：**'+qualification(info,gate)+'**',
                  '- 期刊全称：'+clean(info['name']),
                  '- ISSN/eISSN：'+clean(' / '.join(info.get('issns', [])) or UNKNOWN)+'；ISSN-L：'+clean(info.get('issn_l') or UNKNOWN),
                  '- 期刊识别：'+clean(info.get('identity_status', 'unverified'))+'；'+source_link(info.get('identity_api') or info.get('identity_source'))+'；查询：'+clean(info.get('identity_checked_at') or info.get('checked_at') or UNKNOWN),
                  ('- 标识说明：知网导出提供ISSN；未独立核验纸刊/电子刊角色或ISSN-L，不以此证明收录等级。'
                   if p.get('source')=='cnki' and not stage else
                   '- 标识说明：OpenAlex提供ISSN集合，未区分纸刊ISSN和电子eISSN角色时不猜测；ISSN-L是聚合标识。'),
                  '- 最新可取得记录与发表年份对应记录分别列示；最新仅指本地已取得，不保证覆盖所有年份。']
        labels={'passed':'通过','pending':'待核验','failed':'有明确证据不符合'}
        for key,title in [('current_qualification','当前资格'),('historical_qualification','发表时资格'),('jcr_qualification','JCR 阅读资格')]:
            q=gate.get(key,{'status':'pending','evidence':[]})
            lines += ['- '+title+'：'+labels[q['status']]+('（发表时资格已核验）' if key=='historical_qualification' and q['status']=='passed' else '')]
            for r in q.get('evidence',[]):
                if r.get('cas_edition'):
                    lines += [f"  中科院：{r.get('cas_year')} / {r.get('cas_edition')} / {r.get('cas_major_category')} / 大类{r.get('cas_major_zone')}区；SCIE证据日期：{r.get('sci_as_of',r.get('sci_checked_at'))}；查询：{r.get('cas_checked_at')}；审校者：{r.get('reviewer_type','旧回执未标明')}；用户复核：{r.get('user_reviewed','未记录')}"]
        jq=gate.get('jcr_qualification')
        if jq:
            from .jcr import label
            lines += ['- JCR说明：'+clean(label(jq))+'；中科院资格独立；审校方式：'+clean(jq.get('reviewer_type','未记录'))]
            for cat in jq.get('categories',[]):
                lines += ['- JIF学科：'+clean(cat['category'])+'｜'+cat['quartile']+'｜排名 '+cat['rank']]
        lines += ['- 本篇实际接纳依据：'+gate.get('reason','待核验')+'；资格判定查询日期：'+gate.get('qualification_checked_at','旧回执未记录')]
        for field in FIELDS:
            rows = [r for r in metrics if r['field'] == field]
            latest = max((metric_year(r) for r in rows), default=0)
            lines += ['', '### '+LABELS[field]]
            if not rows:
                lines += ['- '+UNKNOWN+'；来源／年份未取得。标注日期：'+clean(info.get('checked_at') or UNKNOWN)]
                if field!='scie':lines += ['- 尚未接通获准自动指标来源；可通过本地CSV/JSON导入。']
                continue
            for r in sorted(rows, key=lambda r:(metric_year(r),r.get('category',''),r.get('source','')), reverse=True):
                y = metric_year(r)
                scope = '最新可取得' if y == latest else '历史记录'
                value = ('是' if r['value'] else '否') if field == 'scie' else str(r['value'])
                years = (('历史白名单索引依据年份 '+str(r['index_year'])+'；核验日期 '+r.get('as_of',UNKNOWN)) if r.get('index_year') else ('状态日期 '+r.get('as_of',UNKNOWN))) if field == 'scie' else ('版本年份 '+str(y)+'；版本 '+r.get('edition',UNKNOWN)) if field in ('cas_major','cas_minor','xinrui') else ('指标年份 '+str(y)+'；JCR报告发布年份 '+str(r.get('report_year') or UNKNOWN))
                authority = '官方来源、已核验' if r.get('verification') == 'verified_official' else '第三方来源、待官方核验' if r.get('authority') == 'third_party' else '官方链接、证据待核验'
                lines += [f"- {scope}：{clean(r.get('category',''))} {clean(value)}；{clean(years)}；{authority}；{source_link(r['source'])}；查询 {clean(r['checked_at'])}" + ('；**来源冲突，待核验**' if metric_key(r) in conflict else '')]
                if r.get('note'):lines += ['  说明：'+clean(r['note'])]
            at_pub = [r for r in rows if str(metric_year(r)) == pubyear and r['field'] != 'scie']
            lines += [f'- 论文发表年份（{clean(pubyear or UNKNOWN)}）对应信息：'+('上列同年份记录，仅供该年参考；实际适用规则另核验。' if at_pub else '暂无数据；不以最新记录回填历史。')]
        lines += ['', '- 缺失／冲突／历史说明：有JIF不代表SCIE；JCR、中科院与新锐分区互不替代；当前SCIE快照不证明历史收录。',
                  '- 缓存：'+('已过期，以上仅历史参考；等待更新。' if info.get('stale') else '按期刊复用；查询失败不阻断正文或翻译。')]
        lines += ['- '+clean(x) for x in info.get('issues', [])]
    else:
        lines += ['- 发表场所：'+clean(p.get('venue') or UNKNOWN)+'；正式期刊／会议身份待核验，未套用期刊指标。']
    return '\n'.join(lines + ['', END])


def with_header(body, p, gate, store):
    if BEGIN in body or END in body:
        if body.count(BEGIN) != 1 or body.count(END) != 1 or body.index(END) < body.index(BEGIN):
            raise ValueError('期刊管理标记异常，拒绝覆盖')
        body = body[:body.index(BEGIN)] + body[body.index(END)+len(END):]
        body = body.lstrip('\r\n')
    return header(p, gate, store) + '\n\n' + body


def patch_note(path, p, gate, c, store):
    """Replace only our nested block; all original bytes outside it survive."""
    from .vault import START, END as PAPER_END
    path = Path(path)
    raw = path.read_bytes().decode('utf-8')
    if raw.count(START) != 1 or raw.count(PAPER_END) != 1 or raw.index(PAPER_END) < raw.index(START):
        raise ValueError('PAPER-AUTO标记异常；保留原文件')
    a, b = raw.index(START)+len(START), raw.index(PAPER_END)
    managed = raw[a:b]
    h = header(p, gate, store)
    if BEGIN in managed or END in managed:
        if managed.count(BEGIN) != 1 or managed.count(END) != 1 or managed.index(END) < managed.index(BEGIN):
            raise ValueError('期刊标记异常，拒绝覆盖')
        updated = managed[:managed.index(BEGIN)] + h + managed[managed.index(END)+len(END):]
    else:
        updated = '\n' + h + '\n' + managed
    return atomic_write(path, raw[:a]+updated+raw[b:], c['paths']['data']/'backups')


from .capacity import guarded


@guarded
def refresh_notes(c, env, store, whitelist, *, offline=False, only=None):
    from .eligibility import reading_classify as classify
    from .library import note_for_id
    from .vault import render_paper
    service = JournalService(c, env, store)
    result = {'updated':[], 'unchanged':[], 'failures':[], 'model_calls':0, 'translation_calls':0}
    try:
        for p in store.papers():
            if only and p['id'] not in only:continue
            if p.get('venue_kind') not in ('journal','conference'):continue
            gate = classify(p, whitelist)
            try:
                if p.get('venue_kind') == 'journal':service.annotate(p, gate, offline=offline)
                path = existing_note(c,store,p['id'])
                if path:
                    changed = patch_note(path,p,gate,c,store)
                elif p.get('venue_kind') == 'journal':
                    path = render_paper(p,gate,c,store)
                    changed = True
                else:continue
                result['updated' if changed else 'unchanged'].append({'id':p['id'],'note':str(path)})
            except Exception as exc:
                result['failures'].append({'id':p['id'],'error':safe_error(exc)})
    finally:service.close()
    return result


def existing_note(c,store,pid):
    from .library import note_for_id
    formal=note_for_id(c,store,pid,formal=True)
    candidate=note_for_id(c,store,pid)
    if formal and candidate:
        raise ValueError('同一稳定ID存在正式和候选两份笔记；拒绝覆盖')
    return formal or candidate
