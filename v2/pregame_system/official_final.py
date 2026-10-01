"""One immutable official prediction per game, created in the final five minutes.

This is an on-demand production entrypoint, not a scheduler or notification job.
Audit snapshots remain separate and never become official just by being created.
"""
import json
import math
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path
from .agent import Report
from .storage import BASE,instant,now,save,digest,content_hash


def validate_report(report,game,at):
    Report.model_validate(report)
    if not report.get('game') or any(report['game'].get(k)!=game[k] for k in ['game_id','home_team_id','away_team_id','scheduled_tipoff']):
        raise ValueError('report_identity_mismatch')
    p=report.get('prediction');s=report.get('snapshot')
    if not p or not s:raise ValueError('base_prediction_missing')
    if s.get('demo_runtime'):raise ValueError('offline_demo_cannot_be_official')
    if s.get('historical_replay'):raise ValueError('historical_replay_cannot_be_official')
    if not (instant(s['training_cutoff'])<=instant(s['cutoff'])<=instant(s['generated_at'])<=instant(at)<instant(game['scheduled_tipoff'])):
        raise ValueError('report_time_boundary')
    for key in ['win_probability','expected_margin','expected_total','expected_home_score','expected_away_score']:
        if not isinstance(p.get(key),(int,float)) or not math.isfinite(p[key]):raise ValueError('invalid_prediction_number')
    if not 0<=p['win_probability']<=1:raise ValueError('invalid_probability')
    if abs(p['expected_home_score']-p['expected_away_score']-p['expected_margin'])>1e-7:raise ValueError('prediction_margin_mismatch')
    if abs(p['expected_home_score']+p['expected_away_score']-p['expected_total'])>1e-7:raise ValueError('prediction_total_mismatch')
    usage=report.get('market_usage') or {}
    live_market=(report.get('market') or {}).get('as_of_cutoff')
    if live_market and usage.get('status')!='used':
        layers=report.get('prediction_layers') or {}
        if not (usage.get('reason')=='residual_unavailable' and
                layers.get('residual_model',{}).get('used_for_prediction') is False and
                layers.get('residual_prediction') is None and
                layers.get('official_market_adjustment') is None and
                p==layers.get('pure_prediction')==layers.get('official_numeric_prediction')==report.get('pure_prediction')):
            raise ValueError('eligible_market_not_used')
        from .market import validate_prediction_market_quote
        cutoff=layers.get('legal_cutoff')
        if not instant(s['cutoff'])<=instant(cutoff)<=instant(at):raise ValueError('residual_market_cutoff_boundary')
        quote=validate_prediction_market_quote(live_market,game,cutoff)
        if layers.get('market_benchmark',{}).get('quote_hash')!=content_hash(quote):raise ValueError('market_layer_quote_hash_mismatch')
    if usage.get('status')=='used':
        layers=report.get('prediction_layers') or {}
        required={'pure_prediction','market_benchmark','divergence','residual_prediction',
                  'official_numeric_prediction','official_market_adjustment','prediction_method','residual_model','legal_cutoff'}
        if not required<=set(layers):raise ValueError('official_prediction_layers_missing')
        if layers.get('prediction_method')!='market_residual_v1':raise ValueError('fixed_market_blend_not_allowed')
        if 'weight' in json.dumps(layers):raise ValueError('fixed_market_weight_not_allowed')
        if report['prediction']!=layers.get('official_numeric_prediction'):
            raise ValueError('official_numeric_prediction_not_public')
        if report.get('pure_prediction')!=layers.get('pure_prediction'):
            raise ValueError('pure_prediction_layer_mismatch')
        from .market import validate_prediction_market_quote
        market_cutoff=layers['legal_cutoff']
        if not instant(s['cutoff'])<=instant(market_cutoff)<=instant(at):raise ValueError('residual_market_cutoff_boundary')
        validated_market=validate_prediction_market_quote(live_market,game,market_cutoff)
        benchmark=layers['market_benchmark'];residual=layers['residual_prediction'];official=layers['official_numeric_prediction']
        if benchmark.get('quote_hash')!=content_hash(validated_market):raise ValueError('market_layer_quote_hash_mismatch')
        if benchmark.get('market_expected_margin') is not None:
            if abs(official['expected_margin']-benchmark['market_expected_margin']-residual['expected_margin_residual'])>1e-7:
                raise ValueError('official_margin_residual_formula_mismatch')
        if benchmark.get('market_total') is not None:
            if abs(official['expected_total']-benchmark['market_total']-residual['expected_total_residual'])>1e-7:
                raise ValueError('official_total_residual_formula_mismatch')


