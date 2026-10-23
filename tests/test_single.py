"""No live requests: scoped runner guards and paid-request accounting."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from paper_obsidian.analysis import Model, AnalysisError, FULL_FIELDS, decode_model_json, validate_summary
from paper_obsidian.core import Store, load_config
from paper_obsidian.pipeline import process, run
from paper_obsidian.single import summarize_one


class SingleTests(unittest.TestCase):
    def test_acceptance_config_limits(self):
        c = load_config("config.test.toml")
        self.assertEqual(c["scope"]["paper_ids"], ["80c6ca8c01157eb19816"])
        self.assertFalse(c["scope"]["allow_fetch"])
        self.assertFalse(c["scope"]["allow_download"])
        self.assertFalse(c["scope"]["write_brief"])
        self.assertEqual(c["limits"]["model_calls_per_day"], 1)
        self.assertEqual(c["limits"]["max_output_tokens"], 6000)
        self.assertFalse(c["translation"]["enabled"])

    def test_scoped_fetch_rejected_before_any_access(self):
        c = load_config("config.test.toml")
        with self.assertRaises(ValueError):
            run(c, {}, fetch=True)

    def test_multiple_ids_rejected(self):
        c = load_config("config.test.toml")
        c["scope"]["paper_ids"].append("not-allowed")
        with self.assertRaises(ValueError):
            summarize_one(c, {})

    def test_scope_renders_only_the_selected_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = load_config("config.test.toml")
            c["paths"]["data"] = Path(tmp)
            s = Store(Path(tmp))
            try:
                ids = [s.ingest({"source": "TEST_ONLY", "aliases": ["fixture:"+str(i)], "title": "TEST", "publication": {"status":"submitted"}}) for i in range(3)]
                c["scope"]["paper_ids"] = [ids[1]]
                with patch("paper_obsidian.pipeline.render_paper", return_value=Path(tmp)/"test.md") as render:
                    process(c, {}, s, None, {"journals":[],"conferences":[]}, metadata_only=True)
                self.assertEqual(render.call_count, 1)
                self.assertEqual(render.call_args.args[0]["id"], ids[1])
            finally:
                s.db.close()

    def test_billed_invalid_output_receipt_survives_validation_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            c = load_config("config.test.toml"); c["paths"]["data"] = Path(tmp)
            s = Store(Path(tmp))
            response = {"id":"TEST_ONLY_RECEIPT", "choices":[{"finish_reason":"stop", "message":{"content":"not valid JSON"}}],
                        "usage":{"prompt_tokens":123,"completion_tokens":7,"total_tokens":130}}
            m = Model(c, {"API_BASE_URL":"https://example.invalid/v1", "API_KEY":"TEST_ONLY", "MODEL":"fixture"}, s,
                      httpx.MockTransport(lambda r: httpx.Response(200,json=response)))
            try:
                status, result = m.summarize({"title":"TEST", "abstract":"TEST"})
                self.assertEqual(status,"failed")
                self.assertEqual(m.network_attempts,1)
                self.assertEqual(m.receipt["usage"]["total_tokens"],130)
                self.assertEqual(len(list((Path(tmp)/"model-responses").glob("*.json"))),1)
            finally:
                s.db.close()

    def test_absent_usage_is_unknown_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=load_config("config.test.toml"); c["paths"]["data"]=Path(tmp)
            s=Store(Path(tmp))
            fixture={"title_zh":"测试", "abstract_zh":"测试", "summary":"仅测试", "recommendation":"仅测试"}
            response={"choices":[{"finish_reason":"stop", "message":{"content":json.dumps(fixture)}}]}
            m=Model(c,{"API_BASE_URL":"https://example.invalid/v1", "API_KEY":"TEST_ONLY", "MODEL":"fixture"},s,
                    httpx.MockTransport(lambda r: httpx.Response(200,json=response)))
            try:
                status,r=m.summarize({"title":"TEST","abstract":"TEST"})
                self.assertEqual(status,"success")
                self.assertIsNone(r["usage"]["prompt_tokens"])
                status,r=m.summarize({"title":"TEST","abstract":"TEST"})
                self.assertTrue(m.cache_hit)
                self.assertEqual(m.network_attempts,1)
            finally:
                s.db.close()

    def test_thinking_disabled_is_in_real_request_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=load_config("config.test.toml"); c["paths"]["data"]=Path(tmp)
            s=Store(Path(tmp)); captured=[]
            def response(req):
                captured.append(json.loads(req.content))
                return httpx.Response(503)
            m=Model(c,{"API_BASE_URL":"https://example.invalid/v1","API_KEY":"TEST_ONLY","MODEL":"fixture"},s,httpx.MockTransport(response))
            try:
                m.summarize({"title":"TEST","abstract":"TEST"})
                self.assertEqual(captured[0]["thinking"],{"type":"disabled"})
                self.assertEqual(captured[0]["max_tokens"],6000)
                self.assertEqual(len(captured),1)
            finally:
                s.db.close()

    def test_only_missing_last_brace_is_repaired(self):
        value, repairs=decode_model_json('{"sections":{"test":{"text":"模型原文"}}')
        self.assertEqual(value["sections"]["test"]["text"],"模型原文")
        self.assertEqual(len(repairs),1)
        with self.assertRaises(json.JSONDecodeError):
            decode_model_json('{"text":"正文被截断')


if __name__ == "__main__":
    unittest.main()
