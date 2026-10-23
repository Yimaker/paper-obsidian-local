"""One existing paper, one bounded model request, local PDF only, no daily recommendation."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from .analysis import Model, PROMPT, parse_pdf, validate_summary, decode_model_json, analysis_version, request_data, model_key
from .core import Store, atomic_write, digest, load_config, now, run_lock
from .eligibility import reading_classify as classify, load_whitelist
from .vault import END, START, check_links, render_paper
from .evidence import publishable
from .library import note_for_id, portable


def manual_parts(path):
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8")
    if raw.count(START) != 1 or raw.count(END) != 1:
        raise ValueError("原笔记托管标记不完整，拒绝覆盖")
    front, after = raw.split(START, 1)
    tail = after.split(END, 1)[1]
    # Preserve all text outside the automatic body, excluding owned YAML metadata.
    if front.startswith("---\n"):
        front = front.split("---\n", 2)[2]
    return digest([front, tail])


def summarize_one(c, env, *, model_factory=Model, allow_extra_call=False, recover_saved_response=False):
    a = c.get("acceptance", {})
    pid = a.get("paper_id")
    if not pid or c.get("scope", {}).get("paper_ids") != [pid] or a.get("max_calls_per_run") != 1:
        raise ValueError("需要明确的单论文 scope 与 1 次请求上限")
    if c["limits"]["model_calls_per_day"] != 1 or c["limits"]["max_output_tokens"] > 6000:
        raise ValueError("单篇验收限额不正确")
    if c["translation"]["enabled"]:
        raise ValueError("本次不允许翻译")
    c = copy.deepcopy(c)
    if allow_extra_call:
        c["limits"]["model_calls_per_day"] = 2
    target = load_config(a["target_config"])
    report_path = c["paths"]["data"] / "single-paper-report.json"
    report = {"paper_id": pid, "started": now(), "network_attempts_this_run": 0,
              "max_calls_per_run": 1, "output_token_limit": c["limits"]["max_output_tokens"],
              "input_char_limit": c["limits"]["input_chars_per_call"], "fetch_performed": False,
              "translation_performed": False, "daily_brief_written": False, "historical_function_test": True}
    report["extra_call_explicitly_authorized"] = allow_extra_call
    report["maximum_total_acceptance_calls"] = 2 if allow_extra_call else 1
    with run_lock(c["paths"]["data"]):
        s = Store(c["paths"]["data"])
        try:
            p = s.get(pid)
            if not p:
                raise ValueError("指定论文不在现有数据库")
            gate = classify(p, load_whitelist(c["paths"]["whitelist"]))
            if not gate["eligible"]:
                raise ValueError("指定论文未通过现有发表核验")
            rec = s.stage(pid, "pdf")
            if not rec or rec["status"] != "success":
                raise ValueError("没有已成功下载的本地 PDF")
            pdf = Path(json.loads(rec["detail"])["path"])
            if not pdf.exists():
                raise FileNotFoundError("已有本地 PDF 不存在，本入口不会重新下载")
            parsed = parse_pdf(pdf, c)
            # The old proceedings web abstract differs from the PDF; omit it from this full-text request.
            request_paper = dict(p, abstract="")
            version = analysis_version(p, c, env)
            report.update(title=p["title"], pdf=str(pdf), pdf_sha256=digest(pdf.read_bytes()),
                          page_count=parsed["page_count"], extracted_chars=len(parsed["text"]), parser_warning=parsed["warning"])
            atomic_write(c["paths"]["data"] / "single-paper-original.json", json.dumps(parsed, ensure_ascii=False, indent=2))
            model = model_factory(c, env, s)
            previous = s.get_state("single-attempt:" + pid)
            previous_count = (previous or {}).get("request_count", (previous or {}).get("network_attempts", int(bool((previous or {}).get("attempted")))))
            receipts = s.get_state("single-receipts:" + pid, [])
            if not receipts and report_path.exists():
                old_report = json.loads(report_path.read_text(encoding="utf-8"))
                if old_report.get("receipt") and old_report.get("network_attempts_this_run"):
                    receipts.append(old_report["receipt"])
                    s.set_state("single-receipts:" + pid, receipts)
            report.update(acceptance_calls_total=previous_count, receipts=receipts)
            report["maximum_total_acceptance_calls"] = max(report["maximum_total_acceptance_calls"], previous_count)
            full = s.stage(pid, "fulltext")
            if full and full["version"] == version and full["status"] == "success":
                result = json.loads(full["detail"])
                validate_summary(result, parsed)
                if result.get("origin") != "model_api":
                    raise ValueError("旧结果没有真实模型来源记录，不予采用")
                status, cache_hit = ("success" if publishable(result) else "review_required"), True
            elif recover_saved_response:
                result = recover_response(c, env, request_paper, parsed, receipts)
                status, cache_hit = ("success" if publishable(result) else "review_required"), True
                report["recovered_offline"] = True
            elif previous_count >= (2 if allow_extra_call else 1):
                report.update(status="previous_attempt_not_retried", message="此前已请求过该输入，未自动重复付费；查看保存的原始模型响应与状态")
                return finish(report, report_path)
            elif not model.configured():
                report.update(status="needs_configuration", configuration={k: "已配置" if env.get(k) else "未配置" for k in ("API_BASE_URL", "API_KEY", "MODEL")})
                return finish(report, report_path)
            else:
                # Persist a conservative attempt marker before sending, so interruption cannot cause an unnoticed second charge.
                s.set_state("single-attempt:" + pid, {"version": version, "attempted": True, "started": now(), "request_count": previous_count + 1})
                status, result = model.summarize(request_paper, parsed)
                cache_hit = model.cache_hit
                report.update(network_attempts_this_run=model.network_attempts, receipt=model.receipt)
                s.record(pid, "fulltext", version, status, result)
                s.set_state("single-attempt:" + pid, {"version": version, "attempted": bool(model.network_attempts), "status": status,
                            "network_attempts": model.network_attempts, "request_count": previous_count + model.network_attempts, "finished": now()})
                if model.receipt:
                    receipts.append(model.receipt)
                    s.set_state("single-receipts:" + pid, receipts)
                report.update(acceptance_calls_total=previous_count + model.network_attempts, receipts=receipts)
            if status != "success":
                report.update(status=status, detail=result)
                with run_lock(target["paths"]["data"]):
                    t = Store(target["paths"]["data"])
                    try:
                        target_p = t.get(pid)
                        old = t.stage(pid, "fulltext")
                        if target_p and (not old or old["status"] != "success"):
                            target_gate = classify(target_p, load_whitelist(target["paths"]["whitelist"]))
                            t.record(pid, "fulltext", version, status, dict(result, receipt=model.receipt))
                            t.record(pid, "abstract", version, "not_requested", {"message": "本轮只请求全文总结，没有另行付费调用摘要"})
                            failed_note = render_paper(target_p, target_gate, target, t)
                            report.update(note=str(failed_note), target_links=check_links(target["paths"]["vault"]))
                    finally:
                        t.db.close()
                return finish(report, report_path)
            if result.get("origin") != "model_api":
                raise ValueError("模型结果缺少来源记录")
            report.update(status="success", cache_hit=cache_hit, usage=result.get("usage"), receipt=result.get("receipt"))
            result["historical_function_test"] = True
            s.record(pid, "fulltext", version, "success", result)
            test_note = render_paper(p, gate, c, s)
            with run_lock(target["paths"]["data"]):
                t = Store(target["paths"]["data"])
                try:
                    target_p = t.get(pid)
                    if not target_p:
                        raise ValueError("目标库缺少该现有论文，禁止扩大论文数量")
                    target_gate = classify(target_p, load_whitelist(target["paths"]["whitelist"]))
                    if not target_gate["eligible"]:
                        raise ValueError("目标库发表核验未通过")
                    note = note_for_id(target,t,pid,True) or note_for_id(target,t,pid)
                    before_manual = manual_parts(note) if note else None
                    pdf_stage=t.stage(pid,'pdf')
                    attachment=portable(json.loads(pdf_stage['detail'])['path']) if pdf_stage and pdf_stage['status']=='success' else None
                    before_pdf = digest(attachment.read_bytes()) if attachment and attachment.exists() else None
                    # Store the result under the ordinary pipeline version as well, so future batch runs reuse it.
                    ordinary_version = analysis_version(target_p, target, env)
                    t.record(pid, "fulltext", ordinary_version, "success", result)
                    t.record(pid, "pdf", ordinary_version, "success", {"path": str(pdf)})
                    final_note = render_paper(target_p, target_gate, target, t)
                    report.update(note=str(final_note), test_note=str(test_note),
                                  manual_preserved=before_manual is None or manual_parts(final_note) == before_manual,
                                  original_pdf_preserved=before_pdf is None or digest(attachment.read_bytes()) == before_pdf,
                                  target_links=check_links(target["paths"]["vault"]), target_paper_count=len(t.papers()))
                finally:
                    t.db.close()
            return finish(report, report_path)
        finally:
            s.db.close()


def finish(report, path):
    report["ended"] = now()
    atomic_write(path, json.dumps(report, ensure_ascii=False, indent=2))
    return report


def recover_response(c, env, paper, parsed, receipts):
    data = request_data(paper, parsed)
    known = {r.get("response_id") for r in receipts if r.get("response_id")}
    matches = []
    for path in (c["paths"]["data"] / "model-responses").glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        r = raw.get("receipt", {})
        if (r.get("origin") == "model_api" and r.get("response_id") in known and r.get("finish_reason") == "stop"
                and r.get("input_sha256") == digest(data)
                and r.get("endpoint_sha256") == digest(env["API_BASE_URL"].rstrip("/"))):
            matches.append((path, raw))
    if len(matches) != 1:
        raise ValueError("未找到唯一、可核对来源的完整模型响应；禁止离线虚构总结")
    path, raw = matches[0]
    result, repairs = decode_model_json(raw["content"])
    validate_summary(result, parsed)
    result.update(origin="model_api", basis="全文文本层（图表未视觉核验）", model=env["MODEL"],
                  receipt=raw["receipt"], usage=raw["receipt"]["usage"], format_repairs=repairs,
                  offline_recovery={"response_file": str(path), "raw_content_sha256": digest(raw["content"]), "additional_requests": 0})
    key = model_key(paper, parsed, c, env)
    atomic_write(c["paths"]["data"] / "model-cache" / (key + ".json"), json.dumps(result, ensure_ascii=False, indent=2))
    return result
