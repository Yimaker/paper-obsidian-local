from __future__ import annotations

import json
import copy
from pathlib import Path

from .analysis import AnalysisError, Model, parse_pdf, translate_pdf, analysis_version
from .core import ROOT, Store, atomic_write, digest, now, run_lock, safe_error, today
from .eligibility import reading_classify as classify, load_whitelist
from .sources import HTTP, discover, query_signature, relevance, window
from .vault import FOLDERS, check_links, render_brief, render_paper
from .journals import JournalService
from .capacity import dormant, apply as apply_capacity, guarded


@guarded
def process(c, env, store, http, whitelist, *, retry=False, metadata_only=False, demo=False):
    apply_capacity(c,store)
    model = Model(c, env, store)
    papers = sorted(store.papers(), key=lambda p: (relevance(p, c), p.get("date", "")), reverse=True)
    selected = c.get("scope", {}).get("paper_ids")
    if selected:
        papers = [p for p in papers if p["id"] in selected]
    formal, failures = [], []
    journals = JournalService(c,env,store,http=http)
    for rank, p in enumerate(papers):
        if (c["paths"]["data"] / "STOP").exists():
            break
        pid, gate = p["id"], classify(p, whitelist)
        if dormant(p,c,store): continue
        if p.get('venue_kind') == 'journal':
            try: journals.annotate(p,gate,offline=demo)
            except Exception as exc:
                store.record(pid,'journal_info','unavailable','pending',{'name':p.get('venue','待核验'),'issns':p.get('issns',[]),'metrics':[], 'issues':['查询异常：'+safe_error(exc)]})
        can_analyze = gate["eligible"] or c["model"].get("summarize_candidates", False)
        analyze_rank = sum(1 for prior in papers[:rank] if classify(prior, whitelist)["eligible"] or c["model"].get("summarize_candidates", False))
        version = analysis_version(p, c, env)
        try:
            if can_analyze and not metadata_only and analyze_rank < c["limits"]["brief_per_day"]:
                # Fulltext is one concise request. Do not pay for a duplicate abstract
                # before it, and do not turn a malformed paid result into a second call.
                # At most two new full-text attempts a day, with persistent quotas across reruns.
                full = store.stage(pid, "fulltext")
                needs_full = not full or full["version"] != version or (retry and full["status"] != "success")
                if needs_full and store.spend("fulltext", c["limits"]["fulltext_per_day"]):
                    pdf = store.stage(pid, "pdf")
                    try:
                        if pdf and pdf["status"] == "success" and Path(json.loads(pdf["detail"])["path"]).exists():
                            source_path = Path(json.loads(pdf["detail"])["path"])
                        elif p.get("pdf_url"):
                            if c.get("scope", {}).get("allow_download") is False:
                                raise FileNotFoundError("本次范围禁止下载；必须有本地 PDF")
                            raw = http.get(p["pdf_url"], binary=True)
                            source_path = c["paths"]["data"] / "pdf" / (pid + "-" + digest(raw)[:12] + ".pdf")
                            atomic_write(source_path, raw)
                            store.record(pid, "pdf", version, "success", {"path": str(source_path)})
                        else:
                            raise FileNotFoundError("没有可访问的开放获取 PDF")
                        parsed = parse_pdf(source_path, c)
                        atomic_write(c["paths"]["data"] / "extracted" / (pid + ".json"), json.dumps(parsed, ensure_ascii=False))
                        status, detail = model.summarize(p, parsed)
                        store.record(pid, "fulltext", version, status, detail)
                    except Exception as exc:
                        if not store.stage(pid, "pdf") or store.stage(pid, "pdf")["status"] != "success":
                            store.record(pid, "pdf", version, "failed", {"error": safe_error(exc), "message": "未获取有效开放 PDF"})
                        store.record(pid, "fulltext", version, "failed", {"error": safe_error(exc), "message": str(exc) if isinstance(exc, AnalysisError) else "PDF 下载/解析失败或超限；降级摘要，未声称读过全文"})
                        if not store.stage(pid, "pdf") or store.stage(pid, "pdf")["status"] != "success":
                            status, detail = model.summarize(p)
                            store.record(pid, "abstract", version, status, detail)
                    pdf = store.stage(pid, "pdf")
                    if pdf and pdf["status"] == "success":
                        status, detail = translate_pdf(json.loads(pdf["detail"])["path"], c, env, store)
                        store.record(pid, "translation", version, status, detail)
                # An earlier PDF may be available even when no new full summary is requested.
                pdf = store.stage(pid, "pdf")
                tr = store.stage(pid, "translation")
                if c["translation"]["enabled"] and pdf and pdf["status"] == "success" and (not tr or (retry and tr["status"] not in ("success", "failed", "daily_limit"))):
                    status, detail = translate_pdf(json.loads(pdf["detail"])["path"], c, env, store)
                    store.record(pid, "translation", version, status, detail)
            note = render_paper(p, gate, c, store, demo=demo)
            if gate["eligible"] and note.parent.name == "10_论文笔记":
                formal.append((p, gate, note))
        except Exception as exc:
            store.record(pid, "note", version, "failed", {"error": safe_error(exc), "message": "笔记写入失败；原文件保留，参见 data/backups"})
            failures.append({"paper_id": pid, "stage": "note", "error": safe_error(exc)})
    journals.close()
    return formal, failures


