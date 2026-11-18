"""月末节省工时结算的数据库服务：事务边界、行锁与持久化。

并发口径（关账与改量并发、中断重试）：
- 期间行 SELECT ... FOR UPDATE 串行化同一月份的关账/登记/更正；
- uq_shift_once 保证同班次同期间只登记一次，uq_entry_period_posting
  保证一条班次变化在一个期间至多一条分录，重试不重复入账；
- 幂等键命中时直接回放既有分录，调用方网络重试安全。
"""
import time
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from src.models import Job, AccountingPeriod, ShiftEntry, Posting
from src.services import settlement_rules as rules

TEAMS = ('一组', '二组', '三组', '四组')


def _with_lock(db, model, row_id):
    """MySQL 下行锁；SQLite 等不支持 FOR UPDATE 的库退化为普通查询。"""
    try:
        return db.scalar(select(model).where(model.id == row_id).with_for_update())
    except Exception:
        return db.get(model, row_id)


def _month_of(shift_date):
    return shift_date[:7]


def ensure_period(db, month):
    period = db.scalar(select(AccountingPeriod).where(AccountingPeriod.month == month))
    if period is None:
        period = AccountingPeriod(month=month, closed=False, created_at=time.time())
        db.add(period)
        db.flush()
    return period


def first_open_period(db, *, after_month=None):
    """首个未关账月份；after_month 给出时只考虑更晚的月份。"""
    stmt = select(AccountingPeriod).where(AccountingPeriod.closed == False)  # noqa: E712
    if after_month:
        stmt = stmt.where(AccountingPeriod.month > after_month)
    period = db.scalar(stmt.order_by(AccountingPeriod.month.asc()))
    if period is None:
        raise HTTPException(409, '没有未关账月份可承接迟到单量或关账补差，请先建立新月份')
    return period


def rate_card(db):
    """各班组当前可采用的已批准复测结论与折算值。"""
    jobs = list(db.scalars(select(Job).where(Job.status == 'completed', Job.approved == True)))  # noqa: E712
    card = []
    for team in TEAMS:
        try:
            snapshot = rules.build_rate_snapshot(team, jobs)
            card.append({'team': team, 'available': True, **snapshot})
        except ValueError:
            card.append({'team': team, 'available': False, 'hours_per_pick': None,
                         'mean_saved_seconds': None, 'job_id': None, 'job_name': None, 'basis': None})
    return card


def list_periods(db):
    periods = list(db.scalars(select(AccountingPeriod).order_by(AccountingPeriod.month.desc())))
    postings = list(db.scalars(select(Posting).order_by(Posting.id.asc())))
    entries = {e.id: e for e in db.scalars(select(ShiftEntry))}
    result = []
    for p in periods:
        rows = [x for x in postings if x.period_id == p.id]
        result.append({
            'id': p.id, 'month': p.month, 'closed': p.closed, 'closed_at': p.closed_at,
            'closed_by': p.closed_by,
            'entry_count': len({x.entry_id for x in rows if x.entry_id}),
            'volume': sum(x.volume for x in rows),
            'saved_hours': round(sum(x.saved_hours for x in rows), 6),
            'postings': [_posting_dict(x, entries.get(x.entry_id) if x.entry_id else None) for x in rows],
        })
    return result


def _entry_dict(e):
    return {'id': e.id, 'period_id': e.period_id, 'team': e.team, 'shift_date': e.shift_date,
            'volume': e.volume, 'rate_snapshot': e.rate_snapshot, 'late': e.late, 'note': e.note,
            'created_by': e.created_by, 'created_at': e.created_at, 'updated_at': e.updated_at}


def _posting_dict(p, entry=None):
    return {'id': p.id, 'period_id': p.period_id, 'entry_id': p.entry_id, 'team': p.team,
            'shift_date': p.shift_date, 'source': p.source, 'volume': p.volume,
            'rate_snapshot': p.rate_snapshot, 'saved_hours': p.saved_hours,
            'adjustment': p.adjustment, 'idempotency_key': p.idempotency_key,
            'entry': _entry_dict(entry) if entry else None, 'created_at': p.created_at}


def list_entries(db, period_id=None):
    stmt = select(ShiftEntry).order_by(ShiftEntry.id.desc())
    if period_id:
        stmt = stmt.where(ShiftEntry.period_id == period_id)
    return [_entry_dict(e) for e in db.scalars(stmt).limit(500)]


def _replay_by_key(db, key):
    posting = db.scalar(select(Posting).where(Posting.idempotency_key == key))
    return _posting_dict(posting) if posting else None


