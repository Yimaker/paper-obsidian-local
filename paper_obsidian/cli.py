from __future__ import annotations

import argparse
import copy
import importlib.metadata
import json
import os
import platform
import signal
import sys
import tomllib
from pathlib import Path

from .core import ROOT, Store, atomic_write, load_config, local_path, now, run_lock, secrets, today
from .eligibility import reading_classify as classify, import_csv, load_whitelist
from .pipeline import process, run
from .proceedings import verify_neurips
from .sources import HTTP, discover
from .sources import openalex_paper
from .vault import FOLDERS, check_links, render_brief


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def doctor(c, env, network=False):
    data, vault = c["paths"]["data"], c["paths"]["vault"]
    data.mkdir(parents=True, exist_ok=True)
    probe = data / "write-probe.txt"
    atomic_write(probe, "中文路径访问测试")
    file_access = probe.read_text(encoding="utf-8") == "中文路径访问测试"
    probe.unlink()
    result = {"time": now(), "platform": platform.platform(), "hostname": platform.node(),
              "python": sys.version.split()[0], "executable": sys.executable, "cwd": str(Path.cwd()),
              "project": str(ROOT), "vault": str(vault), "vault_exists": vault.exists(),
              "data": str(data), "project_file_access": file_access,
              "model": "已配置" if all(env.get(k) for k in ("API_BASE_URL", "API_KEY", "MODEL")) else "未配置",
              "openalex_key": "已配置" if env.get("OPENALEX_API_KEY") else "未配置",
              "translation": "已配置" if all(env.get(k) for k in ("TRANSLATION_API_BASE_URL", "TRANSLATION_API_KEY", "TRANSLATION_MODEL")) else "未配置",
              "translation_component": "已安装" if local_path(c["translation"]["executable"]).exists() else "未安装",
              "packages": {k: importlib.metadata.version(k) for k in ("httpx", "pypdf", "PyYAML", "python-dotenv")},
              "gui_verification": "未进行；当前工具未提供原生 Obsidian GUI 控制能力",
              "sources": {"openalex": "已实现", "arxiv": "已实现", "openreview": "已实现 API2 公开录用精确核验", "semantic_scholar": "未接入（可选）"}}
    w = load_whitelist(c["paths"]["whitelist"])
    result["whitelist"] = {"conferences": len(w["conferences"]), "journals": len(w["journals"])}
    if network:
        h = HTTP(c, env)
        result["network"] = {}
        small = copy.deepcopy(c)
        small["search"].update(per_source=1, max_pages=1)
        for name in c["search"]["sources"]:
            try:
                papers, truncated = discover(name, small, h, c["search"]["start_date"], c["search"]["end_date"] or today(), w.get("openreview_profiles", []))
                result["network"][name] = {"status": "success", "papers": len(papers), "truncated": truncated}
            except Exception as exc:
                from .core import safe_error
                result["network"][name] = {"status": "failed", "error": safe_error(exc), "message": str(exc) if type(exc).__name__ == "FetchError" else "解析失败"}
        h.client.close()
    atomic_write(data / "doctor.json", json.dumps(result, ensure_ascii=False, indent=2))
    return result


def demo(c):
    # The production whitelist/DB/Vault are never used by demo mode.
    c = copy.deepcopy(c)
    c["paths"].update(data=ROOT / "data/offline-demo", vault=ROOT / "data/离线测试 Vault")
    c["translation"]["enabled"] = False
    for f in FOLDERS:
        (c["paths"]["vault"] / f).mkdir(parents=True, exist_ok=True)
    with run_lock(c["paths"]["data"]):
        st = Store(c["paths"]["data"])
        fixture = {"source": "OFFLINE_TEST_FIXTURE", "title": "[离线测试，非真实论文] Machine Learning Fixture",
                   "aliases": ["fixture:offline-001"], "abstract": "Synthetic fixture for pipeline checks. Not a real paper.",
                   "url": "", "date": today(), "venue_kind": "journal", "publication": {"status": "unknown"}}
        pid = st.ingest(fixture)
        st.record(pid, "abstract", "fixture-v1", "offline_no_model", {"message": "离线演示没有调用模型，也未生成模型总结"})
        w = {"journals": [], "conferences": []}
        h = HTTP(c, {})
        items, failures = process(c, {}, st, h, w, metadata_only=True, demo=True)
        report = {"mode": "OFFLINE TEST ONLY", "papers": len(st.papers()), "formal": len(items), "failures": failures}
        brief = render_brief(items, report, c, demo=True)
        report.update(vault=str(c["paths"]["vault"]), brief=str(brief), links=check_links(c["paths"]["vault"]))
        atomic_write(c["paths"]["data"] / "result.json", json.dumps(report, ensure_ascii=False, indent=2))
        st.db.close()
        h.client.close()
        return report


