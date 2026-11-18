import time, json
from sqlalchemy import select, func
from src.config.database import Session
from migrations.initial import migrate
from src.models import User, Dataset, Job, Report, Delivery, Audit, AccountingPeriod, ShiftEntry
from src.services.business import metadata, seed_records, analyze
from src.services.reports import deliver
from src.services import settlement
from src.services.settlement_rules import saved_hours
from src.utils.security import hash_password
from src.utils.serialization import checksum


class _Actor:
    def __init__(self, email):
        self.email = email


def seed():
    migrate()
    with Session() as db:
        for role,name in [('admin','林知 · 业务负责人'),('analyst','陈舟 · 分析员'),('viewer','周禾 · 观察员')]:
            email=role+'@demo.local'
            if not db.scalar(select(User).where(User.email==email)):
                db.add(User(email=email,name=name,role=role,password=hash_password('Demo123!')))
        db.flush()
        m=metadata()
        d=db.scalar(select(Dataset).where(Dataset.name==m['dataset_name']))
        if not d:
            rows=seed_records()
            d=Dataset(name=m['dataset_name'],source=m['short']+'-202611.csv',records=rows,checksum=checksum(rows),created_at=time.time())
            db.add(d); db.flush()
        if not db.scalar(select(Job).where(Job.name=='基线复核 · '+m['name'])):
            params={p['key']:p['default'] for p in m.get('parameters',[])}
            result=analyze(d.records,params)
            json.dumps(result,ensure_ascii=False,allow_nan=False)
            admin=db.scalar(select(User).where(User.role=='admin'))
            j=Job(name='基线复核 · '+m['name'],dataset_id=d.id,params=params,status='completed',result=result,created_by=admin.id,created_at=time.time()-3600,finished_at=time.time()-3595,approved=True)
            db.add(j);db.flush()
            r=Report(name='每日业务复核简报',job_id=j.id,destination='业务复核组 · 本地收件箱',interval_seconds=86400,next_run=time.time()+86400,enabled=True)
            db.add(r);db.flush();deliver(db,r)
            db.add(Audit(actor='seed',action='demo.baseline',detail={'dataset':d.id},at=time.time()))

        # 班组口径的已批准复测结论：月末折算节省工时的锁定依据
        admin=db.scalar(select(User).where(User.role=='admin'))
        team_jobs=[]
        for team in ('一组','二组','三组','四组'):
            name=f'班组复测 · {team} · 已批准'
            j=db.scalar(select(Job).where(Job.name==name))
            if not j:
                params={'team':team,'confidence':'0.95'}
                result=analyze(d.records,params)
                json.dumps(result,ensure_ascii=False,allow_nan=False)
                j=Job(name=name,dataset_id=d.id,params=params,status='completed',result=result,
                      created_by=admin.id,created_at=time.time()-1800,finished_at=time.time()-1700,approved=True)
                db.add(j); db.flush()
                db.add(Audit(actor='seed',action='demo.team-approval',detail={'job':j.id,'team':team},at=time.time()))
            team_jobs.append(j)

        # 月末工时结算演示：9 月已关账（含一笔关账后更正差额），10 月未关账（含迟到单量）
        if not db.scalar(select(AccountingPeriod).where(AccountingPeriod.month=='2026-09')):
            actor=_Actor(admin.email)
            plan=[
                ('2026-09-28',{'一组':1260,'二组':1180,'三组':1090,'四组':1320}),
                ('2026-09-29',{'一组':1305,'二组':1212,'三组':1118,'四组':1288}),
                ('2026-09-30',{'一组':1284,'二组':1196,'三组':1140,'四组':1302}),
                ('2026-10-01',{'一组':1272,'二组':1201,'三组':1125,'四组':1295}),
                ('2026-10-02',{'一组':1318,'二组':1230,'三组':1156,'四组':1337}),
            ]
            entries=[]
            for shift_date, volumes in plan:
                for team, volume in volumes.items():
                    class _V: pass
                    payload=_V(); payload.team=team; payload.shift_date=shift_date; payload.volume=volume
                    payload.month=None; payload.late=False; payload.note=''; payload.idempotency_key=None
                    posting,_=settlement.register_volume(db,payload,actor)
                    entries.append((posting['entry_id'],team,shift_date,volume))
            # 迟到的 9 月班次单量：登记到首个未关账月份（10 月）
            payload=_V(); payload.team='三组'; payload.shift_date='2026-09-27'; payload.volume=420
            payload.month='2026-10'; payload.late=True; payload.note='仓储系统延迟推送的迟到单量'; payload.idempotency_key=None
            settlement.register_volume(db,payload,actor)
            # 关账 9 月
            class _C: pass
            close=_C(); close.month='2026-09'
            settlement.close_period(db,close.month,actor)
            # 关账后发现 9-30 二组少计 36 单：原数保留，差额按原折算值结转到 10 月
            target_entry=db.scalar(select(ShiftEntry).where(ShiftEntry.shift_date=='2026-09-30',ShiftEntry.team=='二组'))
            class _R: pass
            correction=_R(); correction.new_volume=target_entry.volume+36
            correction.note='关账后盘点补记 36 单'; correction.idempotency_key=None
            settlement.correct_volume(db,target_entry.id,correction.new_volume,actor,note=correction.note)
            db.add(Audit(actor='seed',action='demo.settlement',detail={'entries':len(entries)},at=time.time()))
        db.commit()
        counts={model.__tablename__:db.scalar(select(func.count()).select_from(model)) for model in [User,Dataset,Job,Report,Delivery,Audit,AccountingPeriod,ShiftEntry]}
        print(json.dumps(counts,ensure_ascii=False))
        return counts
if __name__=='__main__':seed()