def last_legal_snapshot(game,at,snapshots):
    candidates=[]
    for path in Path(snapshots).glob('*/snapshot.json'):
        try:
            s=json.loads(path.read_text(encoding='utf8'));folder=path.parent
            inputs=json.loads((folder/'inputs.json').read_text(encoding='utf8'))
            if any(inputs['game'].get(k)!=game[k] for k in ['game_id','home_team_id','away_team_id','scheduled_tipoff']):continue
            if content_hash(inputs)!=s['input_hash']:continue
            models=json.loads((folder/'model_reference.json').read_text(encoding='utf8'))
            if any(digest(m['path'])!=m['sha256'] for m in models['models'].values()):continue
            if models['version']!=s['model_version']:continue
            # Prediction bytes must be bound to the stored input/model receipt.
            receipt=json.loads((folder/'integrity.json').read_text(encoding='utf8'))
            if receipt['prediction_sha256']!=digest(folder/'prediction.json'):continue
            if receipt['model_reference_sha256']!=digest(folder/'model_reference.json') or receipt['input_hash']!=s['input_hash']:continue
            p=json.loads((folder/'prediction.json').read_text(encoding='utf8'))
            layers=None
            layers_path=folder/'prediction_layers.json';layers_integrity=folder/'prediction_layers_integrity.json'
            if layers_path.is_file() and layers_integrity.is_file():
                proof=json.loads(layers_integrity.read_text(encoding='utf8'))
                if proof['prediction_layers_sha256']!=digest(layers_path):continue
                layers=json.loads(layers_path.read_text(encoding='utf8'))
                p=layers['official_numeric_prediction']
                s=dict(s,market_usage=layers['market_usage'])
            report=Report(game=game,snapshot=s,prediction=p,
                pure_prediction=(layers or {}).get('pure_prediction') or p,
                market_benchmark=(layers or {}).get('market_benchmark'),
                divergence=(layers or {}).get('divergence'),
                residual_prediction=(layers or {}).get('residual_prediction'),
                official_numeric_prediction=(layers or {}).get('official_numeric_prediction') or p,
                official_market_adjustment=(layers or {}).get('official_market_adjustment'),
                prediction_method=(layers or {}).get('prediction_method') or 'pure_basketball_fallback',
                residual_model=(layers or {}).get('residual_model'),
                prediction_layers=layers,
                residual_feature_values=inputs.get('features'),
                basketball_only_prediction=(layers or {}).get('pure_prediction') or p,
                market_informed_prediction=(layers or {}).get('official_numeric_prediction') if (layers or {}).get('residual_prediction') else None,
                market_usage=(layers or {}).get('market_usage'),
                missing_data=[{'section':name,'reason':'current retrieval failed; only prior verified base-prediction data retained'}
                              for name in ['market','team_form','matchup','availability','key_players']],
                analysis={'Fact':'使用最后一次成功的合法基础预测数据。','Inference':[],
                          'data_fallback':True,'player_conclusion':None},
                sources=[{'path':str(folder/'inputs.json'),'sha256':digest(folder/'inputs.json')}],
                trace=[{'node':'last_legal_data','snapshot_id':s['snapshot_id']}]).model_dump()
            validate_report(report,game,at)
            candidates.append((instant(s['cutoff']),instant(s['generated_at']),report))
        except (ValueError,KeyError,OSError,TypeError):continue
    return max(candidates,key=lambda x:x[:2])[2] if candidates else None