def status(c):
    from .capacity import preview
    s = Store(c["paths"]["data"])
    result = {"papers": len(s.papers()), "last_run": s.get_state("last_run"), "last_fetch": s.get_state("last_fetch"),
              "daily_usage": {k: s.used(k) for k in ("candidates", "abstracts", "fulltext", "model_calls", "bilingual")},
              "stages": [dict(r) for r in s.db.execute("SELECT stage,status,count(*) AS n FROM stages GROUP BY stage,status")],
              "failures": [dict(r) for r in s.db.execute("SELECT paper_id,stage,status,detail FROM stages WHERE status IN ('failed','needs_configuration','component_missing','input_limit')")],
              "links": check_links(c["paths"]["vault"]) if c["paths"]["vault"].exists() else None}
    result['capacity'] = preview(c,s)
    s.db.close()
    return result


def stop(c):
    # Cooperative, portable stop: the pipeline observes this marker between papers/sources.
    atomic_write(c["paths"]["data"] / "STOP", "stop requested " + now())
    return {"status": "已请求停止；当前 HTTP/翻译步骤结束后停止，下一次手动运行会清除请求"}


def main(argv=None):
    parser = argparse.ArgumentParser(description="本机论文获取、证据筛选与 Obsidian 写入")
    parser.add_argument("--config", default="config.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("doctor"); d.add_argument("--network", action="store_true")
    for name in ("run", "retry"):
        r = sub.add_parser(name)
        r.add_argument("--metadata-only", action="store_true", help="禁止模型与翻译调用")
        r.add_argument("--no-fetch", action="store_true", help="仅处理已入库记录")
    sub.add_parser("demo")
    sub.add_parser("status")
    sub.add_parser("check-links")
    sub.add_parser("stop")
    cnki = sub.add_parser('import-cnki', help='本地 EndNote 题录导入；不调用网络或模型')
    cnki.add_argument('file', nargs='?', help='省略时扫描 data/imports/cnki/pending')
    full = sub.add_parser('attach-fulltext', help='保存授权取得的本地全文；不调用模型')
    full.add_argument('paper_id')
    full.add_argument('file')
    full.add_argument('--language', choices=('zh','en','unknown'), default='unknown')
    browser = sub.add_parser('cnki-browser', help='手动触发本地 DeepSeek + Playwright MCP 知网网页流程')
    browser.add_argument('action', choices=('run','resume','inspect','open','status','disable','enable'))
    browser.add_argument('--query')
    browser.add_argument('--url')
    browser.add_argument('--session')
    browser.add_argument('--paper-id')
    browser.add_argument('--download-one', action='store_true')
    browser.add_argument('--school', action='store_true', help='仅人工打开深圳理工大学官方 CARSI 链接')
    browser.add_argument('--after-school-login', action='store_true', help='用户已完成学校认证，允许该篇普通下载按钮检查权限；不推定已授权')
    cap = sub.add_parser('capacity', help='容量状态、只读预览、安全整理、恢复及固定保留')
    cap.add_argument('action', choices=('status','preview','apply','restore','pin','unpin'))
    cap.add_argument('--paper-id')
    daily = sub.add_parser("daily", help="完整日常流程：检索、原文审校、双语PDF与正式文件夹交付")
    daily.add_argument("--no-fetch", action="store_true")
    daily.add_argument("--offline-replay", action="store_true", help="禁止所有网络、模型与翻译；仅核对已保存成果、命名和已有日报")
    daily.add_argument("--paper-id", action="append")
    daily.add_argument("--max-complete", type=int)
    recent = sub.add_parser("recent-test", help="本轮受限近期论文验收；只使用已缓存检索结果，不检索或翻译")
    recent.add_argument("--summarize", action="store_true", help="允许对已核验的一篇新论文最多请求一次模型；失败不自动重试")
    recent.add_argument("--replay", action="store_true", help="只重放同一批缓存，禁止模型请求")
    one = sub.add_parser("summarize-one", help="单篇验收：只使用已下载 PDF，最多 1 次模型请求，不生成每日推荐")
    one.add_argument("--approve-extra-call", action="store_true", help="显式授权失败后的额外 1 次请求（验收累计最多 2 次），不循环重试")
    one.add_argument("--recover-response", action="store_true", help="仅离线核对已保存的完整模型响应；不发起网络请求")
    w = sub.add_parser("import-whitelist")
    w.add_argument("kind", choices=("journals", "conferences")); w.add_argument("csv")
    r = sub.add_parser("verify-proceedings", help="从官方论文集导入已发表主会长文；手动导入不受主题时间窗口限制")
    r.add_argument("url")
    r.add_argument("--metadata-only", action="store_true")
    r = sub.add_parser("lookup-openalex", help="按稳定 OpenAlex Work ID 获取真实元数据，独立于主题检索")
    r.add_argument("work_id")
    r = sub.add_parser('journal-refresh', help='仅查询期刊及补笔记头部；不调用模型或翻译')
    r.add_argument('--offline', action='store_true')
    r.add_argument('--paper-id', action='append')
    r = sub.add_parser('journal-import', help='导入CSV/JSON期刊指标并离线补头部；不修改正式资格白名单')
    r.add_argument('file')
    args = parser.parse_args(argv)
    try:
        c, env = load_config(args.config), secrets()
        if args.command == 'cnki-browser':
            from .cnki_browser import command
            if args.action in ('resume','open') and not args.session:
                raise ValueError('需要 --session')
            result=command(c,env,args)
        elif args.command == 'capacity':
            from .capacity import command
            if args.action in ('restore','pin','unpin') and not args.paper_id:
                raise ValueError('此操作需要 --paper-id')
            result = command(c,args.action,args.paper_id)
        elif args.command == 'import-cnki':
            from .cnki import command
            result = command(c, local_path(args.file) if args.file else None)
        elif args.command == 'attach-fulltext':
            from .cnki import attach_fulltext
            result = attach_fulltext(c,args.paper_id,local_path(args.file),args.language)
        elif args.command == "doctor":
            result = doctor(c, env, args.network)
        elif args.command == "demo":
            result = demo(c)
        elif args.command == "daily":
            from .daily import run_daily
            result = run_daily(c, env, fetch=not args.no_fetch, only=args.paper_id, max_complete=args.max_complete, offline=args.offline_replay)
        elif args.command in ("run", "retry"):
            if c.get("acceptance") and not args.metadata_only:
                if not args.no_fetch:
                    raise ValueError("单篇配置禁止检索；请使用 summarize-one")
                from .single import summarize_one
                result = summarize_one(c, env)
            elif c.get("daily", {}).get("enabled"):
                from .daily import run_daily
                result = run_daily(c, env, fetch=not args.no_fetch, metadata_only=args.metadata_only)
            else:
                result = run(c, env, retry=args.command == "retry", metadata_only=args.metadata_only, fetch=not args.no_fetch)
        elif args.command == "status":
            result = status(c)
        elif args.command == "recent-test":
            from .recent import run_recent
            result = run_recent(summarize=args.summarize, replay=args.replay)
        elif args.command == "summarize-one":
            from .single import summarize_one
            result = summarize_one(c, env, allow_extra_call=args.approve_extra_call, recover_saved_response=args.recover_response)
        elif args.command == "check-links":
            result = check_links(c["paths"]["vault"])
        elif args.command == "stop":
            result = stop(c)
        elif args.command == "import-whitelist":
            with run_lock(c["paths"]["data"]):
                result = {"imported": import_csv(args.kind, local_path(args.csv), c["paths"]["whitelist"])}
        elif args.command in ('journal-import','journal-refresh'):
            from .journals import import_metrics, refresh_notes
            with run_lock(c['paths']['data']):
                st=Store(c['paths']['data'])
                try:
                    imported=import_metrics(local_path(args.file),st) if args.command=='journal-import' else None
                    result=refresh_notes(c,env,st,load_whitelist(c['paths']['whitelist']),
                        offline=args.command=='journal-import' or args.offline,
                        only=getattr(args,'paper_id',None))
                    if imported:result['import']=imported
                    atomic_write(c['paths']['data']/'journal-refresh-latest.json',json.dumps(result,ensure_ascii=False,indent=2))
                finally:st.db.close()
        elif args.command == "verify-proceedings":
            with run_lock(c["paths"]["data"]):
                st, h = Store(c["paths"]["data"]), HTTP(c, env)
                w = load_whitelist(c["paths"]["whitelist"])
                p = verify_neurips(args.url, h)
                gate = classify(p, w)
                pid = st.ingest(p, c["limits"]["candidates_per_day"])
                if not pid:
                    result = {"status": "daily_limit"}
                else:
                    items, failures = process(c, env, st, h, w, metadata_only=args.metadata_only)
                    report = {"manual_proceedings_import": True, "paper_id": pid, "title": p["title"], "eligibility": gate, "failures": failures}
                    result = dict(report, brief=str(render_brief(items, report, c)), links=check_links(c["paths"]["vault"]))
                st.db.close(); h.client.close()
        elif args.command == "lookup-openalex":
            import re
            if not re.fullmatch(r"W\d+", args.work_id):
                raise ValueError("需要 W 开头的 OpenAlex Work ID")
            with run_lock(c["paths"]["data"]):
                st, h = Store(c["paths"]["data"]), HTTP(c, env)
                p = openalex_paper(h.json("https://api.openalex.org/works/" + args.work_id))
                pid = st.ingest(p, c["limits"]["candidates_per_day"])
                w = load_whitelist(c["paths"]["whitelist"])
                gate = classify(p, w)
                items, failures = process(c, env, st, h, w, metadata_only=True)
                result = {"source": "openalex", "paper_id": pid, "title": p["title"], "eligibility": gate,
                          "failures": failures, "links": check_links(c["paths"]["vault"])}
                st.db.close(); h.client.close()
        output(result)
        if isinstance(result, dict) and result.get('stopped'): return 130
        if args.command=='cnki-browser' and args.action in ('run','resume') and result.get('status')!='complete':
            return 2
        if args.command in ('import-cnki','attach-fulltext') and (result.get('failed') or result.get('render_errors') or result.get('deferred')):
            return 2
        if args.command == 'capacity' and result.get('problems'):
            return 2
        if args.command == "recent-test" and args.summarize and result.get("summary_status") != "success":
            return 2
        if isinstance(result, dict) and result.get("failures"):
            return 2
        if result.get("max_calls_per_run") == 1 and result.get("status") != "success":
            return 2
        if args.command in ("run", "retry", "daily") and result.get("fetch_performed", True) and any(v["status"] == "failed" for v in result.get("sources", {}).values()):
            return 2
        return 0
    except KeyboardInterrupt:
        print("已停止；已完成阶段已保存在 SQLite，可用 retry 续跑。", file=sys.stderr)
        return 130
    except Exception as exc:
        from .core import safe_error
        output({"status": "error", "error": safe_error(exc), "message": "请检查配置、路径、证据文件和日志；未打印异常正文以避免泄露凭据"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
