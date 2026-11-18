"""月末节省工时结算规则的标准库单测（无第三方依赖）：

python3 test-artifacts/gb-1006/tests/test_settlement_rules.py
"""
import os
import sys
import unittest

_BACKEND = os.environ.get(
    'PICK_BACKEND',
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'backend')),
)
sys.path.insert(0, _BACKEND)

from src.services.settlement_rules import (  # noqa: E402
    Ledger, build_rate_snapshot, resolve_rate_job, saved_hours,
    SOURCE_INITIAL, SOURCE_CORRECTION, SOURCE_LATE,
)


class FakeJob:
    def __init__(self, jid, team, mean, approved=True, status='completed', name=None):
        self.id = jid
        self.name = name or f'job-{jid}'
        self.params = {'team': team, 'confidence': '0.95'}
        self.approved = approved
        self.status = status
        self.result = {'metrics': [
            {'label': '配对 p 值', 'value': 0.01},
            {'label': '平均节省', 'value': mean},
        ]}


class RateSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.jobs = [
            FakeJob(1, '全部', 20.0),
            FakeJob(2, '一组', 30.0),
            FakeJob(3, '一组', 33.0),                      # 同口径更新的批准结论
            FakeJob(4, '二组', 25.0, approved=False),     # 未批准不可采用
            FakeJob(5, '二组', 25.0, status='failed'),    # 未完成不可采用
        ]

    def test_team_specific_wins_over_overall_and_latest_wins(self):
        job = resolve_rate_job('一组', self.jobs)
        self.assertEqual(job.id, 3)
        snap = build_rate_snapshot('一组', self.jobs)
        self.assertEqual(snap['job_id'], 3)
        self.assertEqual(snap['mean_saved_seconds'], 33.0)
        self.assertEqual(snap['basis'], '班组复测')
        self.assertAlmostEqual(snap['hours_per_pick'], 33.0 / 3600, places=10)

    def test_overall_fallback_when_no_team_job(self):
        snap = build_rate_snapshot('三组', self.jobs)
        self.assertEqual(snap['job_id'], 1)
        self.assertEqual(snap['basis'], '全部口径')

    def test_no_approved_job_raises(self):
        with self.assertRaises(ValueError):
            build_rate_snapshot('四组', [FakeJob(9, '全部', 10.0, approved=False)])

    def test_saved_hours_uses_locked_rate(self):
        snap = build_rate_snapshot('一组', self.jobs)
        self.assertAlmostEqual(saved_hours(360, snap), 360 * 33.0 / 3600, places=6)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        jobs = [FakeJob(1, '全部', 36.0), FakeJob(2, '一组', 18.0)]
        self.rate_all = build_rate_snapshot('二组', jobs)   # -> 全部口径，0.01 小时/单
        self.rate_team = build_rate_snapshot('一组', jobs)  # -> 班组复测，0.005 小时/单
        self.ledger = Ledger()

    def test_register_once_and_hours_calculation(self):
        posting, created = self.ledger.register('2026-09', '二组', '2026-09-30', 100, self.rate_all)
        self.assertTrue(created)
        self.assertEqual(posting.source, SOURCE_INITIAL)
        self.assertAlmostEqual(posting.saved_hours, 1.0, places=6)
        with self.assertRaises(ValueError):
            self.ledger.register('2026-09', '二组', '2026-09-30', 50, self.rate_all)

    def test_open_month_correction_recomputes_in_place_keeps_rate(self):
        posting, _ = self.ledger.register('2026-10', '一组', '2026-10-01', 200, self.rate_team)
        entry = self.ledger.entries['2026-10|一组|2026-10-01']
        corrected, changed = self.ledger.correct_open(entry, 260)
        self.assertTrue(changed)
        self.assertEqual(corrected.source, SOURCE_CORRECTION)
        self.assertEqual(corrected.volume, 260)
        self.assertAlmostEqual(corrected.saved_hours, 260 * 0.005, places=6)
        self.assertEqual(corrected.adjustment['delta_volume'], 60)
        self.assertAlmostEqual(corrected.adjustment['delta_hours'], 0.3, places=6)
        # 仍是同一条分录、同一个月份，未新增期间
        self.assertEqual(len([p for p in self.ledger.postings if p.entry_key == entry.key]), 1)
        self.assertEqual(self.ledger.month_totals('2026-10')['postings'], 1)
        # 锁定的折算值不随新结论变化
        self.assertEqual(corrected.rate_snapshot['job_id'], self.rate_team['job_id'])

    def test_closed_month_keeps_original_and_carries_delta_to_first_open_month(self):
        original, _ = self.ledger.register('2026-09', '一组', '2026-09-29', 200, self.rate_team)
        entry = self.ledger.entries['2026-09|一组|2026-09-29']
        # 9 月关账后更正为 236 单：原数保留
        carried, created = self.ledger.carry_to_open(
            entry, '2026-10', 36, source=SOURCE_CORRECTION, note='关账后补记')
        self.assertTrue(created)
        self.assertEqual(carried.source, SOURCE_CORRECTION)
        self.assertEqual(carried.month, '2026-10')
        self.assertEqual(carried.volume, 36)                       # 只入账差额
        self.assertAlmostEqual(carried.saved_hours, 36 * 0.005, places=6)
        self.assertEqual(carried.adjustment['origin_month'], '2026-09')
        # 原 9 月分录原数与折算值原样保留
        self.assertEqual(original.volume, 200)
        self.assertAlmostEqual(original.saved_hours, 1.0, places=6)
        self.assertEqual(self.ledger.month_totals('2026-09')['volume'], 200)

    def test_late_volume_uses_locked_original_rate_in_open_month(self):
        # 迟到的 9 月班次，登记到首个未关账月份 10 月
        posting, _ = self.ledger.register('2026-10', '三组', '2026-09-27', 420, self.rate_all, late=True)
        self.assertEqual(posting.source, SOURCE_LATE)
        self.assertEqual(posting.month, '2026-10')
        self.assertAlmostEqual(posting.saved_hours, 420 * 0.01, places=6)

    def test_one_change_one_period_duplicate_carry_rejected(self):
        self.ledger.register('2026-09', '一组', '2026-09-29', 200, self.rate_team)
        entry = self.ledger.entries['2026-09|一组|2026-09-29']
        self.ledger.carry_to_open(entry, '2026-10', 36, source=SOURCE_CORRECTION, note='first')
        with self.assertRaises(ValueError):
            self.ledger.carry_to_open(entry, '2026-10', 36, source=SOURCE_CORRECTION, note='retry')

    def test_idempotency_key_replay_does_not_double_post(self):
        first, created = self.ledger.register('2026-10', '一组', '2026-10-02', 100, self.rate_team,
                                              idempotency_key='req-abc-1')
        replay, again = self.ledger.register('2026-10', '一组', '2026-10-02', 100, self.rate_team,
                                             idempotency_key='req-abc-1')
        self.assertFalse(again)
        self.assertIs(replay, first)
        self.assertEqual(self.ledger.month_totals('2026-10')['postings'], 1)
        # 结转重试同样回放
        entry = self.ledger.entries['2026-10|一组|2026-10-02']
        carried, _ = self.ledger.carry_to_open(entry, '2026-11', 20,
                                               source=SOURCE_CORRECTION, note='x',
                                               idempotency_key='req-def-2')
        replay2, again2 = self.ledger.carry_to_open(entry, '2026-11', 20,
                                                    source=SOURCE_CORRECTION, note='x',
                                                    idempotency_key='req-def-2')
        self.assertFalse(again2)
        self.assertIs(replay2, carried)
        self.assertEqual(len([p for p in self.ledger.postings if p.month == '2026-11']), 1)

    def test_negative_delta_allowed_for_downward_correction(self):
        self.ledger.register('2026-09', '一组', '2026-09-29', 200, self.rate_team)
        entry = self.ledger.entries['2026-09|一组|2026-09-29']
        carried, _ = self.ledger.carry_to_open(entry, '2026-10', -40,
                                               source=SOURCE_CORRECTION, note='多记冲回')
        self.assertEqual(carried.volume, -40)
        self.assertAlmostEqual(carried.saved_hours, -40 * 0.005, places=6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
