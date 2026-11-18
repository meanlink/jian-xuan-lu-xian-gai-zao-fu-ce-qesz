from fastapi import APIRouter, Depends
from sqlalchemy import select
from src.config.database import get_db
from src.models import ShiftEntry
from src.middlewares.auth import current_user, can_write, admin_only
from src.schemas.settlement import VolumeInput, CorrectionInput, CloseInput
from src.services import settlement
from src.services.audit import record
router = APIRouter(prefix='/settlement')


@router.get('/rate-card')
def rate_card(db=Depends(get_db), u=Depends(current_user)):
    return settlement.rate_card(db)


@router.get('/periods')
def periods(db=Depends(get_db), u=Depends(current_user)):
    return settlement.list_periods(db)


@router.get('/entries')
def entries(period_id: int | None = None, db=Depends(get_db), u=Depends(current_user)):
    return settlement.list_entries(db, period_id)


@router.post('/volumes', status_code=201)
def register_volume(data: VolumeInput, db=Depends(get_db), u=Depends(can_write)):
    posting, created = settlement.register_volume(db, data, u)
    record(db, u.email, 'settlement.register',
           {'entry_id': posting.get('entry_id'), 'team': data.team, 'volume': data.volume,
            'late': data.late, 'replayed': not created})
    db.commit()
    return {**posting, 'created': created}


@router.post('/entries/{entry_id}/correction')
def correct_volume(entry_id: int, data: CorrectionInput, db=Depends(get_db), u=Depends(can_write)):
    posting, created = settlement.correct_volume(
        db, entry_id, data.new_volume, u, idempotency_key=data.idempotency_key, note=data.note)
    record(db, u.email, 'settlement.correct',
           {'entry_id': entry_id, 'new_volume': data.new_volume, 'source': posting['source'],
            'replayed': not created})
    db.commit()
    return {**posting, 'created': created}


@router.post('/close')
def close_period(data: CloseInput, db=Depends(get_db), u=Depends(admin_only)):
    summary, closed = settlement.close_period(db, data.month, u)
    record(db, u.email, 'settlement.close', {'month': data.month, 'closed_now': closed})
    db.commit()
    return {**summary, 'closed_now': closed}
