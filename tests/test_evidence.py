import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from paper_obsidian.analysis import Model, AnalysisError, parse_pdf, PROMPT, request_data, analysis_version, decode_editorial_json
from paper_obsidian.core import Store, digest, load_config, now
from paper_obsidian.evidence import SCHEMA, FIELDS, attach_catalog, claims, content_payload, approve_review, publishable, validate_concise


def fixture():
    parsed = attach_catalog({"pages": ["TEST original: model Alpha scores 76.46 accuracy on dataset One.\n" * 32], "text":"TEST", "page_count":1})
    ids = [next(iter(parsed["evidence_catalog"]))]
    result = {"schema_version":SCHEMA, "title_zh":"测试", "summary":"测试结论", "summary_evidence_ids":ids,
              "sections":{key:[{"text":"测试结论", "evidence_ids":ids}] for key in FIELDS}, "ai_analysis":[]}
    return parsed, result


def reviewed(parsed, result):
    result = copy.deepcopy(result)
    result["evidence_catalog"] = parsed["evidence_catalog"]
    review = {"content_sha256":digest(content_payload(result)), "catalog_sha256":digest(parsed["evidence_catalog"]),
              "reviewer":"UNIT TEST ONLY", "reviewed_at":now(), "claims":{
                  key:{"supported":True, "reason":"SYNTHETIC TEST ONLY", "claim_sha256":digest(claim)} for key, claim in claims(result)}}
    return approve_review(result, parsed, review)


