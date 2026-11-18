from pydantic import BaseModel, Field, field_validator
import re

MONTH_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])$')
DATE_RE = re.compile(r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$')
TEAMS = ('一组', '二组', '三组', '四组')


class VolumeInput(BaseModel):
    team: str = Field(min_length=1, max_length=60)
    shift_date: str = Field(min_length=10, max_length=10)
    volume: int = Field(ge=0, le=100_000_000)
    month: str | None = Field(default=None, max_length=7)
    late: bool = False
    note: str = Field(default='', max_length=300)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=80)

    @field_validator('shift_date')
    @classmethod
    def _date(cls, v):
        if not DATE_RE.match(v):
            raise ValueError('班次日期格式应为 YYYY-MM-DD')
        return v

    @field_validator('month')
    @classmethod
    def _month(cls, v):
        if v is not None and not MONTH_RE.match(v):
            raise ValueError('月份格式应为 YYYY-MM')
        return v

    @field_validator('team')
    @classmethod
    def _team(cls, v):
        if v not in TEAMS:
            raise ValueError(f'班组必须是 {"/".join(TEAMS)} 之一')
        return v


class CorrectionInput(BaseModel):
    new_volume: int = Field(ge=0, le=100_000_000)
    note: str = Field(default='', max_length=300)
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=80)


class CloseInput(BaseModel):
    month: str = Field(min_length=7, max_length=7)

    @field_validator('month')
    @classmethod
    def _month(cls, v):
        if not MONTH_RE.match(v):
            raise ValueError('月份格式应为 YYYY-MM')
        return v
