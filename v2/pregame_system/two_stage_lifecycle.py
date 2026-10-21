"""Scoped EARLY → FINAL planning on existing task/store contracts."""
import json,hashlib
from datetime import timedelta
from .prediction_versions import _instant,_utc,VersionStoreError
from .cost_ledger import next_off_peak

MODE='early-final-v1'
def enabled(policy):return policy.get('lifecycle')==MODE

def plan(planner,db,game,state,policy,at):
    tip=_instant(game['tipoff'],'tipoff');start=_instant(policy['early_requested_at'],'early_requested_at')
    final_at=tip-timedelta(minutes=20)
    if start>=final_at:raise VersionStoreError('early_start_must_precede_final_window')
    revision=state['schedule_revision'];meta={'policy':policy,'schedule_revision':revision,
        'lifecycle':MODE,'final_window_target_at':_utc(final_at),'final_ready_required':True,'model_polling':False}
    def ensure(stage,due,deadline,reason):
        logical_key=policy['policy_identity']+':'+stage
        row=db.execute('SELECT * FROM generation_tasks WHERE game_id=? AND schedule_revision=? AND policy_version=? AND logical_key=?',
            (game['id'],revision,policy['policy_version'],logical_key)).fetchone()
        if row is None:row=planner.tasks._insert(db,game['id'],stage,'scheduled',_utc(due),_utc(deadline),at,
            logical_key=logical_key,metadata=meta,created_reason=reason)
        return dict(row)
    current=db.execute('SELECT payload_json FROM prediction_versions WHERE version_id=?',(state['current_version_id'],)).fetchone()
    payload=json.loads(current[0])if current else{}
    if payload.get('stage')=='FINAL_PREGAME':return []
    result=[ensure('FINAL_PREGAME',final_at,tip,'t_minus_20_readiness_window')]
    if payload.get('stage')not in {'EARLY_PREVIEW','UPDATED_PREVIEW'} and at<final_at:
        earliest=max(at,start)
        hour=policy.get('early_release_hour_beijing')
        if hour is not None:
            from .publication_timing import preview_evening
            earliest=max(earliest,preview_evening(tip,start,hour=hour,preheat=policy.get('preheat_enabled',False)))
        due=next_off_peak(earliest,final_at,policy.get('price_holidays',()))if policy.get('prefer_off_peak',True)else earliest
        result.append(ensure('EARLY_PREVIEW',due,final_at,'evening_initial_judgment'if hour is not None else'off_peak_initial_judgment'))
    return result

def readiness_for_task(versions,task,collector=None,*,prewarm=False):
    """Returns persisted qualification before a worker/model is allowed to start."""
    from .final_readiness import FinalReadinessStore
    metadata=json.loads(task.get('metadata_json')or'{}')
    if task.get('stage')!='FINAL_PREGAME' or metadata.get('lifecycle')!=MODE:return None
    if not prewarm and versions.clock()<_instant(task['scheduled_for'],'scheduled_for'):
        return {'ready':False,'reasons':['final_window_not_open'],'next_check_at':task['scheduled_for'],'llm_calls':0}
    with versions.connect()as db:
        state=db.execute('SELECT registered_game_json,current_version_id FROM game_prediction_state WHERE game_id=?',(task['game_id'],)).fetchone()
        row=db.execute('SELECT payload_json,payload_sha256 FROM prediction_versions WHERE version_id=?',(state['current_version_id'],)).fetchone()if state else None
    if row is None:return {'ready':False,'reasons':['early_baseline_not_published'],'llm_calls':0}
    if hashlib.sha256(row['payload_json'].encode()).hexdigest()!=row['payload_sha256']:
        raise VersionStoreError('early_baseline_hash_mismatch')
    early=json.loads(row['payload_json']);early.update(version_id=state['current_version_id'],payload_sha256=row['payload_sha256'])
    if early.get('stage')=='FINAL_PREGAME':return {'ready':False,'reasons':['final_already_published'],'llm_calls':0}
    if early.get('stage')not in {'EARLY_PREVIEW','UPDATED_PREVIEW'}:return {'ready':False,'reasons':['early_baseline_not_published'],'llm_calls':0}
    game=json.loads(state['registered_game_json'])
    return FinalReadinessStore(versions,collector).check(game,early)
