"""Evidence is supplied by official data or audited local imports, never by an LLM."""
import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from .core import atomic_write, digest, local_path, now


def official(url, suffixes):
    p = urlparse(url or "")
    return p.scheme == "https" and any((p.hostname or "") == d or (p.hostname or "").endswith("." + d) for d in suffixes)


def stamp(value):
    try:
        d = datetime.fromisoformat(value)
        return d.tzinfo is not None and d <= datetime.now(d.tzinfo)
    except (ValueError, TypeError):
        return False


def load_whitelist(path):
    d = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if d.get("test_only"):
        raise ValueError("测试名单禁止用于正式运行")
    for r in d.get("journals", []):
        # Changed/missing evidence becomes pending per journal, not a global outage.
        if r.get('qualification_schema') not in (2,3):
            validate_journal(r)
    for r in d.get("conferences", []):
        validate_conference(r)
    d["_jcr_review_root"] = str(Path(path).parent / "jcr-paper-reviews")
    return d


def validate_conference(r):
    if r.get("grade") != "A" or not r.get("ccf_year") or not official(r.get("source"), ["ccf.org.cn"]) or not stamp(r.get("checked_at")):
        raise ValueError("会议名单必须包含 CCF A、目录年份、官方来源和带时区核验时间")
    if not r.get("acronym"):
        raise ValueError("会议缺少规范名称")


def validate_journal(r):
    if r.get('qualification_schema') == 3:
        from .jcr import validate
        return validate(r)
    if r.get('qualification_schema') == 2:
        return validate_dual_record(r)
    if not r.get("issns") or r.get("sci_index") != "SCIE" or r.get("cas_major_zone") not in (1, 2):
        raise ValueError("期刊必须按 ISSN 列出，分别有 SCIE 与中科院大类 1/2 区依据")
    if r.get("cas_partition_scope") not in ("major", "大类") or not r.get("cas_edition") or not r.get("cas_major_category"):
        raise ValueError("中科院证据须明确大类口径、版本和大类名称；JCR或小类不能替代")
    for prefix, domains in (("sci", ["clarivate.com"]), ("cas", ["fenqubiao.com", "las.ac.cn", "las.cas.cn"])):
        if not r.get(prefix + "_year") or not official(r.get(prefix + "_source"), domains) or not stamp(r.get(prefix + "_checked_at")):
            raise ValueError("期刊缺少独立的 SCI/中科院年份、官方来源或核验时间")
        if not str(r[prefix+"_year"]).isdigit() or not 1900 <= int(r[prefix+"_year"]) <= date.today().year:
            raise ValueError("期刊证据年份无效或在未来")
    if not r.get("coverage_from") or not r.get("coverage_to"):
        raise ValueError("期刊需明确本条核验证据适用的文章发表日期区间")
    date.fromisoformat(r["coverage_from"])
    date.fromisoformat(r["coverage_to"])
    if r["coverage_from"] > r["coverage_to"]:
        raise ValueError("证据覆盖日期倒置")
    if not r.get("sci_evidence_file") or not r.get("cas_evidence_file"):
        raise ValueError("SCI收录与中科院分区须分别保存可审计的本地证据文件")
    if date.fromisoformat(r["coverage_to"]).year > int(r["cas_year"]):
        if not official(r.get("coverage_policy_source"), ["edu.cn", "ac.cn", "cas.cn"]) or not r.get("coverage_policy_evidence_file"):
            raise ValueError("将历史中科院分区用于较新论文须有学校/机构的正式适用口径及本地证据")
    for i in r["issns"]:
        digits = i.replace("-", "").upper()
        if not re.fullmatch(r"\d{7}[\dX]", digits) or (sum(int(x)*(8-n) for n,x in enumerate(digits[:7])) + (10 if digits[-1]=="X" else int(digits[-1]))) % 11:
            raise ValueError("ISSN 格式错误")


