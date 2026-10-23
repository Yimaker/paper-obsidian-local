from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path
from urllib.parse import quote, unquote

import yaml

from .analysis import FULL_FIELDS
from .evidence import SCHEMA, FIELDS, publishable, claims
from .core import atomic_write, digest, now, today
from .library import candidate_path, note_for_id, naming_meta, portable, move_note, sync_formal_path, MD_LINK, WIKI_LINK
from .journals import with_header, patch_note
from .capacity import guarded

START, END = "<!-- PAPER-AUTO:START -->", "<!-- PAPER-AUTO:END -->"
FOLDERS = ("00_每日简报", "10_论文", "90_候选与待核验")


def text(s):
    # Disable raw HTML embedded in untrusted papers/model output. Math and Markdown remain editable.
    return str(s).replace("<", "&lt;").replace(">", "&gt;")


def link(from_path, to_path, label):
    return f"[{text(label).replace('[', '').replace(']', '')}]({quote(os.path.relpath(to_path, from_path.parent).replace(os.sep, '/'), safe='/.-_')})"


def managed_write(path, meta, body, c):
    backup = c["paths"]["data"] / "backups"
    personal = "\n\n## 我的手写笔记\n\n（此区域由你维护，自动更新会保留。）\n"
    before, after, previous_meta = "", personal, {}
    if path.exists():
        old = path.read_text(encoding="utf-8")
        if old.count(START) != 1 or old.count(END) != 1 or old.index(END) < old.index(START):
            atomic_write(backup / (digest(str(path))[:16] + ".conflict.md"), old)
            raise ValueError("自动区域标记缺失/重复：已保留原笔记并备份，拒绝覆盖")
        if old.startswith("---\n"):
            _, raw, remainder = old.split("---\n", 2)
            previous_meta = yaml.safe_load(raw) or {}
            if not isinstance(previous_meta, dict):
                raise ValueError("原笔记 YAML 不是对象，拒绝覆盖")
            old = remainder
        before, rest = old.split(START, 1)
        _, after = rest.split(END, 1)
    previous_meta.update(meta)
    front = yaml.safe_dump(previous_meta, allow_unicode=True, sort_keys=False, width=1000).strip()
    content = "---\n" + front + "\n---\n" + before + START + "\n" + body.strip() + "\n" + END + after
    return atomic_write(path, content, backup)


def copy_attachment(src, c, pid, suffix):
    path = c["paths"]["data"] / "archive" / pid / (pid + suffix)
    raw = Path(src).read_bytes()
    # Keep every original version; never replace an existing different PDF.
    if path.exists() and path.read_bytes() != raw:
        path = path.with_name(path.stem + "-" + digest(raw)[:10] + path.suffix)
    atomic_write(path, raw)
    return path


def promote_note(candidate, path, c):
    """Move the stable-ID note once and rewrite local Markdown/wiki references.

    Every changed file and the original candidate is backed up. Handwritten text
    and custom YAML travel with the file; only references to its old path change.
    """
    move_note(candidate, path, c)


