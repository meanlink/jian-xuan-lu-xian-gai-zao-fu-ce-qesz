"""月末节省工时结算的纯业务规则引擎。

不依赖 FastAPI / SQLAlchemy，便于用标准库 unittest 直接验证：
- 折算值只取自已批准（approved 且 completed）的班组复测结论，登记时快照锁定；
- 同一班次同一期间只入账一次，未关账更正按原折算值重算，已关账原数保留；
- 迟到单量与已关账更正差额计入首个未关账月份，补差沿用原折算值；
- 同一笔变化只归一期（entry+period 唯一），调用方重试由幂等键拦截。
"""
from dataclasses import dataclass, field

SECONDS_PER_HOUR = 3600.0
SOURCE_INITIAL = 'initial'
SOURCE_CORRECTION = 'correction'
SOURCE_LATE = 'late'


def mean_saved_seconds(job):
    """从已批准 Job 的结果指标中取『平均节省』秒数；取不到返回 None。"""
    result = getattr(job, 'result', None) or {}
    for metric in result.get('metrics', []) or []:
        if metric.get('label') == '平均节省':
            try:
                value = float(metric.get('value'))
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None
    return None


def is_approved(job):
    return getattr(job, 'status', None) == 'completed' and bool(getattr(job, 'approved', False))


def resolve_rate_job(team, jobs):
    """班组折算值来源：优先班组专属的已批准复测，其次『全部』口径的已批准复测。

    同口径下取最新批准的一条（按 id 最大）。无可用结论时返回 None。
    """
    team_match = None
    overall_match = None
    for job in jobs:
        if not is_approved(job):
            continue
        params = getattr(job, 'params', None) or {}
        mean = mean_saved_seconds(job)
        if mean is None:
            continue
        if params.get('team') == team and (team_match is None or job.id > team_match.id):
            team_match = job
        elif params.get('team') == '全部' and (overall_match is None or job.id > overall_match.id):
            overall_match = job
    return team_match or overall_match


def build_rate_snapshot(team, jobs):
    """生成并锁定折算值快照。无已批准结论时抛 ValueError（调用方转 409/422）。"""
    job = resolve_rate_job(team, jobs)
    if job is None:
        raise ValueError(f'班组 {team} 尚无可采用的已批准复测结论，无法折算工时')
    mean = mean_saved_seconds(job)
    return {
        'team': team,
        'job_id': job.id,
        'job_name': job.name,
        'mean_saved_seconds': round(mean, 6),
        'hours_per_pick': round(mean / SECONDS_PER_HOUR, 10),
        'basis': '班组复测' if (job.params or {}).get('team') == team else '全部口径',
    }


def saved_hours(volume, rate_snapshot):
    """节省工时 = 实拣单量 × 锁定的每单节省工时折算值。"""
    return round(int(volume) * float(rate_snapshot['hours_per_pick']), 6)


@dataclass
class LedgerEntry:
    key: str                      # period-month|team|shift_date
    month: str
    team: str
    shift_date: str
    volume: int
    rate_snapshot: dict
    late: bool = False
    note: str = ''


@dataclass
class LedgerPosting:
    entry_key: str
    month: str
    source: str
    volume: int
    rate_snapshot: dict
    saved_hours: float
    adjustment: dict = field(default_factory=dict)
    idempotency_key: str | None = None


class Ledger:
    """内存态规则推演器：与 DB 服务共用同一套分支判断，服务层只负责持久化与锁。"""

    def __init__(self):
        self.entries = {}          # key -> LedgerEntry
        self.postings = []         # list[LedgerPosting]
        self.idempotency = {}      # key -> LedgerPosting

    def _find_entry(self, month, team, shift_date):
        return self.entries.get(f'{month}|{team}|{shift_date}')

    def _posting_for(self, entry_key, month):
        for p in self.postings:
            if p.entry_key == entry_key and p.month == month:
                return p
        return None

    def register(self, month, team, shift_date, volume, rate_snapshot, *, late=False, note='', idempotency_key=None):
        if idempotency_key and idempotency_key in self.idempotency:
            return self.idempotency[idempotency_key], False
        key = f'{month}|{team}|{shift_date}'
        if key in self.entries:
            raise ValueError('该班次在本期间已登记，同一班次只能入账一次，请使用更正')
        entry = LedgerEntry(key, month, team, shift_date, int(volume), rate_snapshot, late=late, note=note)
        self.entries[key] = entry
        source = SOURCE_LATE if late else SOURCE_INITIAL
        posting = LedgerPosting(key, month, source, int(volume), rate_snapshot,
                                saved_hours(volume, rate_snapshot), idempotency_key=idempotency_key)
        self.postings.append(posting)
        if idempotency_key:
            self.idempotency[idempotency_key] = posting
        return posting, True

    def correct_open(self, entry, new_volume, *, idempotency_key=None):
        """未关账期间更正：原分录就地按锁定折算值重算，不新增期间、不换折算值。"""
        if idempotency_key and idempotency_key in self.idempotency:
            return self.idempotency[idempotency_key], False
        posting = self._posting_for(entry.key, entry.month)
        old_volume = entry.volume
        entry.volume = int(new_volume)
        posting.volume = int(new_volume)
        posting.saved_hours = saved_hours(new_volume, entry.rate_snapshot)
        posting.source = SOURCE_CORRECTION
        posting.adjustment = {'old_volume': old_volume, 'new_volume': int(new_volume),
                              'delta_volume': int(new_volume) - old_volume,
                              'delta_hours': round(posting.saved_hours - saved_hours(old_volume, entry.rate_snapshot), 6)}
        if idempotency_key:
            self.idempotency[idempotency_key] = posting
        return posting, True

    def carry_to_open(self, entry, target_month, delta_volume, *, source, note, idempotency_key=None):
        """已关账更正差额或迟到单量：原数保留，在首个未关账月份按原折算值补差。"""
        if idempotency_key and idempotency_key in self.idempotency:
            return self.idempotency[idempotency_key], False
        existing = self._posting_for(entry.key, target_month)
        if existing is not None:
            # 同一笔变化只归一期：该条目在目标期间已有补差，拒绝重复入账
            raise ValueError('该班次在首个未关账月份已有补差分录，不能重复入账')
        hours = saved_hours(delta_volume, entry.rate_snapshot)
        posting = LedgerPosting(entry.key, target_month, source, int(delta_volume), entry.rate_snapshot, hours,
                                adjustment={'origin_month': entry.month, 'delta_volume': int(delta_volume),
                                            'delta_hours': hours, 'note': note},
                                idempotency_key=idempotency_key)
        self.postings.append(posting)
        if idempotency_key:
            self.idempotency[idempotency_key] = posting
        return posting, True

    def month_totals(self, month):
        rows = [p for p in self.postings if p.month == month]
        return {'month': month, 'postings': len(rows),
                'volume': sum(p.volume for p in rows),
                'saved_hours': round(sum(p.saved_hours for p in rows), 6)}