def register_volume(db, data, user):
    """登记班次实拣单量；同一班次在同一期间只入账一次。"""
    if data.idempotency_key:
        replay = _replay_by_key(db, data.idempotency_key)
        if replay:
            return replay, False
    month = data.month or _month_of(data.shift_date)
    period = _with_lock(db, AccountingPeriod, ensure_period(db, month).id)
    if period.closed:
        raise HTTPException(409, f'{month} 已关账，不能直接登记；迟到单量请登记到首个未关账月份')
    jobs = list(db.scalars(select(Job).where(Job.status == 'completed', Job.approved == True)))  # noqa: E712
    try:
        snapshot = rules.build_rate_snapshot(data.team, jobs)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    now = time.time()
    entry = ShiftEntry(period_id=period.id, team=data.team, shift_date=data.shift_date,
                       volume=data.volume, rate_snapshot=snapshot, late=bool(data.late),
                       note=data.note or '', created_by=user.email, created_at=now, updated_at=now)
    db.add(entry)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该班次在本期间已登记，同一班次只能入账一次，请改用更正')
    source = rules.SOURCE_LATE if data.late else rules.SOURCE_INITIAL
    posting = Posting(period_id=period.id, entry_id=entry.id, team=data.team, shift_date=data.shift_date,
                      source=source, volume=data.volume, rate_snapshot=snapshot,
                      saved_hours=rules.saved_hours(data.volume, snapshot),
                      idempotency_key=data.idempotency_key, created_at=now)
    db.add(posting)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该班次变化已入账，重复请求未再次入账')
    return _posting_dict(posting), True


def correct_volume(db, entry_id, new_volume, user, *, idempotency_key=None, note=''):
    """更正班次单量。未关账就地重算；已关账原数保留，差额计入首个未关账月份。"""
    if idempotency_key:
        replay = _replay_by_key(db, idempotency_key)
        if replay:
            return replay, False
    entry = db.get(ShiftEntry, entry_id)
    if entry is None:
        raise HTTPException(404, '班次登记不存在')
    period = _with_lock(db, AccountingPeriod, entry.period_id)
    now = time.time()
    if new_volume == entry.volume:
        raise HTTPException(422, '更正单量与原数相同，无需入账')
    if new_volume < 0:
        raise HTTPException(422, '实拣单量不能为负')

    if not period.closed:
        # 未关账：原分录按锁定折算值就地重算，不新增期间、不换折算值
        posting = db.scalar(select(Posting).where(Posting.entry_id == entry.id,
                                                  Posting.period_id == period.id).with_for_update())
        old_volume = entry.volume
        old_hours = posting.saved_hours
        entry.volume = new_volume
        entry.updated_at = now
        posting.volume = new_volume
        posting.saved_hours = rules.saved_hours(new_volume, entry.rate_snapshot)
        posting.source = rules.SOURCE_CORRECTION
        posting.adjustment = {'old_volume': old_volume, 'new_volume': new_volume,
                              'delta_volume': new_volume - old_volume,
                              'delta_hours': round(posting.saved_hours - old_hours, 6),
                              'note': note}
        posting.idempotency_key = idempotency_key
        db.flush()
        return _posting_dict(posting), True

    # 已关账：原数保留，更正差额计入首个未关账月份，沿用原折算值
    delta = new_volume - entry.volume
    target = first_open_period(db, after_month=period.month)
    target = _with_lock(db, AccountingPeriod, target.id)
    existing = db.scalar(select(Posting).where(Posting.entry_id == entry.id, Posting.period_id == target.id))
    if existing is not None:
        raise HTTPException(409, '该班次在首个未关账月份已有补差分录，同一笔变化只归一期')
    hours = rules.saved_hours(delta, entry.rate_snapshot)
    posting = Posting(period_id=target.id, entry_id=entry.id, team=entry.team, shift_date=entry.shift_date,
                      source=rules.SOURCE_CORRECTION, volume=delta, rate_snapshot=entry.rate_snapshot,
                      saved_hours=hours,
                      adjustment={'origin_month': period.month, 'old_volume': entry.volume,
                                  'new_volume': new_volume, 'delta_volume': delta, 'delta_hours': hours,
                                  'note': note or '已关账更正差额结转'},
                      idempotency_key=idempotency_key, created_at=now)
    db.add(posting)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, '该补差已入账，重复请求未再次入账')
    return _posting_dict(posting), True


def close_period(db, month, user):
    """关账：锁定期间。并发的登记/更正会在期间行锁上串行，关账后一律走结转规则。"""
    period = db.scalar(select(AccountingPeriod).where(AccountingPeriod.month == month))
    if period is None:
        raise HTTPException(404, '会计期间不存在，请先登记班次单量')
    period = _with_lock(db, AccountingPeriod, period.id)
    if period.closed:
        # 关账重试天然幂等：回放期间现状，不重复处理
        return _period_summary(db, period), False
    period.closed = True
    period.closed_at = time.time()
    period.closed_by = user.email
    db.flush()
    return _period_summary(db, period), True


def _period_summary(db, period):
    rows = list(db.scalars(select(Posting).where(Posting.period_id == period.id)))
    return {'id': period.id, 'month': period.month, 'closed': period.closed,
            'closed_at': period.closed_at, 'closed_by': period.closed_by,
            'posting_count': len(rows), 'volume': sum(x.volume for x in rows),
            'saved_hours': round(sum(x.saved_hours for x in rows), 6)}
