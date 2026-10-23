"""Offline tests for paid request boundaries and preservation behavior."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from tools.translate_one import Meter, LimitError


class Response:
    id, model = 'offline-receipt', 'offline-model'
    def __init__(self, content='有效译文', finish='stop'):
        self.choices = [SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)]
    def model_dump(self, **kwargs):
        return dict(id=self.id, model=self.model, choices=[], usage=dict(prompt_tokens=10, completion_tokens=5, total_tokens=15))


class TranslationBoundaryTests(unittest.TestCase):
    def meter(self, folder, **updates):
        cfg = dict(max_output_tokens=100, max_attempts_per_request=2, max_requests=4, max_total_tokens=10000, thinking='disabled')
        cfg.update(updates)
        return Meter(cfg, {'TRANSLATION_API_BASE_URL': 'https://example.invalid'}, Path(folder))
    def client(self, callback):
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=callback)), max_retries=2)
    def test_empty_billed_output_is_retained_and_not_retried(self):
        with TemporaryDirectory() as tmp:
            m = self.meter(tmp)
            c = self.client(lambda **kw: Response('', 'length'))
            m.wrap(c)
            with self.assertRaises(ValueError): c.chat.completions.create(messages=[])
            with self.assertRaises(LimitError): c.chat.completions.create(messages=[])
            self.assertEqual(m.network, 1)
            self.assertEqual(m.usage()['total_tokens'], 15)
            self.assertEqual(m.rows[0]['status'], 'invalid')
            self.assertEqual(len(list((Path(tmp)/'receipts').glob('*.json'))), 1)
    def test_hard_budget_stops_before_network(self):
        with TemporaryDirectory() as tmp:
            m = self.meter(tmp, max_total_tokens=1)
            c = self.client(lambda **kw: self.fail('Network must not run'))
            m.wrap(c)
            with self.assertRaises(LimitError): c.chat.completions.create(messages=[])
            self.assertEqual(m.network, 0)
    def test_unknown_timeout_is_not_automatically_replayed(self):
        with TemporaryDirectory() as tmp:
            m = self.meter(tmp)
            def timeout(**kw): raise TimeoutError()
            c = self.client(timeout)
            m.wrap(c)
            with self.assertRaises(TimeoutError): c.chat.completions.create(messages=[])
            restored = self.meter(tmp)
            c2 = self.client(lambda **kw: self.fail('Unknown outcomes must not replay'))
            restored.wrap(c2)
            with self.assertRaises(LimitError): c2.chat.completions.create(messages=[])
            self.assertEqual(restored.network, 0)
    def test_offline_mode_blocks_all_requests(self):
        with TemporaryDirectory() as tmp:
            m = self.meter(tmp)
            m.offline = True
            c = self.client(lambda **kw: self.fail('Offline request'))
            m.wrap(c)
            with self.assertRaises(LimitError): c.chat.completions.create(messages=[])
            self.assertEqual(m.rows, [])


if __name__ == '__main__': unittest.main()
