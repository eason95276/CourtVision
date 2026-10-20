"""Production-only attempt accounting. No credentials, prompts or Evaluation writes."""
from __future__ import annotations
import contextvars,json,sqlite3,uuid
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

CHINA=ZoneInfo('Asia/Shanghai')
RATE_REVISION='deepseek-official-cny-20261014'
RATE_SOURCE='https://api-docs.deepseek.com/zh-cn/quick_start/pricing/'
RATES={'deepseek-flash':{'hit':'0.02','miss':'1','output':'4'},
       'deepseek-v4-pro':{'hit':'0.15','miss':'4.5','output':'13.5'}}
ALIASES={'deepseek-v4-flash':'deepseek-flash','deepseek-v4-flash-vision-exp':'deepseek-flash'}
_scope=contextvars.ContextVar('courtvision_production_cost',default=None)
_part=contextvars.ContextVar('courtvision_cost_component',default=('Unclassified',False,0))

def instant(value):
    at=value if isinstance(value,datetime) else datetime.fromisoformat(value.replace('Z','+00:00'))
    if at.tzinfo is None:raise ValueError('cost_timestamp_requires_timezone')
    return at.astimezone(timezone.utc)

def peak(at,holidays=()):
    local=instant(at).astimezone(CHINA)
    return local.weekday()<5 and local.date().isoformat() not in set(holidays) and (9<=local.hour<12 or 14<=local.hour<18)

def next_off_peak(at,latest,holidays=()):
    at=instant(at);latest=instant(latest)
    if at>=latest:raise ValueError('early_window_closed')
    candidate=at
    if peak(candidate,holidays):
        local=candidate.astimezone(CHINA)
        local=local.replace(hour=12 if local.hour<12 else 18,minute=0,second=0,microsecond=0)
        candidate=local.astimezone(timezone.utc)
    # Product deadline wins over a tariff preference.
    return at if candidate>=latest else candidate

def normalize_usage(usage):
    u=usage or{}
    if isinstance(u.get('usage'),dict):u=u['usage']
    def number(*values):
        return next((v for v in values if isinstance(v,int) and not isinstance(v,bool) and v>=0),None)
    incoming=number(u.get('prompt_tokens'),u.get('input_tokens'))
    outgoing=number(u.get('completion_tokens'),u.get('output_tokens'))
    hit=number(u.get('prompt_cache_hit_tokens'),u.get('cached_input_tokens'),
        (u.get('input_token_details')or{}).get('cache_read'),(u.get('prompt_tokens_details')or{}).get('cached_tokens'))
    miss=number(u.get('prompt_cache_miss_tokens'),u.get('uncached_input_tokens'))
    if incoming is not None and hit is None and miss is not None:hit=incoming-miss
    if incoming is not None and miss is None and hit is not None:miss=incoming-hit
    if hit is not None and miss is not None and incoming is None:incoming=hit+miss
    valid=(incoming is None or (hit is None or 0<=hit<=incoming) and (miss is None or 0<=miss<=incoming))
    if hit is not None and miss is not None and incoming is not None:valid=valid and hit+miss==incoming
    if not valid:raise ValueError('cost_cache_usage_inconsistent')
    total=number(u.get('total_tokens'))
    if total is None and incoming is not None and outgoing is not None:total=incoming+outgoing
    return dict(input_tokens=incoming,cached_input_tokens=hit,uncached_input_tokens=miss,output_tokens=outgoing,total_tokens=total)

def calculate(model,usage,at,holidays=()):
    model=ALIASES.get(model,model);u=normalize_usage(usage)
    rates=RATES.get(model);factor=Decimal(2 if peak(at,holidays) else 1)
    result={**u,'billing_model':model,'currency':'CNY','price_tier':'peak' if factor==2 else 'off_peak',
        'rate_revision':RATE_REVISION,'rate_source':RATE_SOURCE,'unit_price':None,'calculated_cost':None,
        'cache_savings':None,'cost_status':'usage_unavailable'}
    if rates is None:result['cost_status']='unverified_model_price';return result
    prices={k:Decimal(v)*factor for k,v in rates.items()};result['unit_price']={k:str(v) for k,v in prices.items()}
    if any(u[k] is None for k in ('cached_input_tokens','uncached_input_tokens','output_tokens')):
        result['cost_status']='cache_breakdown_unknown' if u['input_tokens'] is not None else 'usage_unavailable'
        return result
    cost=(Decimal(u['cached_input_tokens'])*prices['hit']+Decimal(u['uncached_input_tokens'])*prices['miss']+Decimal(u['output_tokens'])*prices['output'])/Decimal(1_000_000)
    savings=Decimal(u['cached_input_tokens'])*(prices['miss']-prices['hit'])/Decimal(1_000_000)
    result.update(calculated_cost=str(cost),cache_savings=str(savings),cost_status='usage_priced');return result

