"""结算服务的 ORM 集成测试：SQLite 内存库验证持久化与唯一约束。

PICK_BACKEND 指向 workspace/backend；依赖 SQLAlchemy 2.x、pydantic v2、FastAPI：
  python3 test-artifacts/gb-1006/tests/test_settlement_db.py
MySQL 行锁在 SQLite 上由服务层降级为普通查询，约束与事务分支保持一致。
服务层在冲突时回滚整个事务（与真实路由每请求独立 session 一致），
因此预期冲突的断言在新 session 中验证。
"""
import os
import sys
import time
import unittest

_BACKEND = os.environ.get(
    'PICK_BACKEND',
    os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..', 'backend')),
)
sys.path.insert(0, _BACKEND)
os.environ.setdefault('DATABASE_URL', 'sqlite://')
os.environ.setdefault('JWT_SECRET', 'test-secret')
os.environ.setdefault('REDIS_URL', 'redis://unused:6379/0')

from fastapi import HTTPException  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as database  # noqa: E402
from src.models import Job, AccountingPeriod, ShiftEntry, Posting  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.services import settlement  # noqa: E402
from src.services.settlement_rules import saved_hours  # noqa: E402

engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
database.engine = engine
database.Session = sessionmaker(engine, expire_on_commit=False)


class Actor:
    def __init__(self, email='analyst@demo.local'):
        self.email = email


class VolumePayload:
    def __init__(self, team, shift_date, volume, *, month=None, late=False, note='', idempotency_key=None):
        self.team = team
        self.shift_date = shift_date
        self.volume = volume
        self.month = month
        self.late = late
        self.note = note
        self.idempotency_key = idempotency_key


class SettlementDBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)

    def setUp(self):
        self.actor = Actor()
        self.admin = Actor('admin@demo.local')
        with database.Session() as db:
            db.add(Job(name='job-all', dataset_id=1, params={'team': '全部', 'confidence': '0.95'},
                       status='completed', approved=True, created_by=1, created_at=time.time(),
                       result={'metrics': [{'label': '平均节省', 'value': 36.0}]}))
            db.add(Job(name='job-team1', dataset_id=1, params={'team': '一组', 'confidence': '0.95'},
                       status='completed', approved=True, created_by=1, created_at=time.time(),
                       result={'metrics': [{'label': '平均节省', 'value': 18.0}]}))
            db.commit()
        self.db = database.Session()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)

    def _fresh(self):
        self.db.close()
        self.db = database.Session()
        return self.db

    def test_register_locks_rate_snapshot_and_unique_shift(self):
        posting, created = settlement.register_volume(
            self.db, VolumePayload('一组', '2026-10-01', 200), self.actor)
        self.assertTrue(created)
        self.assertEqual(posting['rate_snapshot']['job_id'], 2)  # 班组复测优先于全部口径
        self.assertAlmostEqual(posting['saved_hours'], saved_hours(200, posting['rate_snapshot']), places=6)
        self.db.commit()
        with self.assertRaises(HTTPException) as ctx:
            settlement.register_volume(
                database.Session(), VolumePayload('一组', '2026-10-01', 99), self.actor)
        self.assertEqual(ctx.exception.status_code, 409)
        with database.Session() as check:
            self.assertEqual(check.scalar(select(Posting).where(Posting.team == '一组')).volume, 200)

    def test_idempotency_replay_returns_same_posting(self):
        first, created = settlement.register_volume(
            self.db, VolumePayload('二组', '2026-10-02', 100, idempotency_key='key-1'), self.actor)
        replay, again = settlement.register_volume(
            self.db, VolumePayload('二组', '2026-10-02', 100, idempotency_key='key-1'), self.actor)
        self.assertFalse(again)
        self.assertEqual(first['id'], replay['id'])
        self.assertEqual(self.db.query(Posting).count(), 1)

    def test_open_month_correction_recomputes_in_place(self):
        posting, _ = settlement.register_volume(
            self.db, VolumePayload('一组', '2026-10-03', 200), self.actor)
        entry_id = posting['entry_id']
        corrected, changed = settlement.correct_volume(self.db, entry_id, 260, self.actor)
        self.assertTrue(changed)
        self.assertEqual(corrected['volume'], 260)
        self.assertEqual(corrected['source'], 'correction')
        self.assertEqual(self.db.query(Posting).count(), 1)  # 就地重算，无新分录
        entry = self.db.get(ShiftEntry, entry_id)
        self.assertEqual(entry.volume, 260)
        self.assertEqual(entry.rate_snapshot['job_id'], 2)  # 折算值未换

    def test_closed_month_original_preserved_delta_carries_to_open_month(self):
        original, _ = settlement.register_volume(
            self.db, VolumePayload('一组', '2026-09-29', 200), self.actor)
        settlement.ensure_period(self.db, '2026-10')
        self.db.flush()
        summary, closed_now = settlement.close_period(self.db, '2026-09', self.admin)
        self.assertTrue(closed_now)
        self.assertTrue(summary['closed'])
        # 重复关账幂等
        summary2, again = settlement.close_period(self.db, '2026-09', self.admin)
        self.assertFalse(again)
        # 关账后更正为 236：原数保留
        carried, created = settlement.correct_volume(self.db, original['entry_id'], 236, self.actor)
        self.assertTrue(created)
        self.assertEqual(carried['period_id'], self.db.scalar(
            select(AccountingPeriod.id).where(AccountingPeriod.month == '2026-10')))
        self.assertEqual(carried['volume'], 36)
        self.assertEqual(carried['rate_snapshot']['job_id'], 2)  # 沿用原折算值
        self.assertEqual(carried['adjustment']['origin_month'], '2026-09')
        original_posting = self.db.get(Posting, original['id'])
        self.assertEqual(original_posting.volume, 200)  # 原数保留
        # 同一笔变化只归一期：再次结转被拒（冲突会回滚，用新 session 验证计数）
        with self.assertRaises(HTTPException) as ctx:
            settlement.correct_volume(database.Session(), original['entry_id'], 250, self.actor)
        self.assertEqual(ctx.exception.status_code, 409)
        with database.Session() as check:
            self.assertEqual(check.query(Posting).count(), 2)

    def test_register_into_closed_month_rejected_and_late_goes_open_month(self):
        settlement.register_volume(self.db, VolumePayload('一组', '2026-09-10', 100), self.actor)
        settlement.ensure_period(self.db, '2026-10')
        self.db.flush()
        settlement.close_period(self.db, '2026-09', self.admin)
        with self.assertRaises(HTTPException) as ctx:
            settlement.register_volume(
                database.Session(), VolumePayload('一组', '2026-09-11', 50), self.actor)
        self.assertEqual(ctx.exception.status_code, 409)
        # 迟到单量登记到 10 月（三组无专属结论，全部口径兜底，折算值同样快照锁定）
        late, created = settlement.register_volume(
            self.db, VolumePayload('三组', '2026-09-08', 420, month='2026-10', late=True,
                                   note='延迟推送'), self.actor)
        self.assertTrue(created)
        self.assertEqual(late['source'], 'late')
        self.assertEqual(late['volume'], 420)
        self.assertEqual(late['rate_snapshot']['job_id'], 1)
        self.assertEqual(late['rate_snapshot']['basis'], '全部口径')

    def test_no_open_month_for_carry_raises_409(self):
        posting, _ = settlement.register_volume(
            self.db, VolumePayload('一组', '2026-09-29', 200), self.actor)
        settlement.close_period(self.db, '2026-09', self.admin)
        with self.assertRaises(HTTPException) as ctx:
            settlement.correct_volume(database.Session(), posting['entry_id'], 210, self.actor)
        self.assertEqual(ctx.exception.status_code, 409)

    def test_rate_card_reflects_approval_and_basis(self):
        card = {c['team']: c for c in settlement.rate_card(self.db)}
        self.assertTrue(card['一组']['available'])
        self.assertEqual(card['一组']['basis'], '班组复测')
        self.assertEqual(card['一组']['job_id'], 2)
        # 无班组专属结论时由『全部』口径兜底
        self.assertTrue(card['四组']['available'])
        self.assertEqual(card['四组']['basis'], '全部口径')
        self.assertEqual(card['四组']['job_id'], 1)
        # 撤掉全部口径后，没有任何已批准结论的班组不可用
        overall = self.db.get(Job, 1)
        overall.approved = False
        self.db.flush()
        card2 = {c['team']: c for c in settlement.rate_card(self.db)}
        self.assertFalse(card2['四组']['available'])
        self.assertIsNone(card2['四组']['hours_per_pick'])
        self.assertTrue(card2['一组']['available'])  # 班组专属结论仍可用

    def test_period_listing_totals(self):
        settlement.register_volume(self.db, VolumePayload('一组', '2026-10-01', 200), self.actor)
        settlement.register_volume(self.db, VolumePayload('二组', '2026-10-02', 360), self.actor)
        periods = settlement.list_periods(self.db)
        self.assertEqual(len(periods), 1)
        view = periods[0]
        self.assertEqual(view['volume'], 560)
        self.assertEqual(len(view['postings']), 2)
        self.assertAlmostEqual(
            view['saved_hours'],
            200 * 18 / 3600 + 360 * 36 / 3600, places=6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
