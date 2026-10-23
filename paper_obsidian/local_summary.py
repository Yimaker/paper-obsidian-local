"""Resume one existing local paper. No discovery, PDF download or automatic retry."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import httpx

from .analysis import Model, PROMPT, analysis_version, model_key, parse_pdf, request_data, validate_summary, decode_editorial_json
from .core import ROOT, Store, atomic_write, digest, load_config, local_path, now, run_lock, secrets
from .eligibility import reading_classify as classify, load_whitelist
from .evidence import approve_review, publishable, content_payload
from .recent import snapshot
from .single import manual_parts
from .vault import check_links, render_paper
from .library import note_for_id


def run_local(c, env, pid, out, mode, *, review_file=None):
    if mode not in ("preflight", "request", "review", "replay"):
        raise ValueError("未知操作")
    if c["translation"]["enabled"] or c["model"].get("thinking") != "disabled" or c["limits"]["max_output_tokens"] != 6000:
        raise ValueError("本次必须沿用非思考与6000tokens上限，禁用翻译")
    out = Path(out); out.mkdir(parents=True, exist_ok=True)
    with run_lock(c["paths"]["data"]):
        s = Store(c["paths"]["data"])
        try:
            p = s.get(pid)
            if not p: raise ValueError("论文不在现有数据库")
            gate = classify(p, load_whitelist(c["paths"]["whitelist"]))
            if not gate["eligible"]: raise ValueError("既有发表资格未通过")
            rec = s.stage(pid, "pdf")
            if not rec or rec["status"] != "success": raise ValueError("没有已保存PDF")
            pdf = local_path(json.loads(rec["detail"])["path"])
            parsed = parse_pdf(pdf, c)
            version = analysis_version(p,c,env)
            key = model_key(p,parsed,c,env)
            before = snapshot(c["paths"]["vault"])
            report = {"paper_id":pid,"mode":mode,"started":now(),"request_key":key,"stage_version":version,
                      "pdf":str(pdf),"pdf_sha256":digest(pdf.read_bytes()),"page_count":parsed["page_count"],
                      "raw_layout_chars":parsed["raw_layout_chars"],"compacted_chars":len(parsed["text"]),
                      "evidence_text_chars":len(parsed["evidence_text"]),"input_chars_with_system":len(json.dumps(request_data(p,parsed),ensure_ascii=False))+len(PROMPT),
                      "input_limit":c["limits"]["input_chars_per_call"],"output_limit":6000,"thinking":"disabled",
                      "model":env.get("MODEL"),"catalog_sha256":parsed["catalog_sha256"],"publication_pages":parsed["publication_pages"],
                      "eligibility":gate,"model_requests":0,"fetch_requests":0,"downloads":0,"translations":0}
            if not (out/"before.json").exists():
                atomic_write(out/"before.json", json.dumps(before,ensure_ascii=False,indent=2))
                backup = sqlite3.connect(out/"production-before.sqlite3")
                s.db.backup(backup); backup.close()
            atomic_write(out/"original-with-evidence.json",json.dumps(parsed,ensure_ascii=False,indent=2))
            cache = c["paths"]["data"] / "model-cache" / (key+".json")
            if mode == "preflight":
                report["status"] = "ready"
            elif mode == "request":
                # This run authorizes one new request in total, even if the code,
                # prompt or configuration changes again after the reservation.
                reservation = out/"one-request-reservation.json"
                if reservation.exists():
                    report["status"] = "run_request_already_reserved"
                else:
                    atomic_write(reservation,json.dumps({"paper_id":pid,"request_key":key,"started":now(),"maximum_new_requests":1}))
                    model = Model(c,env,s)
                    status,result = model.summarize(p,parsed)
                    s.record(pid,"fulltext",version,status,result)
                    report.update(status=status,model_requests=model.network_attempts,receipt=model.receipt,cache_hit=model.cache_hit)
                    atomic_write(out/"generated.json",json.dumps(result,ensure_ascii=False,indent=2))
            elif mode == "review":
                if not review_file: raise ValueError("缺少校对记录")
                if cache.exists():
                    generated = json.loads(cache.read_text(encoding="utf-8"))
                else:
                    raw_path = c["paths"]["data"] / "model-responses" / (key+".json")
                    raw = json.loads(raw_path.read_text(encoding="utf-8"))
                    receipt = raw["receipt"]
                    if (receipt.get("finish_reason") != "stop" or receipt.get("request_key") != key or
                            receipt.get("input_sha256") != digest(request_data(p,parsed))):
                        raise ValueError("原始响应没有正常结束或输入指纹不符")
                    generated, repairs = decode_editorial_json(raw["content"])
                    atomic_write(out/"model-decoded.json",json.dumps(generated,ensure_ascii=False,indent=2))
                    generated.update(receipt=receipt,usage=receipt["usage"],origin="model_api",model=env["MODEL"],
                                     basis="全文文本层；关键页经原文复核",evidence_catalog=parsed["evidence_catalog"],
                                     publication_pages=parsed["publication_pages"],format_repairs=repairs)
                if generated.get("receipt",{}).get("finish_reason") != "stop": raise ValueError("截断响应不能发布")
                review = json.loads(Path(review_file).read_text(encoding="utf-8"))
                result = json.loads((out/"corrected.json").read_text(encoding="utf-8")) if (out/"corrected.json").exists() else generated
                # Keep the actual response, receipt and evidence independent of editorial text.
                for field in ("receipt","usage","origin","model","basis","evidence_catalog","publication_pages","format_repairs"):
                    result[field] = generated.get(field, [] if field == "format_repairs" else None)
                if review.get("original_content_sha256") != digest(content_payload(generated)):
                    raise ValueError("校对记录与模型原始缓存不一致")
                result = approve_review(result,parsed,review)
                result["editorial_review"] = {"status":"经Codex原文校对，用户未复核" if review.get("reviewer_type") == "codex" and review.get("user_reviewed") is False else "经校对通过", "reviewer_type":review.get("reviewer_type"), "user_reviewed":review.get("user_reviewed",False), "notes":review.get("notes",[]), "terms":"原文术语保留；证据按PDF物理页定位"}
                atomic_write(out/"approved.json",json.dumps(result,ensure_ascii=False,indent=2))
                atomic_write(cache,json.dumps(result,ensure_ascii=False,indent=2),out/"cache-before-review")
                s.record(pid,"fulltext",version,"success",result)
                note = render_paper(p,gate,c,s)
                report.update(status="success",note=str(note),review_status="经校对通过",receipt=result["receipt"])
            else:
                # A transport guard makes the zero-network assertion executable.
                def no_network(request): raise AssertionError("缓存重放禁止网络")
                model = Model(c,env,s,httpx.MockTransport(no_network))
                status,result = model.summarize(p,parsed)
                if status != "success" or not model.cache_hit or not publishable(result): raise ValueError("没有可发布的同版本成功缓存")
                s.record(pid,"fulltext",version,status,result)
                note = render_paper(p,gate,c,s)
                report.update(status=status,model_requests=model.network_attempts,cache_hit=model.cache_hit,note=str(note),receipt=result["receipt"])
            after = snapshot(c["paths"]["vault"])
            report.update(links=check_links(c["paths"]["vault"]),
                          changed_this_run=[k for k in before if k in after and before[k]!=after[k]],
                          new_this_run=[k for k in after if k not in before], removed_this_run=[k for k in before if k not in after],
                          note_files_for_id=[str(f) for f in (note_for_id(c,s,pid,True),note_for_id(c,s,pid)) if f],
                          finished=now())
            atomic_write(out/(mode+"-report.json"),json.dumps(report,ensure_ascii=False,indent=2))
            return report
        finally: s.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["preflight","request","review","replay"])
    parser.add_argument("--paper-id",required=True)
    parser.add_argument("--out",required=True)
    parser.add_argument("--review-file")
    args = parser.parse_args()
    print(json.dumps(run_local(load_config(),secrets(),args.paper_id,local_path(args.out),args.mode,review_file=local_path(args.review_file) if args.review_file else None),ensure_ascii=False,indent=2))
