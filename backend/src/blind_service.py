import math
from datetime import datetime,timezone
from fastapi import HTTPException
from sqlalchemy import select,update
from sqlalchemy.exc import IntegrityError
from .models import Accession,BlindCount,GerminationTest,Lot
from .common import audit,lock,pack,reserve

# 同作物已结案的双人数值是最小拟合样本；不足时不得自动采初点。
MIN_HISTORY=4

def _now():return datetime.now(timezone.utc).isoformat(timespec='seconds')

def fit_interval(pairs,sampled):
    """对同作物已结案记录拟合有符号差值 (B-A) 的95%预测区间。

    pairs 为 [(抽样粒数, B-A)]。区间随抽样粒数线性变化；返回整数上下界。
    纯函数便于离线复核，不依赖数据库。
    """
    n=len(pairs)
    if n<MIN_HISTORY:return {'samples':n,'minimum':MIN_HISTORY,'insufficient':True}
    xs=[float(p[0]) for p in pairs];ys=[float(p[1]) for p in pairs]
    mx=sum(xs)/n;my=sum(ys)/n
    sxx=sum((x-mx)**2 for x in xs);sxy=sum((x-mx)*(y-my) for x,y in zip(xs,ys))
    slope=sxy/sxx if sxx else 0.0
    intercept=my-slope*mx
    residuals=[y-(intercept+slope*x) for x,y in zip(xs,ys)]
    dof=n-2
    rse=math.sqrt(sum(r*r for r in residuals)/dof) if dof>0 else 0.0
    predicted=intercept+slope*sampled
    if sxx:
        se=rse*math.sqrt(1+1/n+(sampled-mx)**2/sxx)
    else:se=rse*math.sqrt(1+1/n)
    margin=1.96*se
    lower=math.floor(predicted-margin);upper=math.ceil(predicted+margin)
    if upper<lower:upper=lower
    return {'samples':n,'minimum':MIN_HISTORY,'insufficient':False,'model':'同作物已结案双人数值 / 有符号差值 B-A 对抽样粒数的一元最小二乘，95%预测区间','intercept':round(intercept,4),'slope':round(slope,6),'residual_std':round(rse,4),'predicted_diff':round(predicted,4),'lower':lower,'upper':upper,'interval95':[lower,upper]}

def calibrate(s,crop,sampled,exclude_id):
    rows=s.scalars(select(BlindCount).where(BlindCount.crop==crop,BlindCount.state=='sealed',BlindCount.id!=exclude_id,BlindCount.count_a.is_not(None),BlindCount.count_b.is_not(None))).all()
    result=fit_interval([(int(r.sampled),int(r.count_b)-int(r.count_a))for r in rows],sampled)
    return {'crop':crop}|result

def view(s,u,b):
    """封单前只显示本人计数；对方数值与时间戳一律遮蔽。"""
    data=pack(b);data['has_a']=b.count_a is not None;data['has_b']=b.count_b is not None
    data['my_slot']='a' if u.email==b.counter_a else ('b' if u.email==b.counter_b else None)
    if b.state=='counting':
        if u.email!=b.counter_a:data['count_a']=None;data['count_a_at']=None
        if u.email!=b.counter_b:data['count_b']=None;data['count_b_at']=None
    return data

def open_count(s,d,u):
    """开单即领样；同一单号重试返回原单，不再扣种。"""
    lot=s.get(Lot,d.lot_id)
    if not lot:raise HTTPException(404,'种批不存在')
    accession=s.get(Accession,lot.accession_id)
    existing=s.scalar(select(BlindCount).where(BlindCount.code==d.code))
    if existing:
        if existing.lot_id!=d.lot_id or existing.sampled!=d.sampled:raise HTTPException(409,'盲核单号已被其他抽样占用')
        return view(s,u,existing)
    # 先以单号占位并 flush，唯一键让并发重试只有一笔能落库；
    # 占位成功后才扣种，唯一的扣种只发生一次。
    x=BlindCount(code=d.code,lot_id=lot.id,crop=accession.crop,sampled=d.sampled,state='counting')
    s.add(x)
    try:s.flush()
    except IntegrityError:
        s.rollback()
        current=s.scalar(select(BlindCount).where(BlindCount.code==d.code))
        if not current:raise
        if current.lot_id!=d.lot_id or current.sampled!=d.sampled:raise HTTPException(409,'盲核单号已被其他抽样占用')
        return view(s,u,current)
    reserve(s,lot,d.sampled)
    audit(s,u,'开立双人盲核单并领样 '+d.code)
    return view(s,u,x)