def run(c, env, *, retry=False, metadata_only=False, fetch=True):
    if fetch and c.get("scope", {}).get("allow_fetch") is False:
        raise ValueError("该配置禁止重新检索，请使用 summarize-one 或 --no-fetch")
    data = c["paths"]["data"]
    with run_lock(data):
        (data / "STOP").unlink(missing_ok=True)
        store = Store(data)
        with store.db:
            rid = store.db.execute("INSERT INTO runs(started) VALUES (?)", (now(),)).lastrowid
        whitelist = load_whitelist(c["paths"]["whitelist"])
        http = HTTP(c, env)
        report = {"started": now(), "sources": {}, "failures": [], "truncated": False}
        report["fetch_performed"] = fetch
        if not fetch:
            previous = store.get_state("last_fetch")
            if not previous:
                for row in store.db.execute("SELECT report FROM runs WHERE report IS NOT NULL ORDER BY id DESC"):
                    candidate = json.loads(row[0])
                    if candidate.get("sources"):
                        previous = candidate
                        break
            if previous:
                report.update(sources=previous["sources"], truncated=previous.get("truncated", False),
                              source_status_from=previous["started"])
        for folder in FOLDERS:
            (c["paths"]["vault"] / folder).mkdir(parents=True, exist_ok=True)
        if fetch:
            for name in c["search"]["sources"]:
                if (data / "STOP").exists():
                    report["stopped"] = True
                    break
                start, end, full, state = window(c, store, name)
                key = query_signature(c, name)
                try:
                    sc = copy.deepcopy(c)
                    offset = state.get("resume", {}).get("page", 0)
                    sc["search"]["_page_start"] = offset
                    papers, truncated = discover(name, sc, http, start, end, whitelist.get("openreview_profiles", []))
                    accepted, cap_reached = 0, False
                    # Cache/replay the complete response set for failure resumption within the daily cap.
                    batchfile = data / "batches" / (digest([key, start, end]) + ".json")
                    atomic_write(batchfile, json.dumps(papers, ensure_ascii=False))
                    for p in papers:
                        if relevance(p, c):
                            pid = store.ingest(p, c["limits"]["candidates_per_day"])
                            accepted += pid is not None
                            cap_reached |= pid is None
                    report["sources"][name] = {"status": "success", "returned": len(papers), "processed": accepted,
                        "window": [start, end], "truncated": truncated, "daily_cap_reached": cap_reached}
                    report["truncated"] |= truncated or cap_reached
                    # Do not advance a watermark past an unconsumed/truncated result window.
                    if not truncated and not cap_reached:
                        state.pop("resume", None)
                        state["last_success"] = end
                        if full:
                            state["last_backfill"] = end
                        store.set_state(key, state)
                    else:
                        state["resume"] = {"start": start, "end": end, "full": full,
                                           "page": offset if cap_reached else offset + c["search"]["max_pages"]}
                        store.set_state(key, state)
                except Exception as exc:
                    report["sources"][name] = {"status": "failed", "error": safe_error(exc), "message": str(exc) if type(exc).__name__ == "FetchError" else "响应解析失败", "window": [start, end]}
            store.set_state("last_fetch", {"started": report["started"], "sources": report["sources"], "truncated": report["truncated"]})
        formal, failures = process(c, env, store, http, whitelist, retry=retry, metadata_only=metadata_only)
        report["failures"] = failures
        report["formal"] = len(formal)
        report["papers"] = len(store.papers())
        report["processing_status"] = [dict(r) for r in store.db.execute("SELECT stage,status,count(*) AS n FROM stages GROUP BY stage,status")]
        report["ended"] = now()
        if c.get("scope", {}).get("write_brief", True):
            brief = render_brief(formal, report, c)
            report["brief"] = str(brief)
        report["links"] = check_links(c["paths"]["vault"])
        store.set_state("last_run", report)
        with store.db:
            store.db.execute("UPDATE runs SET ended=?,report=? WHERE id=?", (now(), json.dumps(report, ensure_ascii=False), rid))
        atomic_write(data / "last-run.json", json.dumps(report, ensure_ascii=False, indent=2))
        log = data / "logs" / (today() + ".jsonl")
        log.parent.mkdir(exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(report, ensure_ascii=False) + "\n")
        store.db.close()
        http.client.close()
        return report