class CostLedger:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS production_cost_entries (
                entry_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,stage TEXT NOT NULL,
                component TEXT NOT NULL,provider TEXT NOT NULL,model TEXT,
                happened_at TEXT NOT NULL,retry INTEGER NOT NULL,repair INTEGER NOT NULL,
                success INTEGER NOT NULL,entry_json TEXT NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS idx_cost_game_stage ON production_cost_entries(game_id,stage,happened_at)')
            db.execute('CREATE TABLE IF NOT EXISTS production_search_account_snapshots (snapshot_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, provider TEXT NOT NULL, account_json TEXT NOT NULL)')
    def connect(self):return sqlite3.connect(self.path,timeout=20)
    def save_search_account(self,account,at=None):
        allowed={k:account.get(k)for k in ('current_plan','plan_usage','plan_limit','paygo_usage','search_usage','extract_usage')}
        at=at or datetime.now(timezone.utc).isoformat()
        with self.connect()as db:db.execute('INSERT INTO production_search_account_snapshots VALUES(?,?,?,?)',
            (str(uuid.uuid4()),at,'Tavily',json.dumps(allowed)))
        return allowed
    def refresh_search_account(self):
        import os,urllib.request
        key=os.environ.get('TAVILY_API_KEY')
        if not key:return None
        try:
            request=urllib.request.Request('https://api.tavily.com/usage',headers={'Authorization':'Bearer '+key})
            with urllib.request.urlopen(request,timeout=8)as response:data=json.load(response)
            return self.save_search_account(data['account'])
        except Exception:return None
    def record(self,*,provider,model,usage=None,at=None,success=True,error_type=None,detail=None):
        scope=_scope.get()
        if scope is None:return None
        component,repair,retry=_part.get();at=at or datetime.now(timezone.utc).isoformat()
        generation_retry=scope.get('generation_retry',0)
        if generation_retry:
            prior=[r for r in self.rows(scope['game_id'])if r['stage']==scope['stage']and r['component']==component and r['run_id']!=scope['run_id']and(r.get('input_tokens')or 0)>0]
            if prior:retry+=generation_retry
        try:price=calculate(model,usage,at,scope.get('holidays',())) if provider=='DeepSeek' else {'currency':'CNY','calculated_cost':None,'cost_status':'provider_price_unknown'}
        except ValueError:price={'currency':'CNY','calculated_cost':None,'cost_status':'provider_usage_invalid'}
        if provider in {'DeepSeek Search','DeepSeek Fetch'}:
            requests=(detail or {}).get('billable_requests')
            if isinstance(requests,int) and not isinstance(requests,bool) and requests>=0:
                price={'currency':'CNY','calculated_cost':str(Decimal(requests)*Decimal('0.005')),
                    'cost_status':'published_request_rate','billable_requests':requests,
                    'rate_source':'https://api-docs.deepseek.com/zh-cn/guides/search/',
                    'rate_revision':'deepseek-search-fetch-20261021'}
        value={**price,'entry_id':str(uuid.uuid4()),'game_id':scope['game_id'],'stage':scope['stage'],
            'run_id':scope['run_id'],'component':component,'provider':provider,'model':model,
            'generation_attempt':generation_retry+1,
            'timestamp':at,'retry':retry,'repair':repair,'success':bool(success),'error_type':error_type,
            'detail':detail or{},'price_semantics':'actual provider usage × dated published unit price; not wallet reconciliation'}
        value['success_semantics']='provider response; generation acceptance is linked by run_id to task/version receipt'
        with self.connect() as db:db.execute('INSERT INTO production_cost_entries VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (value['entry_id'],value['game_id'],value['stage'],component,provider,model,at,retry,int(repair),int(success),json.dumps(value,ensure_ascii=False)))
        return value['entry_id']
    def rows(self,game_id=None):
        with self.connect() as db:
            sql='SELECT entry_json FROM production_cost_entries';args=()
            if game_id:sql+=' WHERE game_id=?';args=(game_id,)
            result=[json.loads(r[0]) for r in db.execute(sql+' ORDER BY happened_at,entry_id',args)]
            snapshots=[(r[0],json.loads(r[1]))for r in db.execute('SELECT observed_at,account_json FROM production_search_account_snapshots WHERE provider=? ORDER BY observed_at DESC',('Tavily',))]
        for row in result:
            if row['provider']!='Tavily':continue
            for observed,account in snapshots:
                if (instant(row['timestamp'])<=instant(observed)and instant(row['timestamp']).strftime('%Y-%m')==instant(observed).strftime('%Y-%m')
                    and account.get('current_plan')=='Researcher'and account.get('paygo_usage')==0
                    and isinstance(account.get('plan_usage'),(int,float))and account['plan_usage']<=1000
                    and account.get('plan_limit')==1000):
                    row.update(calculated_cost='0',cost_status='verified_free_plan_no_paygo',
                        search_account_snapshot_at=observed,search_pricing_source='https://docs.tavily.com/documentation/api-credits',
                        search_cost_semantics='incremental cash fee within verified free monthly quota; credits are tracked separately')
                    break
        return result
    def finish(self,entry_id,*,model,usage,success,error_type=None):
        with self.connect()as db:
            raw=db.execute('SELECT entry_json FROM production_cost_entries WHERE entry_id=?',(entry_id,)).fetchone()
            if raw is None:raise ValueError('billing_attempt_missing')
            value=json.loads(raw[0]);scope=_scope.get()or{}
            try:price=calculate(model,usage,value['timestamp'],scope.get('holidays',()))if value['provider']=='DeepSeek'else{}
            except ValueError:price={'currency':'CNY','calculated_cost':None,'cost_status':'provider_usage_invalid'}
            value.update(price,model=model,success=bool(success),error_type=error_type,attempt_status='finished')
            if value['provider']!='DeepSeek':value['detail']['actual_provider_usage']=usage
            db.execute('UPDATE production_cost_entries SET model=?,success=?,entry_json=? WHERE entry_id=?',
                (model,int(success),json.dumps(value,ensure_ascii=False),entry_id))
        return entry_id
    def summary(self,game_id=None):
        groups={}
        for row in self.rows(game_id):
            group=groups.setdefault(row['game_id'],{}).setdefault(row['stage'],{'calls':0,'known_cost_cny':Decimal(0),'unknown_cost_calls':0,'input_tokens':0,'output_tokens':0,'cached_input_tokens':0,'repair_retry_cost_cny':Decimal(0),'cache_savings_cny':Decimal(0),'components':{}})
            group['calls']+=1
            for key in ('input_tokens','output_tokens','cached_input_tokens'):group[key]+=row.get(key)or 0
            if row['calculated_cost'] is None:group['unknown_cost_calls']+=1
            else:
                cost=Decimal(row['calculated_cost']);group['known_cost_cny']+=cost
                group['components'][row['component']]=group['components'].get(row['component'],Decimal(0))+cost
                if row['repair']or row['retry']:group['repair_retry_cost_cny']+=cost
                group['cache_savings_cny']+=Decimal(row.get('cache_savings')or'0')
        def plain(v):
            if isinstance(v,Decimal):return str(v)
            if isinstance(v,dict):return {k:plain(x)for k,x in v.items()}
            return v
        return plain(groups)

@contextmanager
def production_scope(path,game_id,stage,run_id,holidays=(),retry=0):
    ledger=CostLedger(path);token=_scope.set({'ledger':ledger,'game_id':game_id,'stage':stage,'run_id':run_id,'holidays':holidays,'generation_retry':int(retry)})
    try:yield ledger
    finally:_scope.reset(token)

@contextmanager
def component(name,*,repair=False,retry=0):
    token=_part.set((name,bool(repair),int(retry)))
    try:yield
    finally:_part.reset(token)

def active():return _scope.get() is not None

def record_call(**kwargs):
    scope=_scope.get()
    return scope['ledger'].record(**kwargs) if scope is not None else None

def begin_call(provider,model,at=None,detail=None):
    return record_call(provider=provider,model=model,at=at,success=False,
        detail={**(detail or{}),'attempt_status':'started_usage_pending'})

def finish_call(entry_id,**kwargs):
    scope=_scope.get()
    if entry_id is not None and scope is not None:return scope['ledger'].finish(entry_id,**kwargs)

def child_scope_environment():
    scope=_scope.get()
    if scope is None:return None
    return json.dumps({'path':str(scope['ledger'].path),'game_id':scope['game_id'],'stage':scope['stage'],'run_id':scope['run_id'],'holidays':scope.get('holidays',[]),'retry':scope.get('generation_retry',0)})

@contextmanager
def inherited_scope():
    import os
    value=os.environ.get('COURTVISION_PRODUCTION_COST_SCOPE')
    if not value:
        yield;return
    data=json.loads(value)
    with production_scope(data['path'],data['game_id'],data['stage'],data['run_id'],data.get('holidays',[]),data.get('retry',0)):
        yield

def metered_deepseek(invoke,model,opener):
    """Observe the existing raw HTTP body without changing parser or response content."""
    if not active():return invoke(opener)
    import urllib.request
    started=datetime.now(timezone.utc).isoformat();observed={}
    entry=begin_call('DeepSeek',model,started,{'requested_model':model})
    base=opener or urllib.request.urlopen
    class Response:
        def __init__(self,response):self.response=response
        def __enter__(self):self.response.__enter__();return self
        def __exit__(self,*args):return self.response.__exit__(*args)
        def read(self,*args):
            raw=self.response.read(*args)
            try:observed.update(json.loads(raw))
            except (ValueError,TypeError):pass
            return raw
    def wrapped(request,timeout):return Response(base(request,timeout=timeout))
    try:
        result=invoke(wrapped)
    except Exception as error:
        finish_call(entry,model=observed.get('model')or model,usage=observed.get('usage'),success=False,error_type=type(error).__name__)
        raise
    finish_call(entry,model=observed.get('model')or model,usage=observed.get('usage'),success=True)
    return result
