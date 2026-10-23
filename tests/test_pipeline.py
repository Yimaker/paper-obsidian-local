"""Synthetic test objects stay in TemporaryDirectory; they never feed the production whitelist."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from paper_obsidian.analysis import AnalysisError, Model, FULL_FIELDS, parse_pdf, translate_pdf, validate_summary
from paper_obsidian.cli import demo
from paper_obsidian.core import ROOT, Store, atomic_write, digest, load_config, local_path, now, run_lock
from paper_obsidian.eligibility import classify, load_whitelist
from paper_obsidian.pipeline import process, run
from paper_obsidian.proceedings import verify_neurips
from paper_obsidian.sources import HTTP, FetchError, arxiv_papers, openreview_paper, query_signature, window
from paper_obsidian.vault import check_links, managed_write, render_paper, START, END


class LocalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="paper-test-中文-")
        self.root = Path(self.tmp.name)
        self.c = load_config("tests/fixtures/config.toml")
        self.c["paths"].update(vault=self.root / "测试 Vault 中文", data=self.root / "data")
        self.c["network"].update(retries=0)
        self.store = Store(self.c["paths"]["data"])
        self.w = load_whitelist(self.c["paths"]["whitelist"])
        self.p = {"source": "TEST_FIXTURE", "aliases": ["fixture:001"], "title": "[测试] machine learning",
                  "abstract": "This is synthetic fixture text for tests only.", "date": "2026-10-01", "venue": "NeurIPS",
                  "venue_kind": "conference", "url": "https://example.invalid/test-only",
                  "publication": {"status": "submitted", "main_track": True, "paper_type": "full",
                                  "source": "https://proceedings.neurips.cc/test-only", "checked_at": now()}}

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def paper(self, status="published"):
        p = copy.deepcopy(self.p)
        p["publication"]["status"] = status
        pid = self.store.ingest(p)
        return self.store.get(pid)

    def test_repeat_identity_and_alias_bridge(self):
        a = self.store.ingest(self.p)
        self.assertEqual(a, self.store.ingest(self.p))
        p = dict(self.p, aliases=["doi:10.TEST/fixture"], source="OTHER_TEST_FIXTURE")
        self.store.ingest(p)
        p["aliases"] += self.p["aliases"]
        self.store.ingest(p)
        self.assertEqual(len(self.store.papers()), 1)
        self.assertEqual(self.store.db.execute("SELECT count(DISTINCT paper_id) FROM identifiers").fetchone()[0], 1)

    def test_titles_never_merge(self):
        self.store.ingest(self.p)
        self.store.ingest(dict(self.p, aliases=["fixture:different-id-same-title"]))
        self.assertEqual(len(self.store.papers()), 2)

    def test_preprint_cannot_erase_publication_evidence(self):
        p = self.paper()
        incoming = dict(self.p, venue="arXiv", venue_kind="repository", date="2026-10-02")
        incoming["publication"] = {"status": "preprint"}
        self.store.ingest(incoming)
        saved = self.store.get(p["id"])
        self.assertEqual(saved["venue"], "NeurIPS")
        self.assertTrue(classify(saved, self.w)["eligible"])

    def test_daily_cap_persistent(self):
        self.assertIsNotNone(self.store.ingest(self.p, 1))
        self.assertIsNone(self.store.ingest(dict(self.p, aliases=["fixture:2"]), 1))
        self.assertIsNotNone(self.store.ingest(self.p, 1))

    def test_preflight_failure_replays_as_candidate_without_summary_call(self):
        p = self.paper()
        self.c['scope'] = {'paper_ids':[p['id']]}
        self.store.record(p['id'], 'fulltext', 'TEST', 'input_limit', {'message':'完整输入62082 > 60000；未截断'})
        with patch.object(Model, 'summarize', side_effect=AssertionError('must not generate')):
            formal, failures = process(self.c, {}, self.store, None, self.w, metadata_only=True)
            self.assertEqual((formal, failures), ([], []))
            from paper_obsidian.library import note_for_id
            path = note_for_id(self.c,self.store,p['id'])
            before = path.read_bytes()
            self.assertIn('候选（资格已核验）', before.decode())
            self.assertIn('完整输入62082', before.decode())
            self.assertIn('尚未生成中文草稿，也未实施内容校对', before.decode())
            self.assertEqual(process(self.c, {}, self.store, None, self.w, metadata_only=True), ([], []))
            self.assertEqual(path.read_bytes(),before)

    def test_unknown_journal_and_submission_stay_pending(self):
        before = copy.deepcopy(self.w["journals"])
        self.assertFalse(classify(self.p, self.w)["eligible"])
        p = dict(self.p, venue_kind="journal", issns=["0000-TEST"])
        p["publication"] = {"status": "published"}
        self.assertFalse(classify(p, self.w)["eligible"])
        self.assertEqual(self.w["journals"], before)

    def test_ccf_b_and_workshop_not_admitted(self):
        p = self.paper()
        p["venue"] = "EMNLP"
        self.assertFalse(classify(p, self.w)["eligible"])
        p["venue"] = "NeurIPS"
        p["publication"]["main_track"] = False
        self.assertFalse(classify(p, self.w)["eligible"])
        p["publication"]["main_track"] = True
        p["publication"]["paper_type"] = "short"
        self.assertFalse(classify(p, self.w)["eligible"])

    def test_formal_gate_accepts_audited_full_paper(self):
        self.assertTrue(classify(self.paper(), self.w)["eligible"])

    def test_openreview_exact_main_venue_and_conditional_rejection(self):
        profile = self.w["openreview_profiles"][0]
        note = {"id": "TEST", "content": {"title": {"value": "Test"}, "venueid": {"value": profile["venue_id"] + "/Submission"}}}
        p = openreview_paper(note, profile)
        self.assertEqual(p["publication"]["status"], "submitted")
        note["content"]["venueid"]["value"] = profile["venue_id"]
        note["content"]["venue"] = {"value": "Conditional Accept"}
        self.assertEqual(openreview_paper(note, profile)["publication"]["status"], "submitted")

    def test_handwriting_and_custom_yaml_survive(self):
        p = self.paper()
        gate = classify(p, self.w)
        path = render_paper(p, gate, self.c, self.store)
        before = path.read_text()
        path.write_text(before.replace("paper_id:", "my_rating: 5\npaper_id:") + "\n手写内容：保持中文、公式 $x^2$。\n", encoding="utf-8")
        p["abstract"] += " Updated source text in test."
        render_paper(p, gate, self.c, self.store)
        after = path.read_text()
        self.assertIn("手写内容：保持中文", after)
        self.assertIn("my_rating: 5", after)
        self.assertEqual(after.count(START), 1)
        self.assertTrue(list((self.c["paths"]["data"] / "backups").glob("*.md")))
        snap = path.read_bytes()
        render_paper(p, gate, self.c, self.store)
        self.assertEqual(snap, path.read_bytes())

    def test_unmanaged_note_is_preserved(self):
        path = self.c["paths"]["vault"] / "10_论文笔记/existing.md"
        atomic_write(path, "用户原始笔记")
        with self.assertRaises(ValueError):
            managed_write(path, {}, "new", self.c)
        self.assertEqual(path.read_text(), "用户原始笔记")

    def test_promotion_retains_old_file_and_handwriting(self):
        p = self.paper("submitted")
        old = render_paper(p, classify(p, self.w), self.c, self.store)
        atomic_write(old.parent/"manual-reference.txt", "TEST ONLY")
        old.write_text(old.read_text().replace("paper_id:","my_rating: 5\npaper_id:") + "手写晋升测试 [手工相对链接](manual-reference.txt)", encoding="utf-8")
        p["publication"]["status"] = "published"
        p["publication"]["proceedings_year"] = 2026
        self.store.record(p["id"], "fulltext", "TEST", "success", {"title_zh":"测试", "summary":"测试", "abstract_zh":"测试", "recommendation":"测试", "basis":"SYNTHETIC", "editorial_review":{"status":"TEST ONLY", "notes":[], "terms":"TEST"}})
        brief = old.parent.parent / "00_每日简报" / "TEST.md"
        from paper_obsidian.vault import link
        atomic_write(brief, link(brief, old, "TEST") + "\n[[90_候选与待核验/" + old.stem + "]]\n历史失败事实保留")
        new = render_paper(p, classify(p, self.w), self.c, self.store)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())
        self.assertTrue(new.name.startswith('2026_'))
        self.assertIn("手写晋升测试", new.read_text())
        self.assertIn("my_rating: 5",new.read_text())
        self.assertIn("历史失败事实保留", brief.read_text())
        self.assertIn(new.stem,brief.read_text())
        self.assertFalse((self.c['paths']['vault']/'10_论文笔记').exists())
        self.assertFalse(check_links(self.c["paths"]["vault"])["missing_links"])

    def test_revoked_eligibility_keeps_file_but_removes_formal_status(self):
        p = self.paper()
        self.store.record(p["id"], "fulltext", "TEST", "success", {"title_zh":"测试", "summary":"测试", "abstract_zh":"测试", "recommendation":"测试", "basis":"SYNTHETIC", "editorial_review":{"status":"TEST ONLY", "notes":[], "terms":"TEST"}})
        formal = render_paper(p, classify(p, self.w), self.c, self.store)
        formal.write_text(formal.read_text() + "手写保留", encoding="utf-8")
        pending = render_paper(p, classify(p, {"journals": [], "conferences": []}), self.c, self.store)
        self.assertTrue(formal.exists() and pending.exists())
        self.assertEqual(formal,pending)  # no bilingual delivery: stays one candidate
        self.assertIn("library_status: 待核验", formal.read_text())
        self.assertIn("手写保留", pending.read_text())

    def test_original_and_bilingual_links_chinese_space_paths(self):
        # Test-only byte attachments validate link handling, not real translation success.
        p = self.paper()
        for stage in ("pdf", "translation"):
            f = self.root / ("测试 附件 " + stage + ".pdf")
            f.write_bytes(b"TEST FIXTURE ONLY - NOT A TRANSLATED PDF")
            self.store.record(p["id"], stage, "test", "success", {"path": str(f)})
        note = render_paper(p, classify(p, self.w), self.c, self.store, demo=True)
        self.assertIn("中英双语 PDF", note.read_text())
        self.assertEqual(check_links(self.c["paths"]["vault"])["missing_links"], [])

    def test_windows_chinese_space_path(self):
        if __import__("os").name != "nt":
            self.assertEqual(str(local_path(r"C:\Users\测试 名\Vault")), "/mnt/c/Users/测试 名/Vault")

    def test_pdf_failure_falls_back_and_has_state(self):
        p = self.paper()
        p["pdf_url"] = "https://example.invalid/not-pdf"
        self.store.ingest(p)
        h = HTTP(self.c, {}, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"not a pdf")), sleep=lambda s: None)
        process(self.c, {}, self.store, h, self.w)
        self.assertEqual(self.store.stage(p["id"], "pdf")["status"], "failed")
        self.assertEqual(self.store.stage(p["id"], "fulltext")["status"], "failed")
        self.assertEqual(self.store.stage(p["id"], "abstract")["status"], "needs_configuration")
        h.client.close()

    def test_parse_invalid_pdf_clear_error(self):
        p = self.root / "invalid.pdf"
        p.write_bytes(b"not pdf")
        with self.assertRaises(AnalysisError):
            parse_pdf(p, self.c)

    def test_model_failure_redacts_body_and_key(self):
        secret = "TEST_ONLY_DUMMY_SECRET"
        env = {"API_BASE_URL": "https://example.invalid/v1", "API_KEY": secret, "MODEL": "test"}
        transport = httpx.MockTransport(lambda r: httpx.Response(500, text=secret))
        status, detail = Model(self.c, env, self.store, transport).summarize(self.p)
        self.assertEqual(status, "failed")
        self.assertNotIn(secret, json.dumps(detail))
        self.assertEqual(self.store.used("model_calls"), 1)

    def test_model_cache_avoids_second_paid_request(self):
        # Fixed response is explicitly a unit fixture and never written to production notes.
        fixture = {"title_zh": "测试固定响应", "abstract_zh": "测试摘要", "summary": "这是测试响应，不是实际模型总结", "recommendation": "仅测试"}
        count = []
        def reply(req):
            count.append(1)
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(fixture)}, "finish_reason": "stop"}]})
        env = {"API_BASE_URL": "https://example.invalid/v1", "API_KEY": "TEST-ONLY", "MODEL": "unit-fixture"}
        m = Model(self.c, env, self.store, httpx.MockTransport(reply))
        self.assertEqual(m.summarize(self.p)[0], "success")
        self.assertEqual(m.summarize(self.p)[0], "success")
        self.assertEqual(len(count), 1)

    def test_fake_fulltext_quote_rejected(self):
        d = {"title_zh": "测试", "abstract_zh": "测试", "summary": "测试", "recommendation": "测试",
             "sections": {k: {"text": "测试", "evidence": [{"page": 1, "locator": "Section TEST", "quote": "a fabricated quote that does not exist"}]} for k in FULL_FIELDS}}
        with self.assertRaises(AnalysisError):
            validate_summary(d, {"pages": ["Original test fixture page."]})

    def test_translation_failure_nonblocking_and_temp_secret_removed(self):
        c = copy.deepcopy(self.c)
        exe = self.root / "fixture-engine"
        exe.write_text("test-only placeholder")
        c["translation"].update(enabled=True, executable=str(exe), budget_confirmed=True)
        pdf = self.root / "fixture.pdf"; pdf.write_bytes(b"%PDF-test-only")
        env = {"TRANSLATION_API_BASE_URL": "https://example.invalid", "TRANSLATION_API_KEY": "DUMMY-TEST", "TRANSLATION_MODEL": "TEST"}
        class Failed:
            returncode = 7
        status, detail = translate_pdf(pdf, c, env, self.store, runner=lambda *a, **kw: Failed())
        self.assertEqual(status, "failed")
        self.assertTrue(pdf.exists())
        self.assertFalse(list(c["paths"]["data"].glob("translation-*.toml")))

    def test_http_retry_cache_and_safe_failure(self):
        calls = []
        def reply(req):
            calls.append(1)
            return httpx.Response(503 if len(calls) == 1 else 200, content=b"OK")
        self.c["network"]["retries"] = 1
        h = HTTP(self.c, {}, transport=httpx.MockTransport(reply), sleep=lambda s: None)
        self.assertEqual(h.get("https://example.invalid/data"), b"OK")
        self.assertEqual(h.get("https://example.invalid/data"), b"OK")
        self.assertEqual(len(calls), 2)
        h.client.close()

    def test_lock_prevents_concurrent_runs(self):
        with run_lock(self.c["paths"]["data"]):
            with self.assertRaises(RuntimeError):
                with run_lock(self.c["paths"]["data"]):
                    pass

    def test_resume_window_and_configuration_isolation(self):
        key = query_signature(self.c, "arxiv")
        self.store.set_state(key, {"resume": {"start": "2026-09-19", "end": "2026-10-01", "full": True, "page": 2}})
        start, end, full, state = window(self.c, self.store, "arxiv")
        self.assertEqual((start, end, full), ("2026-09-19", "2026-10-01", True))
        self.c["search"]["keywords"].append("test query change")
        self.assertNotEqual(key, query_signature(self.c, "arxiv"))

    def test_synthetic_whitelist_refused_in_production(self):
        p = self.root / "bad-list.json"
        p.write_text('{"test_only":true,"journals":[],"conferences":[]}')
        with self.assertRaises(ValueError):
            load_whitelist(p)

    def test_proceedings_companion_track_rejected_before_network(self):
        with self.assertRaises(ValueError):
            verify_neurips("https://proceedings.neurips.cc/paper_files/paper/2025/hash/" + "a" * 32 + "-Abstract-Creative_AI_Track.html", None)


if __name__ == "__main__":
    unittest.main()