def concise_body(r, path, c, pid):
    registry = r["evidence_catalog"]
    evidence_path = c["paths"]["data"] / "archive" / pid / (pid + "-evidence.md")
    used = sorted({eid for _, claim in claims(r) for eid in claim["evidence_ids"]})
    pages = r.get("publication_pages", {})
    evidence = ["# 程序定位的原文证据", "", "PDF物理页从1开始；出版页码单独显示。定位通过不代表语义支持；逐项复核见笔记验收记录。", ""]
    for eid in used:
        b = registry[eid]
        page = b["physical_page"]
        publication = pages.get(str(page), {}).get("label", "未识别")
        evidence += ["## " + eid, f"物理第{page}页；出版页码：{publication}；原始行 {b['line_start']} 起。", "", "```text", b["raw_text"].rstrip(), "```", ""]
    managed_write(evidence_path, {"paper_id": pid, "kind": "evidence", "catalog_sha256": digest(registry)}, "\n".join(evidence), c)
    review = r.get("semantic_review", {})
    label = "经Codex原文校对，用户未复核" if review.get("reviewer_type") == "codex" and review.get("user_reviewed") is False else "经校对通过"
    body = ["## " + text(r["title_zh"]), "", "**" + label + "**：逐项校对记录、原始响应与修改差异保留。", "",
            "### 一句话总结", text(r["summary"]), ""]
    def refs(claim):
        return "证据：" + "、".join(link(path, evidence_path, eid)[:-1] + "#" + eid + ")" for eid in claim["evidence_ids"])
    evidence_notes = ["### 一句话总结", refs({"evidence_ids":r["summary_evidence_ids"]}), ""]
    for key, label in FIELDS.items():
        body += ["### " + label, ""]
        for i, claim in enumerate(r["sections"][key], 1):
            body += [text(claim["text"]), ""]
            if claim["evidence_ids"]:
                evidence_notes += [f"- {label} 第{i}项：" + refs(claim), ""]
    if r.get("ai_analysis"):
        body += ["### 可选 AI 分析（推断，非作者声明）", ""]
        for i, claim in enumerate(r["ai_analysis"], 1):
            body += [text(claim["text"]), ""]
            evidence_notes += [f"- AI分析 第{i}项：" + refs(claim), ""]
    body += ["<details>", "<summary>原文证据与定位详情</summary>", ""] + evidence_notes + ["</details>", ""]
    body += ["范围：全文文本层完整输入；关键数值、任务、比较对象及局限经原文复核。未声称穷尽核验所有参考文献与公式图像。", ""]
    return body


