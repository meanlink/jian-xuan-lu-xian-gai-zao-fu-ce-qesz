from sqlalchemy import String, JSON, Double, ForeignKey, Boolean, UniqueConstraint, Integer
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base


class AccountingPeriod(Base):
    __tablename__ = 'accounting_periods'
    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), unique=True)  # YYYY-MM
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    closed_at: Mapped[float | None] = mapped_column(Double, nullable=True)
    closed_by: Mapped[str | None] = mapped_column(String(150), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)


class ShiftEntry(Base):
    """同一班次（班组+班次日期）在一个会计期间只登记一次；折算值在登记时快照锁定。"""
    __tablename__ = 'shift_entries'
    __table_args__ = (UniqueConstraint('period_id', 'team', 'shift_date', name='uq_shift_once'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('accounting_periods.id'))
    team: Mapped[str] = mapped_column(String(60))
    shift_date: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    volume: Mapped[int] = mapped_column(Integer)
    rate_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(String(300), default='')
    created_by: Mapped[str] = mapped_column(String(150))
    created_at: Mapped[float] = mapped_column(Double)
    updated_at: Mapped[float] = mapped_column(Double)


class Posting(Base):
    """入账分录：每条班次变化在同一会计期间只产生一条，差额与迟到单量沿用原折算值。"""
    __tablename__ = 'postings'
    __table_args__ = (
        # 同一条目在同一期间至多一条分录：初始入账或一笔补差，重试不重复入账
        UniqueConstraint('entry_id', 'period_id', name='uq_entry_period_posting'),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    period_id: Mapped[int] = mapped_column(ForeignKey('accounting_periods.id'))
    entry_id: Mapped[int | None] = mapped_column(ForeignKey('shift_entries.id'), nullable=True)
    team: Mapped[str] = mapped_column(String(60))
    shift_date: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(20))  # initial / correction / late（关账后的更正差额用 correction + adjustment.origin_month 标注）
    volume: Mapped[int] = mapped_column(Integer)
    rate_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    saved_hours: Mapped[float] = mapped_column(Double)
    adjustment: Mapped[dict] = mapped_column(JSON, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[float] = mapped_column(Double)