def legacy_classify(p, whitelist):
    pub = p.get("publication", {})
    status = pub.get("status", "unknown")
    result = {"eligible": False, "reason": "发表信息或等级依据不足，待核验", "publication_status": status, "evidence": []}
    if status not in ("accepted", "published"):
        result["reason"] = "预印本/投稿/未知状态：未核验正式发表或录用"
        return result
    if p.get("venue_kind") == "journal":
        for r in whitelist.get("journals", []):
            if r.get('qualification_schema') in (2,3):
                continue
            if not set(p.get("issns", [])) & set(r["issns"]):
                continue
            if status != "published" or not p.get("doi") or not (r["coverage_from"] <= p.get("date", "") <= r["coverage_to"]):
                result["reason"] = "期刊匹配，但 DOI、正式发表状态或证据覆盖日期不足"
                continue
            validate_journal(r)
            result.update(eligible=True, reason="ISSN 匹配；SCIE 与中科院大类分区分别有依据", evidence=[r, pub])
            return result
    if p.get("venue_kind") == "conference":
        for r in whitelist.get("conferences", []):
            if p.get("venue", "").casefold() != r["acronym"].casefold():
                continue
            validate_conference(r)
            if pub.get("main_track") is not True or pub.get("paper_type") not in ("full", "regular"):
                result["reason"] = "A 类会议但主会长文/Regular paper 身份尚未核验"
                continue
            if not official(pub.get("source"), ["openreview.net", "neurips.cc", "nips.cc", "mlr.press", "thecvf.com", "aclanthology.org", "aaai.org", "ijcai.org"]):
                result["reason"] = "缺少官方录用/论文集证据"
                continue
            if not stamp(pub.get("checked_at")):
                continue
            result.update(eligible=True, reason="CCF A + 已核验主会正式长文录用/发表", evidence=[r, pub])
            return result
    return result