class EvidenceTests(unittest.TestCase):
    def test_codex_provenance_and_evidence_are_visible_without_interrupting_body(self):
        from paper_obsidian.vault import render_paper
        with tempfile.TemporaryDirectory() as tmp:
            c = load_config("tests/fixtures/config.toml"); c['paths'].update(data=Path(tmp)/'data', vault=Path(tmp)/'vault')
            s = Store(c['paths']['data']); parsed, result = fixture()
            try:
                p = {'source':'TEST', 'aliases':['test:provenance'], 'title':'TEST'}
                pid = s.ingest(p); p = s.get(pid)
                result = reviewed(parsed, result)
                result['semantic_review'].update(reviewer_type='codex', user_reviewed=False)
                s.record(pid, 'fulltext', 'TEST', 'success', result)
                path = render_paper(p, {'eligible':True,'reason':'TEST','publication_status':'published','evidence':[]}, c, s)
                raw = path.read_text()
                self.assertIn('reviewer_type: codex',raw)
                self.assertIn('user_reviewed: false',raw)
                self.assertIn('经Codex原文校对，用户未复核',raw)
                body, details = raw.split('<details>',1)
                self.assertNotIn('p001-b001',body)
                self.assertIn('p001-b001',details)
                self.assertEqual(path.parent.name,'90_候选与待核验')  # complete two-file delivery is owned by daily.publish
            finally: s.db.close()

    def test_editorial_format_recovery_never_creates_missing_content(self):
        result, changes = decode_editorial_json('{"text":"完整"}}')
        self.assertEqual(result,{"text":"完整"})
        self.assertEqual(len(changes),1)
        for raw in ('{"text":"未完成', '{"text":', '{"text":"完整"}junk'):
            with self.assertRaises(json.JSONDecodeError): decode_editorial_json(raw)

    def test_existing_id_does_not_prove_support_or_publish(self):
        parsed, result = fixture()
        result["sections"]["results"][0]["text"] = "在所有任务都达到99.99%，否定原文"
        self.assertEqual(validate_concise(result, parsed)["semantic_support"], "requires_review")
        self.assertFalse(publishable(result))
        approved = reviewed(parsed, result)
        review = approved["semantic_review"]
        review["claims"]["results.0"]["supported"] = False
        with self.assertRaises(ValueError): approve_review(result, parsed, review)
        self.assertFalse(publishable(approved))

    def test_nonexistent_id_and_tampered_numbers_rejected(self):
        parsed, result = fixture()
        result["summary_evidence_ids"] = ["p999-b999"]
        with self.assertRaises(ValueError): validate_concise(result, parsed)
        parsed, result = fixture()
        block = next(iter(parsed["evidence_catalog"].values()))
        block["raw_text"] = block["raw_text"].replace("76.46", "99.99")
        with self.assertRaises(ValueError): validate_concise(result, parsed)

    def test_physical_page_is_not_publication_label(self):
        parsed = attach_catalog({"pages":["publication page 1606\n", "publication page 1607\n"]})
        self.assertEqual(parsed["evidence_catalog"]["p002-b001"]["physical_page"], 2)
        self.assertIsNone(parsed["evidence_catalog"]["p002-b001"]["publication_page"])
        self.assertIn("1607", parsed["evidence_catalog"]["p002-b001"]["raw_text"])

    def test_changed_claim_or_catalog_invalidates_approval(self):
        parsed, result = fixture()
        result = reviewed(parsed, result)
        self.assertTrue(publishable(result))
        result["sections"]["method"][0]["text"] += "篡改"
        self.assertFalse(publishable(result))

    def test_input_limit_includes_ids_and_prompt_never_clips(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = load_config("tests/fixtures/config.toml"); c["paths"]["data"] = Path(tmp)
            parsed, result = fixture()
            c["limits"]["input_chars_per_call"] = len(json.dumps(request_data({"title":"TEST"}, parsed),ensure_ascii=False)) + len(PROMPT) - 1
            s = Store(Path(tmp))
            try:
                m = Model(c, {"API_BASE_URL":"https://example.invalid", "API_KEY":"TEST", "MODEL":"TEST"}, s,
                          httpx.MockTransport(lambda r:self.fail("input overflow made a request")))
                self.assertEqual(m.summarize({"title":"TEST"}, parsed)[0], "input_limit")
                self.assertEqual(s.used("model_calls"), 0)
            finally: s.db.close()

    def test_truncated_valid_json_still_rejected_and_failure_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = load_config("tests/fixtures/config.toml"); c["paths"]["data"] = Path(tmp)
            s = Store(Path(tmp)); parsed, result = fixture(); count=[]
            def response(r):
                count.append(1)
                return httpx.Response(200,json={"choices":[{"finish_reason":"length", "message":{"content":json.dumps(result)}}], "usage":{"prompt_tokens":23,"completion_tokens":6000,"total_tokens":6023}})
            try:
                m = Model(c, {"API_BASE_URL":"https://example.invalid", "API_KEY":"TEST", "MODEL":"TEST"}, s, httpx.MockTransport(response))
                self.assertEqual(m.summarize({"title":"TEST"}, parsed)[0], "failed")
                self.assertEqual(m.summarize({"title":"TEST"}, parsed)[0], "previous_attempt_not_retried")
                self.assertEqual(len(count),1)
                self.assertTrue(list((Path(tmp)/"model-responses").glob("*.json")))
                self.assertFalse(list((Path(tmp)/"model-cache").glob("*.json")))
            finally: s.db.close()

    def test_approved_fulltext_cache_replay_no_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = load_config("tests/fixtures/config.toml"); c["paths"]["data"] = Path(tmp)
            s = Store(Path(tmp)); parsed, result = fixture(); count=[]
            def response(r):
                count.append(1)
                return httpx.Response(200,json={"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(result)}}]})
            try:
                m = Model(c,{"API_BASE_URL":"https://example.invalid", "API_KEY":"TEST", "MODEL":"TEST"},s,httpx.MockTransport(response))
                status, generated = m.summarize({"title":"TEST"},parsed)
                self.assertEqual(status,"review_required")
                approved = reviewed(parsed,generated)
                (Path(tmp)/"model-cache"/(m.key+".json")).write_text(json.dumps(approved))
                m2 = Model(c,m.env,s,httpx.MockTransport(lambda r:self.fail("cache replay made a request")))
                self.assertEqual(m2.summarize({"title":"TEST"},parsed)[0],"success")
                self.assertEqual(m2.network_attempts,0)
                self.assertEqual(len(count),1)
                v = analysis_version({"title":"TEST"},c,m.env)
                c["model"]["thinking"] = "enabled"
                self.assertNotEqual(v,analysis_version({"title":"TEST"},c,m.env))
            finally: s.db.close()
