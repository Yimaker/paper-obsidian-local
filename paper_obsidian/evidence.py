"""Deterministic physical-page evidence; locating text is not semantic verification."""
from __future__ import annotations

import copy
import re

from .core import digest, now

SCHEMA = "concise-evidence-v2"
FIELDS = {"problem": "研究问题", "method": "核心方法", "experiments": "实验设置",
          "results": "主要结果", "limitations": "作者明确陈述的局限／未来工作", "resources": "复现资源"}


def build_catalog(pages):
    blocks = {}
    for page, raw in enumerate(pages, 1):
        # Contiguous original line spans, including whitespace. No fuzzy matching,
        # digit changes, dehyphenation or dropped columns, lines, references or pages.
        lines = raw.splitlines(keepends=True)
        for offset in range(0, len(lines), 8):
            original = "".join(lines[offset:offset + 8])
            eid = f"p{page:03d}-b{offset // 8 + 1:03d}"
            blocks[eid] = {"physical_page": page, "publication_page": None,
                           "line_start": offset + 1, "raw_text": original,
                           "text": re.sub(r"[ \t]{2,}", " ", original)}
    return blocks


def attach_catalog(parsed):
    parsed = copy.deepcopy(parsed)
    parsed["evidence_schema"] = SCHEMA
    parsed["evidence_catalog"] = build_catalog(parsed["pages"])
    parsed["catalog_sha256"] = digest(parsed["evidence_catalog"])
    parsed["evidence_text"] = "\n".join(f"[{eid}]\n{b['text']}" for eid, b in parsed["evidence_catalog"].items())
    return parsed


def content_payload(result):
    return {k: result.get(k) for k in ("schema_version", "title_zh", "summary", "summary_evidence_ids", "sections", "ai_analysis")}


def claims(result):
    yield "summary", {"text": result["summary"], "evidence_ids": result["summary_evidence_ids"]}
    for key in FIELDS:
        for i, claim in enumerate(result["sections"][key]):
            yield f"{key}.{i}", claim
    for i, claim in enumerate(result.get("ai_analysis", [])):
        yield f"ai_analysis.{i}", claim


def validate_concise(result, parsed, error=ValueError):
    if result.get("schema_version") != SCHEMA:
        raise error("全文总结 schema 版本不匹配")
    for key in ("title_zh", "summary"):
        if not isinstance(result.get(key), str) or not result[key].strip():
            raise error("精简总结缺少必填字段")
    if not re.search(r"[\u4e00-\u9fff]", result["summary"]):
        raise error("模型未生成中文")
    if not isinstance(result.get("sections"), dict) or set(result["sections"]) != set(FIELDS):
        raise error("全文必需章节不完整或含重复章节")
    for key in FIELDS:
        value = result["sections"][key]
        if not isinstance(value, list) or not value:
            raise error("必需章节没有实质内容")
    if not isinstance(result.get("ai_analysis", []), list) or len(result.get("ai_analysis", [])) > 2:
        raise error("AI分析最多2条")
    catalog = parsed["evidence_catalog"]
    if catalog != build_catalog(parsed["pages"]):
        raise error("证据目录与原始分页文字不一致")
    total = 0
    for key, claim in claims(result):
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str) or not claim["text"].strip():
            raise error("结论结构不完整")
        if set(claim) != {"text", "evidence_ids"}:
            raise error("模型只能返回文字和已有证据ID；不能自带验证结论或原文")
        total += len(claim["text"])
        refs = claim.get("evidence_ids")
        if not isinstance(refs, list) or any(not isinstance(eid, str) or eid not in catalog for eid in refs):
            raise error("证据ID不存在")
        if len(refs) != len(set(refs)):
            raise error("同一结论重复引用证据")
        missing_statement = claim["text"] == "未发现明确说明" or claim["text"].startswith("未发现明确说明：")
        if not refs and not (key.startswith(("limitations.", "resources.")) and missing_statement):
            raise error("结论缺少原文证据；缺失说明仅用于作者局限或复现资源")
    # A length guard is a safety bound, not a fixed quota of claims or citations.
    if total > 2800:
        raise error("正文过长，不能以重复章节或证据挤满输出")
    return {"status": "located_only", "semantic_support": "requires_review",
            "claim_count": sum(1 for _ in claims(result)), "body_chars": total,
            "catalog_sha256": digest(catalog), "content_sha256": digest(content_payload(result))}


def approve_review(result, parsed, review):
    """An external reviewer supplies a per-claim audit, bound to exact text and IDs.

    This function deliberately does not infer English-to-Chinese entailment. A
    model-generated or merely located evidence ID cannot approve publication.
    """
    validation = validate_concise(result, parsed)
    if review.get("content_sha256") != validation["content_sha256"] or review.get("catalog_sha256") != validation["catalog_sha256"]:
        raise ValueError("校对对象指纹不一致")
    expected = {key for key, _ in claims(result)}
    checks = review.get("claims", {})
    if set(checks) != expected or not review.get("reviewer") or not review.get("reviewed_at"):
        raise ValueError("缺少逐项原文语义复核")
    for key, claim in claims(result):
        check = checks[key]
        if check.get("supported") is not True or not check.get("reason") or check.get("claim_sha256") != digest(claim):
            raise ValueError("原文不支持结论或未完成语义核对：" + key)
    result = copy.deepcopy(result)
    result["semantic_review"] = dict(review, status="passed")
    result["validation"] = validation
    return result


def publishable(result):
    if result.get("schema_version") != SCHEMA:
        # Historical accepted results are not retroactively rewritten by this migration.
        return bool(result.get("editorial_review"))
    review = result.get("semantic_review", {})
    return (review.get("status") == "passed" and
            bool(review.get("reviewer")) and bool(review.get("reviewed_at")) and
            set(review.get("claims", {})) == {key for key, _ in claims(result)} and
            review.get("catalog_sha256") == digest(result.get("evidence_catalog", {})) and
            review.get("content_sha256") == digest(content_payload(result)) and
            all(review.get("claims", {}).get(key, {}).get("supported") is True and
                review["claims"][key].get("claim_sha256") == digest(claim)
                for key, claim in claims(result)))