def import_csv(kind, source, target):
    d = load_whitelist(target)
    rows = []
    with open(source, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            r = {k: v for k, v in row.items() if v != ""}
            if kind == "journals":
                if r.get('qualification_schema') == '2':
                    r['qualification_schema'] = 2
                    if r.get('user_reviewed') not in ('true','false'):
                        raise ValueError('user_reviewed须明确true/false')
                    r['user_reviewed'] = r['user_reviewed'] == 'true'
                    if r.get('verified_names'):
                        r['verified_names'] = json.loads(r['verified_names'])
                r["issns"] = [x.strip() for x in r["issns"].split(";")]
                if r.get('qualification_schema') == '3':
                    r['qualification_schema']=3
                    for k in ('categories','verified_names'):
                        if k in r:r[k]=json.loads(r[k])
                    for k in ('user_reviewed','all_categories_verified'):r[k]={'true':True,'false':False}.get(r.get(k),r.get(k))
                    for k in ('metric_year','jcr_year'):r[k]=int(r[k])
                    from .jcr import artifact
                    r['evidence_file']=str(local_path(r['evidence_file'],Path(source).parent).resolve())
                    artifact(r)
                else:r["cas_major_zone"] = int(r["cas_major_zone"])
                for prefix in ("sci", "cas", "coverage_policy"):
                    field = prefix + "_evidence_file"
                    if r.get(field):
                        evidence = local_path(r[field], Path(source).parent).resolve()
                        if not evidence.is_file():
                            raise ValueError("期刊导入所指本地证据不存在；整批拒绝导入")
                        r[field] = str(evidence)
                        r[prefix+"_evidence_sha256"] = digest(evidence.read_bytes())
                validate_journal(r)
            else:
                validate_conference(r)
            rows.append(r)
    # Validate all rows before replacing anything; backup audit files locally.
    def key(r):
        return digest(r) if kind == 'journals' else r['acronym']
    by_key = {key(r): r for r in d.get(kind, [])}
    for r in rows:
        by_key[key(r)] = r
    d[kind] = list(by_key.values())
    atomic_write(target, json.dumps(d, ensure_ascii=False, indent=2) + "\n", Path(target).parent / "backups")
    return len(rows)


def validate_dual_record(r):
    """Validate a reviewed evidence assertion, never infer semantics from a domain."""
    if r.get('scope') not in ('current', 'historical') or not r.get('name') or not r.get('issns'):
        raise ValueError('缺少期刊身份或双口径')
    for issn in r['issns']:
        v=issn.replace('-', '').upper()
        if not re.fullmatch(r'\d{7}[\dX]',v) or (sum(int(x)*(8-i) for i,x in enumerate(v[:7]))+(10 if v[-1]=='X' else int(v[-1])))%11:
            raise ValueError('ISSN格式错误')
    if r.get('identity_review') != 'exact' or r.get('conflict'):
        raise ValueError('身份或证据冲突待核验')
    for prefix, domains in [('sci',['clarivate.com']),('cas',['fenqubiao.com','las.ac.cn','las.cas.cn'])]:
        if not official(r.get(prefix+'_source'),domains) or not stamp(r.get(prefix+'_checked_at')):
            raise ValueError('缺少官方来源或实际查询时间')
        f=local_path(r.get(prefix+'_evidence_file',''))
        if not f.is_file() or digest(f.read_bytes()) != r.get(prefix+'_evidence_sha256'):
            raise ValueError('原始证据缺失或哈希不符')
        if not r.get(prefix+'_semantic_support') or r.get('reviewer_type') not in ('codex','user','expert'):
            raise ValueError('必须记录逐字段语义核对及真实审校者')
    if r.get('user_reviewed') not in (True,False) or (r.get('reviewer_type')=='codex' and r.get('user_reviewed') is not False):
        raise ValueError('Codex不能冒充人工审校')
    if r.get('sci_index') not in ('SCIE','not_SCIE') or not r.get('sci_as_of'):
        raise ValueError('SCIE状态不明')
    date.fromisoformat(r['sci_as_of'])
    if r['sci_as_of'] > now()[:10]:
        raise ValueError('SCIE状态日期不可在未来')
    if r.get('cas_partition_scope')!='major' or r.get('cas_major_zone') not in (1,2,3,4) or not all(r.get(k) for k in ('cas_edition','cas_year','cas_major_category')):
        raise ValueError('缺少中科院大类版本；JIF/JCR不能替代')
    if not str(r['cas_year']).isdigit() or not 1900 <= int(r['cas_year']) <= int(now()[:4]):
        raise ValueError('版本年份无效')
    if r['scope']=='current':
        if not r.get('latest_available_basis') or not r.get('review_due'):
            raise ValueError('缺少最新可取得版本依据或复核期限')
        date.fromisoformat(r['review_due'])
    else:
        if not r.get('applicability_rule'):
            raise ValueError('历史适用规则不明，不能按自然年推定')
        for prefix in ('sci','cas'):
            a,b=r.get(prefix+'_coverage_from',''),r.get(prefix+'_coverage_to','')
            date.fromisoformat(a);date.fromisoformat(b)
            if a>b: raise ValueError('历史覆盖区间倒置')
        f=local_path(r.get('coverage_policy_evidence_file',''))
        if not f.is_file() or digest(f.read_bytes())!=r.get('coverage_policy_evidence_sha256') or not official(r.get('coverage_policy_source'),['edu.cn','ac.cn','cas.cn','clarivate.com','fenqubiao.com']):
            raise ValueError('历史适用规则须有可审计的原始依据')


def classify(p, whitelist):
    """eligible retains the legacy historical/conference meaning."""
    legacy=legacy_classify(p,whitelist)
    pending=lambda: {'status':'pending','reason':'缺少充分证据','evidence':[]}
    current,historical=pending(),pending()
    if p.get('venue_kind')=='journal':
        if legacy['eligible']:
            historical={'status':'passed','reason':legacy['reason'],'evidence':legacy['evidence']}
        for scope in ('current','historical'):
            matches=[r for r in whitelist.get('journals',[]) if r.get('qualification_schema')==2 and r.get('scope')==scope and set(p.get('issns',[])) & set(r.get('issns',[]))]
            # A newer incomplete/expired assertion must not revive an older current pass.
            if scope=='current' and matches:
                def version(r):
                    year=str(r.get('cas_year',''))
                    # Malformed versions sort last and are rejected below, fail closed.
                    return (int(year) if year.isdigit() else 99999, r.get('sci_as_of',''))
                newest=max(version(r) for r in matches)
                matches=[r for r in matches if version(r)==newest]
            outcomes=[];problems=[]
            for r in matches:
                try:
                    validate_dual_record(r)
                    if not set(p.get('issns',[])) <= set(r['issns']) or p.get('venue','').casefold() not in [n.casefold() for n in [r['name']]+r.get('verified_names',[])]:
                        raise ValueError('ISSN集合或刊名不一致；更名不能自动合并')
                    if scope=='current' and (r['review_due'] < now()[:10] or r.get('source_status') in ('changed','refresh_failed')):
                        raise ValueError('证据过期或来源状态变化，待复核')
                    if scope=='historical' and not all(r[k+'_coverage_from'] <= p.get('date','') <= r[k+'_coverage_to'] for k in ('sci','cas')):
                        continue
                    outcomes.append(('passed' if r['sci_index']=='SCIE' and r['cas_major_zone'] in (1,2) else 'failed',r))
                except (ValueError,TypeError,KeyError,OSError) as e:
                    problems.append(str(e))
            if problems or len({x[0] for x in outcomes})>1 or len({(x[1]['cas_edition'],x[1]['cas_major_category'],x[1]['cas_major_zone']) for x in outcomes})>1:
                value={'status':'pending','reason':'；'.join(problems) or '证据冲突','evidence':matches}
            elif outcomes:
                value={'status':outcomes[0][0],'reason':'独立官方SCIE与中科院大类证据已逐字段核对','evidence':[x[1] for x in outcomes]}
            else: continue
            if scope=='current':current=value
            else:historical=value
    publication_ok=p.get('publication',{}).get('status')=='published' and bool(p.get('doi'))
    basis=[]
    if p.get('venue_kind')=='conference' and legacy['eligible']:basis=['ccf_a']
    if p.get('venue_kind')=='journal' and publication_ok:
        if current['status']=='passed':basis.append('journal_current')
        if historical['status']=='passed':basis.append('journal_historical')
    from .jcr import classify as jcr_classify
    jcr=jcr_classify(p,whitelist)
    if jcr['status']=='passed':basis.append('journal_jcr')
    legacy.update(jcr_qualification=jcr,fulltext_verification_allowed=jcr['fulltext_verification_allowed'],current_qualification=current,historical_qualification=historical,admission_basis=basis,reading_eligible=bool(basis),qualification_checked_at=now())
    return legacy


def reading_classify(p,whitelist):
    """Explicit admission adapter for all reading consumers; legacy value retained."""
    result=classify(p,whitelist)
    result['legacy_eligible']=result['eligible']
    result['eligible']=result['reading_eligible']
    labels={'journal_jcr':'按 JCR 阅读资格推荐','journal_current':'按当前期刊资格推荐','journal_historical':'发表时资格已核验','ccf_a':'CCF A主会长文通道'}
    if result['admission_basis']:
        result['reason']='；'.join(labels[x] for x in result['admission_basis'])
        if 'journal_current' in result['admission_basis'] and 'journal_historical' not in result['admission_basis']:
            result['reason']+='；发表时资格待核验，不以当前记录回填历史'
        if 'journal_jcr' in result['admission_basis']:
            from .jcr import label
            result['reason'] += '；'+label(result['jcr_qualification'])+'；不声明发表当年资格'
        if not result['evidence']:
            result['evidence']=result['current_qualification']['evidence']+result['historical_qualification']['evidence']+result['jcr_qualification']['evidence']
    return result


def delivery_qualification_counts(rows):
    groups={k:set() for k in ('current','historical','jcr','jcr_unrecorded','ccf_a','unknown','total')}
    for p,d in rows:
        pid=p['id'];groups['total'].add(pid)
        q=d.get('qualification',{});basis=q.get('admission_basis',[])
        for k,b in [('current','journal_current'),('historical','journal_historical'),('ccf_a','ccf_a'),('jcr','journal_jcr')]:
            if b in basis:groups[k].add(pid)
        if 'jcr_qualification' not in q:groups['jcr_unrecorded'].add(pid)
        if not basis:groups['unknown'].add(pid)
    return {**{k:len(v) for k,v in groups.items()},'intersection':len(groups['current']&groups['historical'])}