class OfficialFinalizer:
    def __init__(self,agent,root=None,snapshots=None,clock=None):
        self.agent=agent;self.root=Path(root or BASE/'outputs/official_final')
        self.snapshots=Path(snapshots or BASE/'outputs/snapshots');self.clock=clock or now
        self.simulated_clock=clock is not None
        if self.simulated_clock and root is None:raise ValueError('simulated_clock_requires_isolated_output_directory')

    def finalize(self,game_id):
        if getattr(getattr(self.agent,'predictor',None),'demo_context',None):
            raise ValueError('offline_demo_cannot_be_official')
        if not re.fullmatch(r'(?:\d{10}|espn:\d+)',game_id):raise ValueError('invalid_game_id')
        self.root.mkdir(parents=True,exist_ok=True);path=self.root/(game_id.replace(':','_')+'.json')
        if path.exists():
            existing=json.loads(path.read_text(encoding='utf8'));expected=existing.pop('document_hash')
            if content_hash(existing)!=expected:raise ValueError('official_record_integrity')
            return dict(existing,document_hash=expected)
        matches=[g for g in self.agent.schedule if g['game_id']==game_id]
        if len(matches)!=1:raise ValueError('scheduled_game_not_unique')
        game=matches[0];at=self.clock();tip=instant(game['scheduled_tipoff']);due=tip-timedelta(minutes=5)
        if instant(at)<due:return {'status':'not_due','game_id':game_id,'target_generation_time':due.isoformat()}
        if instant(at)>=tip or game.get('status')!='scheduled':raise ValueError('cannot_create_official_after_tipoff_or_cancellation')
        lock=path.with_suffix('.lock')
        with lock.open('x'):pass
        try:
            failure=None;fallback=False;market_refresh=None
            if hasattr(self.agent,'refresh_market'):
                try:market_refresh=self.agent.refresh_market(game,at)
                except Exception as exc:market_refresh={'status':'unavailable','error_type':type(exc).__name__}
            prediction_at=self.clock()
            if instant(prediction_at)>=tip:raise ValueError('generation_crossed_tipoff')
            try:
                report,_=self.agent.ask('official pregame final',cutoff=prediction_at,
                    plan={'intent':'report','game_id':game_id})
                validate_report(report,game,prediction_at)
                if instant(report['snapshot']['cutoff'])!=instant(prediction_at):raise ValueError('fresh_result_has_old_cutoff')
            except Exception as exc:
                failure=type(exc).__name__;fallback=True
                report=last_legal_snapshot(game,self.clock(),self.snapshots)
                if report is not None and hasattr(self.agent,'market') and hasattr(self.agent,'residual_predictor'):
                    try:
                        current_market=self.agent.market(game,prediction_at,False)
                        quote=(current_market or {}).get('as_of_cutoff')
                        if quote:
                            layers=self.agent.residual_predictor.predict(
                                report.get('pure_prediction') or report['prediction'],quote,
                                report.get('residual_feature_values'),game=game,cutoff=prediction_at)
                            report['pure_prediction']=layers['pure_prediction']
                            report['market_benchmark']=layers['market_benchmark']
                            report['divergence']=layers['divergence']
                            report['residual_prediction']=layers['residual_prediction']
                            report['official_numeric_prediction']=layers['official_numeric_prediction']
                            report['official_market_adjustment']=layers['official_market_adjustment']
                            report['prediction_method']=layers['prediction_method']
                            report['residual_model']=layers['residual_model']
                            report['prediction_layers']=layers
                            report['basketball_only_prediction']=layers['pure_prediction']
                            report['market_informed_prediction']=layers['official_numeric_prediction'] if layers['residual_prediction'] else None
                            report['prediction']=layers['official_numeric_prediction']
                            report['market_usage']=layers['market_usage']
                            report['market']=current_market
                            report['snapshot']['market_usage']=layers['market_usage']
                            report['trace'].append({'node':'fallback_market_refresh','status':'latest_legal_market_residual_applied'})
                    except (ValueError,KeyError,TypeError,OSError):
                        pass
            completed=self.clock()
            if instant(completed)>=tip:raise ValueError('generation_crossed_tipoff')
            if report is None:
                save(self.root/'failures'/(uuid.uuid4().hex+'.json'),{'game_id':game_id,'at':completed,
                     'failure':failure,'status':'no_legal_base_prediction'})
                return {'status':'unavailable','game_id':game_id,'reason':'no legal successful base-prediction data; no official prediction created'}
            validate_report(report,game,completed)
            s=report['snapshot']
            record={'status':'official_pregame_final','game_id':game_id,'generated_at':completed,
                'target_generation_time':due.isoformat(),'seconds_before_tipoff':(tip-instant(completed)).total_seconds(),
                'simulated_clock':self.simulated_clock,'fallback_used':fallback,'fetch_failure_type':failure,
                'actual_data_times':{'input_cutoff':s['cutoff'],'input_generated_at':s['generated_at'],
                    'latest_observed_team_game':s.get('history_latest_game'),
                    'training_cutoff':s['training_cutoff']},
                'market_refresh':market_refresh,
                'pure_prediction':report.get('pure_prediction') or report['prediction'],
                'market_benchmark':report.get('market_benchmark'),
                'divergence':report.get('divergence'),
                'residual_prediction':report.get('residual_prediction'),
                'official_numeric_prediction':report.get('official_numeric_prediction') or report['prediction'],
                'official_market_adjustment':report.get('official_market_adjustment'),
                'prediction_method':report.get('prediction_method') or 'pure_basketball_fallback',
                'residual_model':report.get('residual_model'),
                'prediction_layers':report.get('prediction_layers'),
                'basketball_only_prediction':report.get('pure_prediction') or report['prediction'],
                'market_informed_prediction':report.get('market_informed_prediction'),
                'public_prediction':report['prediction'],'market_usage':report.get('market_usage'),
                'report':report}
            record['document_hash']=content_hash(record)
            temp=self.root/(uuid.uuid4().hex+'.tmp.json');save(temp,record)
            try:os.link(temp,path) # Atomic visibility and no overwrite, same directory/volume.
            finally:temp.unlink()
            return record
        finally:lock.unlink()
