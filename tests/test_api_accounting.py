"""Isolated fixtures only: no live API, models, translation or production writes."""
import copy
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import httpx

from paper_obsidian.api_accounting import (RunAccounting, changes, estimate, guarded,
    official, query_balance, receipt_rows, render, source_id)
from paper_obsidian.core import Store, atomic_write, load_config, now
from paper_obsidian.daily_model import Client


def payload(cny='10.123456', usd=None):
    values = [('CNY', cny)]+([('USD', usd)] if usd is not None else [])
    return {'is_available': True, 'balance_infos': [dict(currency=k, total_balance=v,
        granted_balance='0', topped_up_balance=v) for k, v in values]}


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = load_config("tests/fixtures/config.toml")
        self.c['paths'].update(data=self.root/'data', vault=self.root/'vault')
        self.s = Store(self.c['paths']['data']); self.addCleanup(self.s.db.close)
        self.env = dict(
            API_BASE_URL='https://api.deepseek.com/v1',
            API_KEY='fixture-secret',
            MODEL='fixture-model',
            TRANSLATION_API_BASE_URL='https://api.deepseek.com',
            TRANSLATION_API_KEY='fixture-secret',
            TRANSLATION_MODEL='fixture-model')

    def accounting(self, run_id='1', responses=None):
        values = iter(responses or [payload(), payload('9.123455')])
        return RunAccounting(self.c, self.env, self.s, run_id, now(),
            httpx.MockTransport(lambda _: httpx.Response(200, json=next(values))))

    def test_decimal_decline_and_increase_multi_currency(self):
        a = self.accounting(responses=[payload('10.123456', '3'), payload('9.123455', '4.000001')])
        a.begin(); report = {'model_requests': 0}; a.finish(report)
        d = a.run['sources'][0]['changes']
        self.assertEqual([(x['direction'], x['amount']) for x in d], [('减少', '1.000001'), ('增加', '1.000001')])
        body = '\n'.join(render(a.run))
        self.assertIn('¥9.123455 / $4.000001', render(a.run)[0]); self.assertIn('统计完整',render(a.run)[1]); self.assertIn('@ ',render(a.run)[0])
        self.assertNotIn('负数', body)
        self.assertNotIn('fixture-secret', a.path.read_text())
        missing = copy.deepcopy(a.run['sources'][0]['after'])
        missing['balance_infos'] = missing['balance_infos'][:1]
        diff = changes(a.run['sources'][0]['before'], missing)
        self.assertIsNone(diff[1]['amount'])
        self.assertEqual(diff[1]['direction'], '不可用')

    def test_before_and_after_once_same_credential_grouped(self):
        calls = []
        a = self.accounting()
        a.transport = httpx.MockTransport(lambda r: calls.append(r.url) or httpx.Response(200, json=payload()))
        a.begin(); a.begin(); a.finish({'model_requests': 0}); a.finish({'model_requests': 0})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(a.run['sources']), 1)
        self.assertEqual(len(a.run['sources'][0]['labels']), 2)

    def test_different_keys_never_assert_distinct_accounts_or_sum_balances(self):
        self.env['TRANSLATION_API_KEY'] = 'other-fixture'
        a = self.accounting(responses=[payload(), payload(), payload('9'), payload('9')])
        a.begin(); a.finish({'model_requests': 0})
        self.assertEqual(len(a.run['sources']), 2)
        self.assertEqual(render(a.run)[0].count('¥9'),2)

    def test_proxy_credential_never_sent_and_endpoint_validation(self):
        for base in ('https://api.deepseek.com.evil.test', 'http://api.deepseek.com',
                     'https://api.deepseek.com@evil.test', 'https://api.deepseek.com:444',
                     'https://api.deepseek.com/?redirect=bad', 'https://proxy.test'):
            self.assertFalse(official(base))
        self.env['TRANSLATION_API_BASE_URL'] = 'https://proxy.test'
        self.env['TRANSLATION_API_KEY'] = 'proxy-secret'
        calls = []
        a = self.accounting()
        def response(r):
            self.assertNotIn('proxy-secret', str(r.headers))
            calls.append(r)
            return httpx.Response(200, json=payload())
        a.transport = httpx.MockTransport(response)
        a.begin(); a.finish({'model_requests': 0})
        self.assertEqual(len(calls), 2)
        self.assertEqual(a.run['sources'][1]['after']['status'], 'not_official')

    def test_http_invalid_credentials_failures_and_redirect_no_retry(self):
        for status in (401, 403, 429, 500, 302):
            calls = []
            transport = httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(status, json={'secret': 'must-not-log'}))
            snap = query_balance('fixture', transport)
            self.assertEqual(snap['status'], 'http_error')
            self.assertEqual(len(calls), 1)
            self.assertNotIn('must-not-log', json.dumps(snap))

    def test_timeout_and_invalid_schema(self):
        def timeout(request): raise httpx.ReadTimeout('secret response content')
        self.assertEqual(query_balance('fixture', httpx.MockTransport(timeout))['status'], 'timeout')
        for raw in ({}, {'is_available': True, 'balance_infos': []}, payload('NaN')):
            snap = query_balance('fixture', httpx.MockTransport(lambda _: httpx.Response(200, json=raw)))
            self.assertEqual(snap['status'], 'query_failed')

    def test_one_sided_missing_uses_historical_time_and_no_zero_delta(self):
        a = self.accounting(); a.begin()
        stamp = a.run['sources'][0]['before']['queried_at']
        a.transport = httpx.MockTransport(lambda _: httpx.Response(401))
        a.finish({'model_requests': 0})
        self.assertEqual(a.run['sources'][0]['changes'], [])
        body = '\n'.join(render(a.run))
        self.assertIn('历史快照，查询失败', body)
        self.assertEqual(a.run['sources'][0]['historical']['queried_at'], stamp)
        self.assertEqual(changes(None, a.run['sources'][0]['before']), [])

    def row(self, **updates):
        row = dict(run_id='1', request_id='unique', source_id=source_id(self.env),
                   requested_model='fixture-model', response_model='fixture-model', kind='summary', status='success',
                   started='2026-10-21T12:00:00+08:00', ended='2026-10-21T12:01:00+08:00',
                   usage=dict(prompt_tokens=100, prompt_cache_hit_tokens=40, prompt_cache_miss_tokens=60, completion_tokens=20))
        row.update(updates)
        return row

    def prices(self):
        self.c['api_accounting'] = {'prices': [dict(source_id=source_id(self.env), model='fixture-model', currency='CNY',
            cache_hit='1', cache_miss='2', output='3', source='https://official.invalid/fixture', checked_at='2026-10-21',
            valid_from='2026-10-21T12:00:00+08:00', valid_until='2026-10-21T13:00:00+08:00')]}

    def test_estimate_cache_split_exact_decimal_and_price_evidence(self):
        self.prices()
        result = estimate(self.c, {'model_requests': 1}, [self.row()])
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['amounts'], {'CNY': '0.00022'})
        self.assertIn('checked_at', result['requests'][0]['price'])

    def test_missing_usage_unknown_price_and_uncertain_not_free(self):
        for row in (self.row(usage={}), self.row(status='uncertain'), self.row()):
            result = estimate(self.c, {'model_requests': 1}, [row])
            self.assertEqual(result['status'], 'incomplete'); self.assertEqual(result['amounts'], {})
        self.prices()
        row = self.row(ended='2026-10-21T13:01:00+08:00')
        self.assertEqual(estimate(self.c, {'model_requests': 1}, [row])['status'], 'incomplete')

    def test_partial_translation_never_exposed_as_total(self):
        self.prices()
        for launch in ({'status': 'running'}, {'status': 'finished', 'network_requests': 1}):
            result = estimate(self.c, {'model_requests': 1, 'translation_launches': [launch]}, [self.row()])
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['amounts'], {})
            self.assertEqual(result['known_subtotals'], {'CNY': '0.00022'})

    def test_request_dedup_and_run_filter_excludes_cache_history(self):
        atomic_write(self.c['paths']['data']/'daily-api'/'one'/'attempts.json', json.dumps([self.row(), self.row(), self.row(run_id='old', request_id='old')]))
        self.assertEqual(len(receipt_rows(self.c, '1')), 1)
        self.assertEqual(receipt_rows(self.c, '2'), [])
        self.assertEqual(estimate(self.c, {'model_requests': 0}, [])['status'], 'complete')

    def test_model_cache_rerun_never_reattributes_historical_usage(self):
        def response(_):
            return httpx.Response(200, json={'model': 'fixture-model', 'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok": true}'}}], 'usage': self.row()['usage']})
        client = Client(self.c, self.env, self.s, httpx.MockTransport(response)); client.run_id = '1'
        client.call('draft', 'system', {})
        client2 = Client(self.c, self.env, self.s, httpx.MockTransport(lambda _: self.fail('cache must not call'))); client2.run_id = '2'
        client2.call('draft', 'system', {})
        self.assertEqual(len(receipt_rows(self.c, '1')), 1)
        self.assertEqual(receipt_rows(self.c, '2'), [])

    def daily_patches(self, stack, failure=None):
        for name in ('backup', 'archive_legacy_attachments', 'apply_capacity'):
            stack.enter_context(patch('paper_obsidian.daily.'+name, return_value={}))
        stack.enter_context(patch('paper_obsidian.daily.load_whitelist', return_value={'journals': []}))
        stack.enter_context(patch('paper_obsidian.daily.search', side_effect=failure))
        stack.enter_context(patch('paper_obsidian.api_accounting.query_balance', return_value={'status': 'timeout', 'queried_at': now()}))

    def test_daily_processing_error_retained_and_end_snapshot_saved(self):
        from paper_obsidian.daily import run_daily
        with ExitStack() as stack:
            self.daily_patches(stack, ValueError('original processing error'))
            result = run_daily(self.c, self.env)
        self.assertEqual(result['failures'][0]['error'], 'ValueError')
        self.assertEqual(result['api_accounting']['sources'][0]['after']['status'], 'timeout')
        self.assertEqual(result['api_accounting']['status'], 'partial')

    def test_ancillary_exception_cannot_mask_processing_error(self):
        from paper_obsidian.daily import run_daily
        with ExitStack() as stack:
            self.daily_patches(stack, ValueError('original'))
            stack.enter_context(patch.object(RunAccounting, 'finish', side_effect=OSError('balance write failed')))
            result = run_daily(self.c, self.env)
        self.assertEqual(result['failures'][0]['error'], 'ValueError')
        self.assertIn('OSError', result['accounting_warnings'])
        self.assertEqual(result['api_accounting']['estimate']['status'], 'incomplete')

    def test_unexpected_termination_no_fabricated_end_even_after_recovery(self):
        from paper_obsidian.daily import run_daily
        with ExitStack() as stack:
            self.daily_patches(stack, KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt): run_daily(self.c, self.env)
        receipt = json.loads(next((self.c['paths']['data']/'daily-runs').glob('*.api.json')).read_text())
        self.assertIsNone(receipt['ended']); self.assertEqual(receipt['status'], 'running')
        self.assertNotIn('after', receipt['sources'][0])
        with ExitStack() as stack:
            self.daily_patches(stack)
            run_daily(self.c, self.env)
        old = self.s.db.execute('SELECT ended,report FROM runs WHERE id=1').fetchone()
        self.assertIsNone(old['ended']); self.assertEqual(json.loads(old['report'])['status'], 'interrupted')

    def test_offline_replay_and_render_zero_network_and_original_query_time(self):
        from paper_obsidian.briefs import daily_brief
        from paper_obsidian.daily import run_daily
        a = self.accounting(); a.begin(); report = {'model_requests': 0}; a.finish(report)
        report.update(started=now(), ended=now(), run_id='1', delivered=[])
        path = Path(daily_brief(self.c, self.s, report))
        stamp = a.run['sources'][0]['after']['queried_at']
        with patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
             patch('httpx.Client.send', side_effect=AssertionError('HTTP forbidden')), \
             patch('paper_obsidian.api_accounting.query_balance', side_effect=AssertionError('balance forbidden')), \
             patch('subprocess.run', side_effect=AssertionError('worker forbidden')), \
             patch('paper_obsidian.daily.Client', side_effect=AssertionError('model forbidden')):
            run_daily(self.c, self.env, offline=True)
        body = path.read_text()
        self.assertIn('¥9.123455', body)
        from paper_obsidian.library import day_of
        saved = self.s.get_state('reading:brief:'+day_of(report['ended']))['api_accounting']
        self.assertEqual(saved['sources'][0]['after']['queried_at'], stamp)

    def test_same_day_rerun_keeps_deliveries_and_replaces_only_run_header(self):
        from paper_obsidian.briefs import daily_brief
        from paper_obsidian.core import digest
        from paper_obsidian.library import reading_path
        from paper_obsidian.vault import managed_write
        self.s.ingest({'source': 'TEST', 'aliases': ['paper-a'], 'title': 'Existing result',
                       'venue': 'CVPR', 'publication': {'status': 'accepted', 'proceedings_year': 2026}})
        p = self.s.get(digest('paper-a')[:20]); note = reading_path(p, self.c, self.s, True)
        managed_write(note, {'paper_id': p['id']}, 'Existing summary', self.c)
        pdf = note.parent/'中英对照.pdf'; atomic_write(pdf, b'original-pdf')
        delivery = {'note': str(note), 'bilingual': str(pdf), 'completed_at': now(), 'delivery_kind': '新论文'}
        self.s.record(p['id'], 'delivery', 'v', 'success', delivery)
        original = note.read_bytes()
        for rid in ('1', '2'):
            a = self.accounting(rid); a.begin(); report = {'model_requests': 0}; a.finish(report)
            report.update(started=now(), ended=now(), run_id=rid, delivered=[])
            path = Path(daily_brief(self.c, self.s, report))
        body = path.read_text()
        from paper_obsidian.library import day_of
        self.assertEqual(self.s.get_state('reading:brief:'+day_of(report['ended']))['api_accounting']['run_id'], '2')
        self.assertNotIn('本次 daily', body)
        self.assertIn('新交付 1 篇', body)
        self.assertEqual(note.read_bytes(), original); self.assertEqual(pdf.read_bytes(), b'original-pdf')
        self.assertTrue((self.c['paths']['data']/'daily-runs/1.api.json').exists())
        self.assertLess(body.index('最新账户余额：'), body.index('今日新交付'))

    def test_translation_receipts_bound_to_run_and_retained_on_timeout(self):
        from tools.translate_one import Meter
        from types import SimpleNamespace
        folder = self.c['paths']['data']/'daily-translation/fixture'; folder.mkdir(parents=True)
        cfg = dict(run_id='translation-run', thinking='disabled', max_output_tokens=100,
                   max_attempts_per_request=2, max_requests=4, max_total_tokens=10000)
        def timeout(**kw): raise TimeoutError('unknown billing')
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=timeout)))
        meter = Meter(cfg, self.env, folder); meter.wrap(client)
        with self.assertRaises(TimeoutError): client.chat.completions.create(model='fixture-model', messages=[])
        rows = receipt_rows(self.c, 'translation-run')
        self.assertEqual(len(rows), 1); self.assertEqual(rows[0]['kind'], 'translation')
        self.assertEqual(rows[0]['status'], 'uncertain'); self.assertEqual(rows[0]['source_id'], source_id(self.env))
        self.assertEqual(receipt_rows(self.c, 'later-run'), [])


if __name__ == '__main__': unittest.main()