def _reload(s,id):
    s.expire_all()
    return s.scalar(select(BlindCount).where(BlindCount.id==id).execution_options(populate_existing=True))

def submit(s,id,count,u):
    b=lock(s,BlindCount,id)
    if b.state!='counting':raise HTTPException(409,'已封单，计数不可更改')
    if count>b.sampled:raise HTTPException(409,'计数不能超过抽样粒数')
    now=_now()
    if u.email in (b.counter_a,b.counter_b):
        if (b.count_a if u.email==b.counter_a else b.count_b) is not None:
            raise HTTPException(409,'本人已有计数，请先撤回再重新交数')
        slots=['a' if u.email==b.counter_a else 'b']
    else:slots=['a','b']
    # 条件更新保证同一人只占一个计数位、只交一次；不依赖数据库是否支持行锁。
    for slot in slots:
        own=getattr(BlindCount,f'counter_{slot}');other=getattr(BlindCount,f'counter_{"b" if slot=="a" else "a"}')
        own_count=getattr(BlindCount,f'count_{slot}')
        stmt=update(BlindCount).where(BlindCount.id==id,BlindCount.state=='counting',own_count.is_(None),(own.is_(None)|(own==u.email)),(other.is_(None)|(other!=u.email)))
        stmt=stmt.values(**{f'counter_{slot}':u.email,f'count_{slot}':count,f'count_{slot}_at':now})
        if s.execute(stmt).rowcount==1:
            audit(s,u,'提交盲核计数 '+b.code)
            return view(s,u,_reload(s,id))
    raise HTTPException(409,'两名计数员已各自交数，请由本人撤回后再改')

def withdraw(s,id,u):
    """撤回与封单都先锁同一盲核行，再用条件更新落状态：
    先撤回则封单因“须重新交数”失败；先封单则撤回失败、保留原判定。"""
    b=lock(s,BlindCount,id)
    if b.state!='counting':raise HTTPException(409,'已封单，保留原判定，不能撤回')
    slot='a' if u.email==b.counter_a else ('b' if u.email==b.counter_b else None)
    if not slot:raise HTTPException(403,'只能撤回本人的计数')
    values={f'count_{slot}':None,f'count_{slot}_at':None}
    stmt=update(BlindCount).where(BlindCount.id==id,BlindCount.state=='counting',getattr(BlindCount,f'counter_{slot}')==u.email,getattr(BlindCount,f'count_{slot}').is_not(None)).values(**values)
    if s.execute(stmt).rowcount!=1:
        s.rollback();current=_reload(s,id)
        if current and current.state!='counting':raise HTTPException(409,'已封单，保留原判定，不能撤回')
        raise HTTPException(409,'本人当前没有可撤回的计数')
    audit(s,u,'撤回盲核计数 '+b.code)
    return view(s,u,_reload(s,id))

