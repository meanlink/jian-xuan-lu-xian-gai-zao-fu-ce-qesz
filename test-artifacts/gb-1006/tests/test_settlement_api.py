"""结算模块的 API 端到端测试（FastAPI TestClient + SQLite）。

  python3 test-artifacts/gb-1006/tests/test_settlement_api.py
覆盖：JWT 鉴权与角色权限、登记/更正/关账/迟到/结转全链路、
幂等键重试、关账与改量并发下同一变化只归一期、错误响应可恢复。
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
os.environ['SEED_DEMO'] = 'false'  # 测试自带数据，跳过启动演示 seed

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.config.database as database  # noqa: E402
from src.config.database import Base  # noqa: E402
from src.models import User, Job, Posting  # noqa: E402
from src.utils.security import hash_password, token_for  # noqa: E402

# StaticPool：内存 SQLite 跨线程共享同一连接，TestClient 线程也能读到测试数据
engine = create_engine('sqlite://', connect_args={'check_same_thread': False},
                       poolclass=StaticPool)
database.engine = engine
database.Session = sessionmaker(engine, expire_on_commit=False)

from src.main import app  # noqa: E402


class SettlementApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        Base.metadata.create_all(engine)
        with database.Session() as db:
            cls.admin = User(email='admin@t.local', name='负责人', role='admin',
                             password=hash_password('pw'))
            cls.analyst = User(email='analyst@t.local', name='分析员', role='analyst',
                               password=hash_password('pw'))
            cls.viewer = User(email='viewer@t.local', name='观察员', role='viewer',
                              password=hash_password('pw'))
            db.add_all([cls.admin, cls.analyst, cls.viewer])
            db.flush()
            db.add_all([
                Job(name='全部结论', dataset_id=1, params={'team': '全部', 'confidence': '0.95'},
                    status='completed', approved=True, created_by=cls.admin.id, created_at=time.time(),
                    result={'metrics': [{'label': '平均节省', 'value': 36.0}]}),
                Job(name='一组结论', dataset_id=1, params={'team': '一组', 'confidence': '0.95'},
                    status='completed', approved=True, created_by=cls.admin.id, created_at=time.time(),
                    result={'metrics': [{'label': '平均节省', 'value': 18.0}]}),
            ])
            db.commit()
        cls.client = TestClient(app)
        cls.tokens = {}

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def setUp(self):
        for role, user in [('admin', self.admin), ('analyst', self.analyst), ('viewer', self.viewer)]:
            with database.Session() as db:
                u = db.scalar(select(User).where(User.email == user.email))
                self.tokens[role] = token_for(u)

    def auth(self, role):
        return {'Authorization': 'Bearer ' + self.tokens[role]}

    def test_auth_and_role_permissions(self):
        self.assertEqual(self.client.get('/settlement/rate-card').status_code, 401)
        # viewer 只读
        r = self.client.post('/settlement/volumes', headers=self.auth('viewer'),
                             json={'team': '一组', 'shift_date': '2026-10-01', 'volume': 10})
        self.assertEqual(r.status_code, 403)
        # 关账仅 admin
        r = self.client.post('/settlement/close', headers=self.auth('analyst'),
                             json={'month': '2026-10'})
        self.assertEqual(r.status_code, 403)

    def test_validation_errors_are_recoverable(self):
        r = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                             json={'team': '五组', 'shift_date': '2026-10-01', 'volume': 10})
        self.assertEqual(r.status_code, 422)
        r = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                             json={'team': '一组', 'shift_date': '2026/10/01', 'volume': 10})
        self.assertEqual(r.status_code, 422)
        r = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                             json={'team': '一组', 'shift_date': '2026-10-01', 'volume': -5})
        self.assertEqual(r.status_code, 422)

    def test_full_month_end_workflow_and_concurrency_semantics(self):
        # 1. 折算值卡
        card = {c['team']: c for c in self.client.get(
            '/settlement/rate-card', headers=self.auth('viewer')).json()}
        self.assertEqual(card['一组']['job_id'], 2)
        self.assertEqual(card['二组']['basis'], '全部口径')

        # 2. 9 月登记两笔
        for date_, team, volume in [('2026-09-29', '一组', 200), ('2026-09-30', '二组', 360)]:
            r = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                                 json={'team': team, 'shift_date': date_, 'volume': volume,
                                       'idempotency_key': f'init-{team}-{date_}'})
            self.assertEqual(r.status_code, 201, r.text)
        periods = self.client.get('/settlement/periods', headers=self.auth('admin')).json()
        sep = next(p for p in periods if p['month'] == '2026-09')
        self.assertFalse(sep['closed'])

        # 3. 同班次重复登记被拒（同一班次只入账一次）
        dup = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                               json={'team': '一组', 'shift_date': '2026-09-29', 'volume': 50})
        self.assertEqual(dup.status_code, 409)

        # 4. 未关账更正：就地重算，不新增分录
        entry_id = next(p['entry_id'] for p in sep['postings']
                        if p['shift_date'] == '2026-09-29' and p['team'] == '一组')
        r = self.client.post(f'/settlement/entries/{entry_id}/correction',
                             headers=self.auth('analyst'),
                             json={'new_volume': 240, 'note': '复核调增', 'idempotency_key': 'corr-open-001'})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body['source'], 'correction')
        self.assertEqual(body['volume'], 240)
        self.assertAlmostEqual(body['saved_hours'], 240 * 18 / 3600, places=6)
        # 同一幂等键重试：回放，不重复入账
        retry = self.client.post(f'/settlement/entries/{entry_id}/correction',
                                 headers=self.auth('analyst'),
                                 json={'new_volume': 240, 'note': '复核调增', 'idempotency_key': 'corr-open-001'})
        self.assertEqual(retry.status_code, 200)
        self.assertFalse(retry.json()['created'])
        with database.Session() as db:
            self.assertEqual(db.query(Posting).count(), 2)

        # 5. 先建立 10 月未关账期间，再关账 9 月
        self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                         json={'team': '一组', 'shift_date': '2026-10-01', 'volume': 100,
                               'idempotency_key': 'init-oct'})
        closed = self.client.post('/settlement/close', headers=self.auth('admin'),
                                  json={'month': '2026-09'})
        self.assertTrue(closed.json()['closed_now'])
        # 关账重试幂等
        again = self.client.post('/settlement/close', headers=self.auth('admin'),
                                 json={'month': '2026-09'})
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.json()['closed_now'])

        # 6. 已关账月份拒绝直接登记
        locked = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                                  json={'team': '一组', 'shift_date': '2026-09-15', 'volume': 80})
        self.assertEqual(locked.status_code, 409)

        # 7. 关账后更正：原数保留，36 单差额结转到 10 月，沿用原折算值
        r = self.client.post(f'/settlement/entries/{entry_id}/correction',
                             headers=self.auth('analyst'),
                             json={'new_volume': 276, 'note': '关账后盘点补记',
                                   'idempotency_key': 'corr-closed-1'})
        self.assertEqual(r.status_code, 200)
        carried = r.json()
        self.assertEqual(carried['source'], 'correction')
        self.assertEqual(carried['adjustment']['origin_month'], '2026-09')
        self.assertEqual(carried['volume'], 36)
        self.assertEqual(carried['rate_snapshot']['job_id'], 2)
        # 同一笔变化只归一期：重复结转被拒
        second = self.client.post(f'/settlement/entries/{entry_id}/correction',
                                  headers=self.auth('analyst'),
                                  json={'new_volume': 300, 'idempotency_key': 'corr-closed-2'})
        self.assertEqual(second.status_code, 409)

        # 8. 迟到单量计入首个未关账月份
        late = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                                json={'team': '三组', 'shift_date': '2026-09-08', 'volume': 420,
                                      'month': '2026-10', 'late': True, 'note': '延迟推送',
                                      'idempotency_key': 'late-seed-001'})
        self.assertEqual(late.status_code, 201)
        self.assertEqual(late.json()['source'], 'late')
        # 迟到登记重试不重复
        replay = self.client.post('/settlement/volumes', headers=self.auth('analyst'),
                                  json={'team': '三组', 'shift_date': '2026-09-08', 'volume': 420,
                                        'month': '2026-10', 'late': True, 'idempotency_key': 'late-seed-001'})
        self.assertFalse(replay.json()['created'])

        # 9. 期间核对：9 月原数保留，10 月承接补差与迟到
        periods = self.client.get('/settlement/periods', headers=self.auth('viewer')).json()
        sep = next(p for p in periods if p['month'] == '2026-09')
        octo = next(p for p in periods if p['month'] == '2026-10')
        self.assertTrue(sep['closed'])
        sep_team1 = next(p for p in sep['postings'] if p['team'] == '一组')
        self.assertEqual(sep_team1['volume'], 240)  # 关账前更正后的数，关账后不再变化
        sources_oct = sorted(p['source'] for p in octo['postings'])
        self.assertEqual(sources_oct, ['correction', 'initial', 'late'])
        # 10 月节省工时 = 100×班组 + 36×班组 + 420×全部
        expected = (100 + 36) * 18 / 3600 + 420 * 36 / 3600
        self.assertAlmostEqual(octo['saved_hours'], expected, places=6)


if __name__ == '__main__':
    unittest.main(verbosity=2)
