"""Boundary checks for real-source diagnostics and the recent-paper acceptance path."""
import copy
import csv
import json
import tempfile
import unittest
import re
from unittest.mock import patch
from pathlib import Path
import httpx
from paper_obsidian.core import Store, load_config, now, merge_metadata
from paper_obsidian.eligibility import import_csv, validate_journal
from paper_obsidian.recent import case_config, load_batch, run_recent
from paper_obsidian.sources import HTTP, FetchError
from paper_obsidian.proceedings import verify_cvpr
from paper_obsidian.analysis import parse_pdf


class RecentTests(unittest.TestCase):
    def test_cvf_workshop_url_rejected_before_network(self):
        class NoNetwork:
            def get(self,url): raise AssertionError("unexpected network")
        with self.assertRaises(ValueError):
            verify_cvpr("https://openaccess.thecvf.com/content/CVPR2026W/html/TEST_CVPR_2026_paper.html",NoNetwork())

    def test_cvf_requires_long_main_paper_and_retains_month_precision(self):
        html='<meta name="citation_title" content="TEST ONLY"><meta name="citation_conference_title" content="Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition"><meta name="citation_firstpage" content="1"><meta name="citation_lastpage" content="10"><meta name="citation_publication_date" content="2026"><meta name="citation_pdf_url" content="https://openaccess.thecvf.com/content/CVPR2026/papers/TEST_CVPR_2026_paper.pdf"><p>identical to the accepted versions</p><div id="abstract">TEST ONLY</div><pre>month={June}</pre>'
        class Cached:
            def get(self,url): return html.encode()
        p=verify_cvpr("https://openaccess.thecvf.com/content/CVPR2026/html/TEST_CVPR_2026_paper.html",Cached())
        self.assertEqual(p["date"],"2026-06")
        self.assertEqual(p["publication"]["status"],"accepted")
        self.assertEqual(p["publication"]["page_range"],"1-10")

    def test_layout_compaction_preserves_every_non_whitespace_character(self):
        class Page:
            def extract_text(self,**kw): return ("Alpha          12.3          beta\n"*700)
        class Reader:
            is_encrypted=False
            pages=[Page(),Page()]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"TEST.pdf"; path.write_bytes(b"%PDF-TEST-ONLY")
            c=case_config()
            with patch("paper_obsidian.analysis.PdfReader",return_value=Reader()):
                result=parse_pdf(path,c)
            self.assertEqual(result["page_count"],2)
            self.assertTrue(result["non_whitespace_preserved"])
            compact=re.sub(r"\[PDF PAGE \d+\]", "", result["text"])
            self.assertEqual(re.sub(r"\s","",compact),re.sub(r"\s","","".join(result["pages"])))

    def test_cached_batch_has_hard_limit_before_ingestion(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)
            (p/"discovery-90.json").write_text(json.dumps({"papers":[{}]*31}))
            (p/"official-verification.json").write_text("[]")
            with self.assertRaises(ValueError): load_batch(p)

    def test_replay_and_paid_request_are_mutually_exclusive(self):
        with self.assertRaises(ValueError): run_recent(summarize=True,replay=True)

    def test_recent_uses_existing_single_model_settings(self):
        c=case_config(); old=load_config("config.test.toml")
        self.assertEqual(c["limits"]["model_calls_per_day"],1)
        self.assertEqual(c["limits"]["input_chars_per_call"],old["limits"]["input_chars_per_call"])
        self.assertEqual(c["model"]["thinking"],"disabled")
        self.assertFalse(c["translation"]["enabled"])
        self.assertFalse(c["scope"]["allow_fetch"])

    def test_unknown_metadata_cannot_erase_official_publication_or_dates(self):
        p={"source":"TEST","date":"2026-04-23","venue":"NeurIPS","publication":{"status":"published","main_track":True},"dates":{"official_citation_publication_date":"2026-04-23"}}
        q={"source":"TEST_OTHER","date":"2025-01-01","publication":{"status":"unknown"},"dates":{"preprint_published":"2025-06-05"}}
        r=merge_metadata(p,q)
        self.assertEqual(r["publication"]["status"],"published")
        self.assertEqual(r["date"],"2026-04-23")
        self.assertIn("official_citation_publication_date",r["dates"])
        self.assertIn("preprint_published",r["dates"])

    def test_http_auth_and_quota_errors_are_separate_and_secret_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            c=load_config("tests/fixtures/config.toml"); c["paths"]["data"]=Path(tmp); c["network"]["retries"]=0
            for code,category in ((401,"authentication"),(429,"quota_or_rate_limit"),(403,"access_denied")):
                h=HTTP(c,{"OPENALEX_API_KEY":"TEST_SECRET"},transport=httpx.MockTransport(lambda r:httpx.Response(code,json={"error":"TEST_SECRET"})),sleep=lambda _:None)
                with self.assertRaises(FetchError): h.get("https://api.openalex.org/works")
                self.assertEqual(h.events[-1]["category"],category)
                self.assertNotIn("TEST_SECRET",json.dumps(h.events))
                h.client.close()

    def journal(self):
        return {"name":"SYNTHETIC TEST ONLY","issns":["0162-8828"],"sci_index":"SCIE","sci_year":"2025","sci_source":"https://mjl.clarivate.com/", "sci_checked_at":now(),"sci_evidence_file":"TEST_ONLY.txt",
                "cas_year":"2025","cas_source":"https://www.fenqubiao.com/","cas_checked_at":now(),"cas_evidence_file":"TEST_ONLY.txt","cas_major_zone":1,"cas_major_category":"TEST ONLY","cas_partition_scope":"major","cas_edition":"TEST ONLY","coverage_from":"2025-01-01","coverage_to":"2025-12-31"}

    def test_jcr_scope_and_invalid_issn_are_rejected(self):
        r=self.journal(); validate_journal(r)
        for field,value in (("cas_partition_scope","JCR Q1"),("issns",["1234-5678"]),("sci_evidence_file","")):
            d=dict(r); d[field]=value
            with self.assertRaises(ValueError): validate_journal(d)

    def test_historical_partition_requires_institutional_policy_for_new_year(self):
        r=self.journal(); r["coverage_to"]="2026-12-31"
        with self.assertRaises(ValueError): validate_journal(r)

    def test_missing_evidence_import_is_atomic(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); target=p/"venues.json"; old='{"journals":[],"conferences":[]}'
            target.write_text(old)
            r=self.journal(); r["issns"]="0162-8828"
            source=p/"TEST_ONLY.csv"
            with source.open("w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=list(r)); w.writeheader(); w.writerow(r)
            with self.assertRaises(ValueError): import_csv("journals",source,target)
            self.assertEqual(target.read_text(),old)

    def test_import_freezes_independent_evidence_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); (p/"TEST_ONLY.txt").write_text("Synthetic test evidence, not real journal data")
            target=p/"venues.json"; target.write_text('{"journals":[],"conferences":[]}')
            r=self.journal(); r["issns"]="0162-8828"
            source=p/"TEST_ONLY.csv"
            with source.open("w",newline="",encoding="utf-8") as f:
                w=csv.DictWriter(f,fieldnames=list(r)); w.writeheader(); w.writerow(r)
            self.assertEqual(import_csv("journals",source,target),1)
            saved=json.loads(target.read_text())["journals"][0]
            self.assertEqual(len(saved["sci_evidence_sha256"]),64)
            self.assertEqual(len(saved["cas_evidence_sha256"]),64)

if __name__=="__main__": unittest.main()