def seal(s,id,u):
    b=lock(s,BlindCount,id)
    if b.state!='counting':raise HTTPException(409,'只有待交数的盲核单可以封单')
    if b.count_a is None or b.count_b is None:raise HTTPException(409,'计数员撤回后须重新交数，两名计数员齐全才能封单')
    calibration=calibrate(s,b.crop,b.sampled,b.id)
    if calibration['insufficient']:
        state='recount';adoption='supervisor';result=None;message='同作物结案样本不足，转主管复点 '+b.code
    else:
        difference=int(b.count_b)-int(b.count_a)
        calibration['observed_diff']=difference
        if calibration['lower']<=difference<=calibration['upper']:
            first='a' if (b.count_a_at or '')<=(b.count_b_at or '') else 'b'
            calibration['adopted_slot']=first
            result=int(b.count_a if first=='a' else b.count_b);adoption='initial';state='sealed';message='封单采初点 '+b.code
        else:
            state='recount';adoption='supervisor';result=None;message='盲核差值超界，转主管复点 '+b.code
    # 以当前两份计数与时间为条件，防止读后被撤回/重交；先撤回时此处影响0行。
    stmt=update(BlindCount).where(BlindCount.id==id,BlindCount.state=='counting',BlindCount.count_a==b.count_a,BlindCount.count_b==b.count_b,BlindCount.count_a_at==b.count_a_at,BlindCount.count_b_at==b.count_b_at).values(state=state,adoption=adoption,result=result,calibration=calibration)
    if s.execute(stmt).rowcount!=1:
        s.rollback();current=_reload(s,id)
        if current and current.state!='counting':raise HTTPException(409,'只有待交数的盲核单可以封单')
        raise HTTPException(409,'计数员撤回后须重新交数，两名计数员齐全才能封单')
    b.calibration=calibration;b.state=state;b.adoption=adoption;b.result=result
    audit(s,u,message)
    return pack(_reload(s,id))

def recount(s,id,count,u):
    b=lock(s,BlindCount,id)
    if b.state!='recount':raise HTTPException(409,'只有待复点的盲核单可由主管复点')
    if count>b.sampled:raise HTTPException(409,'复点计数不能超过抽样粒数')
    stmt=update(BlindCount).where(BlindCount.id==id,BlindCount.state=='recount').values(supervisor_count=count,supervisor=u.email,result=count,state='sealed',adoption='supervisor')
    if s.execute(stmt).rowcount!=1:
        s.rollback();raise HTTPException(409,'只有待复点的盲核单可由主管复点')
    audit(s,u,'主管复点并封单 '+b.code)
    return pack(_reload(s,id))

def vitality(s,id,u):
    """封单后才允许生成复测记录；重试按 test_id 幂等，不重复建单。"""
    b=lock(s,BlindCount,id)
    if b.historical:raise HTTPException(409,'历史归档盲核单只用于区间拟合，不再生成复测')
    if b.state!='sealed' or b.result is None:raise HTTPException(409,'封单并有最终计数后才能送活力估计')
    if b.test_id:
        test=s.get(GerminationTest,b.test_id)
        if not test:raise HTTPException(409,'盲核单关联的复测记录缺失')
        if test.state in ('recorded','failed'):
            test.state='queued';test.error=None;audit(s,u,'盲核封单重试送活力估计 '+b.code)
            return test,True
        # 已入队或已完成：不重复投递，也不新建复测记录。
        return test,False
    code=('GT-BC-%d-%s'%(b.id,b.code))[:60]
    test=GerminationTest(lot_id=b.lot_id,code=code,sampled=b.sampled,germinated=int(b.result),state='queued')
    s.add(test)
    try:s.flush()
    except IntegrityError:
        # 并发重试已在唯一编号上落库：回滚本次新建，复用原复测记录，不再投递。
        s.rollback()
        current=_reload(s,id)
        if not current.test_id:raise
        return s.get(GerminationTest,current.test_id),False
    # 条件占用 test_id：并发重试只有一笔能绑定成功，其余删掉本次新建记录并复用原单。
    claimed=s.execute(update(BlindCount).where(BlindCount.id==id,BlindCount.test_id.is_(None)).values(test_id=test.id)).rowcount
    if claimed!=1:
        # 另有并发请求已绑定复测记录；本事务丢弃刚建的记录，只回报原单，不重复投递。
        s.delete(test);s.flush()
        current=_reload(s,id)
        if not current.test_id:raise HTTPException(409,'盲核单复测记录绑定失败，请重试')
        return s.get(GerminationTest,current.test_id),False
    audit(s,u,'盲核封单送活力估计 '+b.code)
    return test,True