@guarded
def render_paper(p, gate, c, store, *, demo=False):
    vault = c["paths"]["vault"]
    pid = p["id"]
    delivery=store.stage(pid,'delivery')
    if delivery and delivery['status']=='success':
        existing=sync_formal_path(p,c,store)
        if existing:
            patch_note(existing,p,gate,c,store)
            return existing
    stages = {s: store.stage(pid, s) for s in ("abstract", "pdf", "fulltext", "translation")}
    full = stages["fulltext"]
    full_result = json.loads(full["detail"]) if full and full["status"] == "success" else {}
    formal = gate["eligible"] and publishable(full_result)
    # Legacy commands may prepare summaries, but only daily.publish creates a complete delivery.
    formal = False
    path = candidate_path(p,c,store)
    meta = {"paper_id": pid, "title": p["title"], "authors": p.get("authors", []), "date": p.get("date"),
            "doi": p.get("doi", ""), "arxiv_id": p.get("arxiv_id", ""), "identifiers": p.get("aliases", []),
            "sources": p.get("sources", []), "venue": p.get("venue", ""), "issns": p.get("issns", []),
            "dates": p.get("dates", {}),
            "publication_status": gate["publication_status"], "library_status": "正式库" if formal else ("候选（资格已核验）" if gate["eligible"] else "待核验"),
            "summary_review_status": "经校对通过" if formal else (full["status"] if full else "not_requested"),
            "eligibility_reason": gate["reason"], "eligibility_evidence": gate["evidence"],
            "processing": {k: v["status"] if v else "not_requested" for k, v in stages.items()}, "test_data": demo}
    meta['qualification'] = gate
    meta.update(naming_meta(p))
    review = full_result.get("semantic_review", {})
    if review.get("reviewer_type"):
        meta.update(reviewer_type=review["reviewer_type"], user_reviewed=review.get("user_reviewed", False))
        if formal and review["reviewer_type"] == "codex" and review.get("user_reviewed") is False:
            meta["summary_review_status"] = "经Codex原文校对，用户未复核"
    body = [f"# {text(p['title'])}", "", f"**{'测试数据；禁止用于正式筛选' if demo else meta['library_status']}**", "",
            f"筛选依据：{text(gate['reason'])}", f"发表状态：`{gate['publication_status']}`", "",
            f"[来源原始记录]({p.get('url', '')})", ""]
    if p.get("dates"):
        body += ["## 日期与收录口径", ""]
        labels = {"preprint_published": "arXiv 首次预印本发布", "preprint_updated": "arXiv 版本更新",
                  "openalex_publication_date": "OpenAlex 发表元数据日期（不能单独证明正式发表）",
                  "openalex_created_date": "OpenAlex 记录创建/收录时间", "openalex_updated_date": "OpenAlex 记录更新",
                  "official_citation_publication_date": "官方论文集 citation_publication_date（不等于首次公开日期）",
                  "official_proceedings_month": "官方BibTeX论文集出版月份（无具体日期）",
                  "proceedings_year": "论文集/会议年份", "official_verified_at": "官方发表核验时间",
                  "openalex_retrieved_at": "本机获取 OpenAlex 时间", "arxiv_retrieved_at": "本机获取 arXiv 时间",
                  "retrieved_at": "本机元数据获取时间", "date_note": "日期说明"}
        body += [f"- {labels.get(k,k)}：{text(v)}" for k,v in p["dates"].items() if v]
        body += ["- 未取得的首次上线/录用日期保留未知；本机获取时间不冒充发表日期。", ""]
    attachments = []
    for stage, suffix, label in (("pdf", "-original.pdf", "原文 PDF"), ("translation", "-bilingual.pdf", "中英双语 PDF")):
        rec = stages[stage]
        if rec and rec["status"] == "success":
            detail = json.loads(rec["detail"])
            src = detail.get("path")
            if src and portable(src).exists():
                attachments.append(link(path, copy_attachment(portable(src), c, pid, suffix), label))
    body += attachments + [""]
    success = stages["fulltext"] if stages["fulltext"] and stages["fulltext"]["status"] == "success" else stages["abstract"]
    if full_result.get("schema_version") == SCHEMA and publishable(full_result):
        body += concise_body(full_result, path, c, pid)
    elif success and success["status"] == "success" and json.loads(success["detail"]).get("schema_version") != SCHEMA:
        # Legacy/abstract display; abstract success alone never promotes a note.
        r = json.loads(success["detail"])
        if r.get("historical_function_test"):
            body += ["**历史论文功能验收：本次中文总结使用已有原文，不属于最新论文推荐。**", ""]
        if r.get("format_repairs"):
            body += ["格式校验记录：" + "；".join(text(x) for x in r["format_repairs"]), ""]
        if r.get("editorial_review"):
            review = r["editorial_review"]
            body += ["原文抽查与校对：" + text(review["status"]) + "。保留原始模型响应；以下校对没有新增 API 请求。", ""]
            body += ["- " + text(item) for item in review["notes"]]
            body += ["", "术语：" + text(review["terms"]), ""]
        body += [f"## {text(r['title_zh'])}", "", f"依据：**{r['basis']}**", "", "### 摘要翻译", text(r["abstract_zh"]),
                 "", "### 简短总结", text(r["summary"]), "", "### 推荐理由", text(r["recommendation"]), ""]
        if r.get("sections"):
            body += ["全文范围说明：使用原文完整文本层；图像、公式和复杂表格未完成视觉核验。请核对原文。", ""]
            for key, label in FULL_FIELDS.items():
                section = r["sections"][key]
                body += ["### " + label, text(section["text"]), ""]
                for e in section.get("evidence", []):
                    body += [f"- PDF 第 {e['page']} 页；{text(e['locator'])}：{text(e['quote'])}"]
                    if e.get("verification"):
                        body += ["  - 引文校验：" + text(e["verification"])]
                body.append("")
    else:
        message = ("全文预检未通过；尚未生成中文草稿，也未实施内容校对。具体原因见下方处理状态。"
                   if full and full["status"] in ("input_limit", "parse_incomplete") else
                   "尚未生成中文总结。模型未配置、未达处理条件或请求失败；以下为原始元数据。")
        if 'cnki' in p.get('sources',[]) and not full and not stages['abstract']:
            message = '知网题录候选；本次未请求模型总结或全文分析。以下是来源元数据，不代表已阅读全文。'
            if stages['pdf'] and stages['pdf']['status']=='success':
                message = '全文已取得，待处理；本次未请求中文总结、原文审校或翻译。以下仍为来源元数据，不计完整阅读交付。'
        body += ["## 中文总结状态", message, ""]
    body += ["## 原始摘要（来源元数据）", "来源页面的摘要版本可能与当前 PDF 不同；全文总结以 PDF 原文为准。", "", text(p.get("abstract") or "来源未提供摘要"), "", "## 处理状态"]
    for key, rec in stages.items():
        if rec:
            detail = json.loads(rec["detail"])
            msg = detail.get("message") or detail.get("error") or detail.get("warning") or ""
            body += [f"- {key}: `{rec['status']}` {text(msg)}"]
    previous_formal = vault / '10_论文笔记' / (pid+'.md')
    if previous_formal.exists() and previous_formal != path:
        if path.exists():raise ValueError('存在同ID多份笔记，拒绝覆盖')
        move_note(previous_formal,path,c)
    managed_write(path, meta, with_header("\n".join(body),p,gate,store), c)
    store.set_state('candidate-path:'+pid,path.relative_to(vault).as_posix())
    return path


