"""One cached recent-paper acceptance batch; never searches or runs translations here.

The source probes save bounded real responses before this entry point is used.
Models run for one explicitly selected verified paper, at most once for this case.
"""
from __future__ import annotations
import copy
import json
import sqlite3
from pathlib import Path

from .analysis import Model, PROMPT, parse_pdf, validate_summary, analysis_version, request_data
from .core import ROOT, Store, atomic_write, digest, load_config, now, run_lock, secrets
from .eligibility import reading_classify as classify, load_whitelist
from .single import manual_parts
from .sources import openalex_paper
from .vault import FOLDERS, check_links, link, managed_write, render_paper

CASE = "recent-2026-10-19"
HISTORICAL = "80c6ca8c01157eb19816"


def case_config():
    c = copy.deepcopy(load_config("config.test.toml"))
    production = load_config()
    c["paths"] = production["paths"]
    c["translation"]["enabled"] = False
    c["limits"]["bilingual_per_day"] = 0
    c["parser"] = {"compact_layout_whitespace":True}
    c["scope"] = {"allow_fetch": False, "allow_download": False, "write_brief": False}
    c.pop("acceptance", None)
    return c


def snapshot(vault):
    result = {}
    for p in sorted(vault.rglob("*")):
        if p.is_file() and ".obsidian" not in p.parts:
            value = {"sha256": digest(p.read_bytes())}
            if p.suffix == ".md":
                value["manual_sha256"] = manual_parts(p)
            result[p.relative_to(vault).as_posix()] = value
    return result


def load_batch(out):
    frozen = out / "batch.json"
    if frozen.exists():
        cached = json.loads(frozen.read_text(encoding="utf-8"))
        if len(cached["rows"]) > 30:
            raise ValueError("冻结批次超过30条，拒绝重放")
        return cached["rows"], cached["discovery"]
    d = json.loads((out / "discovery-90.json").read_text(encoding="utf-8"))
    rows = d["papers"]
    official = json.loads((out / "official-verification.json").read_text(encoding="utf-8"))
    for p in official:
        if "error" in p:
            continue
        p = copy.deepcopy(p)
        p.pop("gate", None)
        p["dates"] = {"official_citation_publication_date": p["date"],
                      "proceedings_year": p["publication"]["proceedings_year"],
                      "official_verified_at": p["publication"]["checked_at"],
                      "date_note": "官方 citation_publication_date 为论文集出版元数据；会议年份不等于出版年月，首次上线及录用日期未取得。"}
        rows.append(p)
    bridge = out / "selected-openalex.json"
    if bridge.exists():
        rows.append(openalex_paper(json.loads(bridge.read_text(encoding="utf-8"))))
    counterpart = out / "moka-arxiv.json"
    if counterpart.exists():
        rows.extend(json.loads(counterpart.read_text(encoding="utf-8")))
    cvpr=out/"cvpr-verification.json"
    if cvpr.exists():
        for p in json.loads(cvpr.read_text(encoding="utf-8")):
            p.pop("gate",None)
            rows.append(p)
    counterpart=out/"clcr-arxiv.json"
    if counterpart.exists():
        rows.extend(json.loads(counterpart.read_text(encoding="utf-8")))
    if len(rows) > 30:
        raise ValueError("本轮累计来源记录超过30；停止，不能只在写入时截断")
    # Metadata only; the arXiv source already enforces the five AI/ML categories.
    for p in rows:
        body = (p["title"] + " " + p.get("abstract", "")).lower()
        if p["source"] == "arxiv":
            p["screening"] = {"keep": True, "reason": "官方 arXiv AI/ML 类别查询命中；仅预印本，仍需正式录用/发表证据"}
        elif p["source"] in ("neurips_proceedings","cvf_proceedings"):
            p["screening"] = {"keep": True, "reason": "官方主会多模态方法论文；本轮仅处理一篇，其余排队"}
        else:
            keep = not any(k in body for k in ("cardiovascular", "suicide prevention", "coding of gender", "8th grade", "radiology", "musculoskeletal", "smart irrigation"))
            p["screening"] = {"keep": keep, "reason": "AI/ML 方法或工程相关元数据" if keep else "本轮聚焦 AI/ML 方法；临床/教育/社会/硬件应用主题不优先"}
    # Ingest weaker metadata first; official publication is authoritative for the selected record.
    rows.sort(key=lambda p: p["source"] in ("neurips_proceedings","cvf_proceedings"))
    atomic_write(frozen,json.dumps({"rows":rows,"discovery":d},ensure_ascii=False,indent=2))
    return rows, d


def preservation(before, after):
    missing = [k for k in before if k not in after]
    changed = [k for k in before if k in after and before[k]["sha256"] != after[k]["sha256"]]
    manual_changed = [k for k in before if k in after and before[k].get("manual_sha256") != after[k].get("manual_sha256")]
    return {"missing_existing_files": missing, "changed_existing_files": changed, "changed_existing_manual_regions": manual_changed,
            "existing_files_unchanged": not missing and not changed, "manual_preserved": not missing and not manual_changed}