def render_brief(items, report, c, *, demo=False):
    path = c["paths"]["data"].parent.parent / "docs" / "history" / "legacy-runs" / (today()+"-"+digest(report)[:12]+".md")
    body = ["# " + today() + " AI / ML 论文简报", "", "**离线测试数据**" if demo else "正式推荐只包含已通过证据筛选的论文。", ""]
    for p, gate, note in items[:c["limits"]["brief_per_day"]]:
        abstract = p.get("abstract", "")
        body += ["- " + link(path, note, p["title"]), "  - 规则推荐依据：主题关键词相关；" + text(gate["reason"])]
    if not items:
        body += ["本次没有足够可靠依据的正式论文；未放宽筛选标准。候选见 `90_候选与待核验`。"]
    body += ["", "## 来源状态", "```json", json.dumps(report, ensure_ascii=False, indent=2), "```", "",
             "写入本地 Vault 即可被本机 Obsidian 读取；本项目未配置跨设备同步。"]
    managed_write(path, {"date": today(), "test_data": demo, "timezone": "Asia/Shanghai"}, "\n".join(body), c)
    return path


def check_links(vault):
    missing, notes = [], 0
    files=[p for p in vault.rglob('*') if p.is_file() and '.obsidian' not in p.parts]
    for p in vault.rglob("*.md"):
        notes += 1
        raw = p.read_text(encoding="utf-8")
        if raw.startswith("---\n"):
            yaml.safe_load(raw.split("---\n", 2)[1])
        for match in MD_LINK.finditer(raw):
            target=match['target'].strip('<>')
            caption=re.match(r'^(.*?)(\s+["\'].*["\'])$',target)
            if caption:target=caption[1]
            if target.startswith(("https://", "http://", "#", "obsidian://")):
                continue
            target = unquote(target.split("#")[0])
            if not (p.parent / target).exists():
                missing.append({"note": str(p), "target": target})
        for match in WIKI_LINK.finditer(raw):
            target=unquote(match[1].split('|',1)[0].split('#',1)[0])
            if not target:continue
            candidates=[vault/target,p.parent/target]
            if not Path(target).suffix:candidates += [x.with_suffix('.md') for x in candidates]
            if not any(x.exists() for x in candidates) and not any(x.name.casefold()==target.casefold() or x.stem.casefold()==target.casefold() for x in files):
                missing.append({'note':str(p),'target':target,'kind':'wikilink'})
    return {"notes": notes, "missing_links": missing}