def run_recent(*, summarize=False, replay=False):
    if summarize and replay:
        raise ValueError("replay 禁止申请模型请求")
    c, env = case_config(), secrets()
    out = ROOT / "data" / CASE
    ledger = out / "acceptance-report.json"
    with run_lock(c["paths"]["data"]):
        s = Store(c["paths"]["data"])
        try:
            baseline_path = out / "vault-before.json"
            if not baseline_path.exists():
                atomic_write(baseline_path, json.dumps(snapshot(c["paths"]["vault"]), ensure_ascii=False, indent=2))
                backup = sqlite3.connect(out / "production-before.sqlite3")
                s.db.backup(backup); backup.close()
            baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
            backup_db=sqlite3.connect(out/"production-before.sqlite3")
            original_ids={row[0] for row in backup_db.execute("SELECT id FROM papers WHERE merged_into IS NULL")}
            backup_db.close()
            before = snapshot(c["paths"]["vault"])
            usage_before = s.used("model_calls")
            rows, discovery = load_batch(out)
            w = load_whitelist(c["paths"]["whitelist"])
            ids, excluded = [], []
            for p in rows:
                if not p["screening"]["keep"]:
                    excluded.append({"title":p["title"],"reason":p["screening"]["reason"],"aliases":p["aliases"]})
                    continue
                pid = s.ingest(p, load_config()["limits"]["candidates_per_day"])
                if not pid:
                    raise ValueError("生产每日候选上限已满；本轮停止，不扩大原预算")
                ids.append(pid)
            ids = list(dict.fromkeys(ids))
            papers = [s.get(pid) for pid in ids]
            selection_path=out/"selection.json"
            selection=json.loads(selection_path.read_text(encoding="utf-8")) if selection_path.exists() else {}
            chosen = next((p for p in papers if selection.get("alias") in p.get("aliases",[])), None)
            if chosen and chosen["id"] == HISTORICAL:
                raise ValueError("禁止使用历史测试论文")
            report = {"case": CASE, "updated_at": now(), "sources": discovery["sources"],
                      "initial_window": discovery["window"], "expanded_window":["2025-10-20","2026-10-19"],
                      "expansion_reason":"90天受限主题检索未取得可核验正式论文；扩展官方会议论文集至近一年",
                      "retrieved_records": len(rows), "excluded":excluded, "metadata_retained_records":len(rows)-len(excluded),
                      "unique_retained":len(ids), "duplicate_records":len(rows)-len(excluded)-len(ids),
                      "new_unique_in_database":len(set(ids)-original_ids),"matched_existing_papers":len(set(ids)&original_ids),
                      "ccf_version":"2026 第七版，3月31日发布、4月9日勘误；复用并核对本地正式PDF第57页",
                      "journal_whitelist_records":len(w["journals"]), "model_attempts_this_run":0, "translation_performed":False,
                      "fetch_performed":False, "replay":replay}
            for p in papers:
                gate = classify(p,w)
                # Do not let the one-paper trial auto-promote every qualified candidate.
                if not chosen or p["id"] != chosen["id"]:
                    s.record(p["id"],"eligibility",digest(gate),"verified_queued" if gate["eligible"] else "pending",gate)
                    pending_gate=dict(gate,eligible=False,reason=("资格已核验；受本轮一篇处理上限限制，保留候选区，未作正式推荐" if gate["eligible"] else gate["reason"]))
                    # Preserve all old notes byte-for-byte; the seven old candidates are outside this batch.
                    old_candidate="90_候选与待核验/"+p["id"]+".md"
                    if old_candidate not in baseline:
                        render_paper(p,pending_gate,c,s)
            if chosen:
                pid=chosen["id"]; gate=classify(chosen,w)
                if not gate["eligible"] or not ("2025-10-20" <= chosen.get("date","") <= "2026-10-19"):
                    raise ValueError("新论文资格或已核验出版日期不满足范围")
                pdf=out/selection["pdf_file"]
                parsed=parse_pdf(pdf,c)
                report["attempted_paper"]={"paper_id":pid,"title":chosen["title"],"title_zh":"CLCR：面向多模态学习的跨层级语义协同表示" if chosen["title"].startswith("CLCR:") else "",
                    "candidate_note":str(c["paths"]["vault"]/"90_候选与待核验"/(pid+".md")),"pdf":str(pdf),"pdf_sha256":digest(pdf.read_bytes()),"pages":parsed["page_count"],
                    "raw_layout_chars":parsed["raw_layout_chars"],"extracted_chars":len(parsed["text"]),"non_whitespace_preserved":parsed["non_whitespace_preserved"],"parser_warning":parsed["warning"],"dates":chosen.get("dates"),"eligibility":gate,"selection":selection}
                atomic_write(c["paths"]["data"]/"extracted"/(pid+".json"),json.dumps(parsed,ensure_ascii=False,indent=2))
                ordinary_version=analysis_version(chosen,c,env)
                version=digest([CASE,digest(pdf.read_bytes()),parsed["text"],PROMPT,env.get("MODEL"),c["limits"]["max_output_tokens"],c["model"].get("thinking")])
                s.record(pid,"pdf",ordinary_version,"success",{"path":str(pdf),"sha256":digest(pdf.read_bytes())})
                full=s.stage(pid,"fulltext")
                result=None; status="not_requested"
                if full and full["status"]=="success":
                    result=json.loads(full["detail"]); validate_summary(result,parsed)
                    expected=request_data(chosen,parsed)
                    if result.get("receipt",{}).get("input_sha256") != digest(expected):
                        raise ValueError("缓存输入指纹与当前原文不一致；禁止默默复用或重新付费")
                    status="success"; report["cache_hit"]=True
                elif full and not summarize:
                    status=full["status"]; result=json.loads(full["detail"])
                    prior=s.get_state(CASE+":model-attempt",{})
                    report.update(receipt=prior.get("receipt"),usage=(prior.get("receipt") or {}).get("usage"),cache_hit=True)
                elif summarize:
                    attempt=s.get_state(CASE+":model-attempt")
                    if attempt:
                        status="previous_attempt_not_retried"
                    else:
                        if not s.spend("fulltext",load_config()["limits"]["fulltext_per_day"]):
                            raise ValueError("沿用生产配置的全文每日上限，当前已无剩余名额")
                        s.set_state(CASE+":model-attempt",{"paper_id":pid,"input_version":version,"started":now(),"reserved_calls":1})
                        model=Model(c,env,s)
                        # PDF is the source of truth; do not mix a different webpage abstract into the full-text request.
                        status,result=model.summarize(dict(chosen,abstract=""),parsed)
                        report.update(model_attempts_this_run=model.network_attempts,receipt=model.receipt,cache_hit=model.cache_hit)
                        report["usage"]=(model.receipt or {}).get("usage")
                        s.record(pid,"fulltext",ordinary_version,status,result)
                        s.set_state(CASE+":model-attempt",{"paper_id":pid,"input_version":version,"status":status,"attempts":model.network_attempts,"receipt":model.receipt})
                s.record(pid,"abstract",ordinary_version,"not_requested",{"message":"仅对一篇请求全文总结，没有另行调用摘要模型"})
                s.record(pid,"translation",ordinary_version,"disabled",{"message":"本轮明确禁用双语PDF，不调用翻译"})
                if status=="success":
                    if result.get("origin")!="model_api": raise ValueError("缺少真实模型来源")
                    note=render_paper(chosen,gate,c,s)
                    report.update(selected={"paper_id":pid,"title":chosen["title"],"title_zh":result["title_zh"],"note":str(note),"pdf":str(pdf),"pdf_sha256":digest(pdf.read_bytes()),"pages":parsed["page_count"],"extracted_chars":len(parsed["text"]),"eligibility":gate,"dates":chosen.get("dates"),"recommendation":result.get("recommendation",result["summary"])},formal_this_batch=int(note.parent.name=="10_论文笔记"),summary_status="success",usage=result.get("usage"))
                else:
                    render_paper(chosen,dict(gate,eligible=False,reason="资格已核验；全文总结尚未通过，保留候选区"),c,s)
                    report.update(formal_this_batch=0,summary_status=status,summary_detail=result)
            else:
                report.update(formal_this_batch=0,summary_status="no_eligible_fulltext_candidate")
            report["verified_qualified"]=sum(classify(p,w)["eligible"] for p in papers)
            report["unique_all_after_dedup"]=len(ids)+len(excluded)
            report["pending_verification"]=sum(not classify(p,w)["eligible"] for p in papers)
            report["verified_queued"]=report["verified_qualified"]-report["formal_this_batch"]
            report["qualified_summary_failed"]=int(bool(chosen) and report["summary_status"] in ("failed","previous_attempt_not_retried"))
            report["verified_unprocessed"]=report["verified_queued"]-report["qualified_summary_failed"]
            report["model_calls_delta"]=s.used("model_calls")-usage_before
            report["preservation_from_baseline"]=preservation(baseline,snapshot(c["paths"]["vault"]))
            report["links"]=check_links(c["paths"]["vault"])
            if replay:
                report["replay_verification"]=preservation(before,snapshot(c["paths"]["vault"]))
                report["replay_verification"]["model_calls_delta"]=report["model_calls_delta"]
                if report["model_calls_delta"] or report["model_attempts_this_run"]:
                    raise ValueError("重放发生模型调用")
                atomic_write(out/"replay-report.json",json.dumps(report,ensure_ascii=False,indent=2))
            else:
                atomic_write(ledger,json.dumps(report,ensure_ascii=False,indent=2))
            return report
        finally:
            s.db.close()
