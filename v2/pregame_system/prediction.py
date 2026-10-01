"""Cutoff-bound inference, independently fitted replays, immutable snapshots."""
import json
from pathlib import Path
import joblib
import numpy as np
import pandas as pd
from . import ROOT
from .storage import BASE,instant,now,save,new_run,digest,content_hash,clean
from .features import history,extra_features
from .experiment import TASKS
from courtvision.features import build_game_dataset
from courtvision.models import build_model,predict_for_task
from courtvision.data import TEAM_ID_TO_ABBR


def latest(folder,filename):
    paths=sorted((BASE/'outputs'/folder).glob('*/'+filename))
    if not paths:raise ValueError('missing_'+folder)
    return paths[-1]


def local_day(cutoff):
    local=pd.Timestamp(instant(cutoff)).tz_convert('America/New_York')
    day=local.normalize().tz_localize(None)
    # At early-morning cutoffs yesterday's late games may not yet be final.
    return day-pd.Timedelta(days=1) if local.hour<12 else day


def historical_identity(game):
    return {'game_id':game['game_id'],'home_team_id':int(game['home_team_id']),
        'away_team_id':int(game['away_team_id']),'home_team':game['home_team'],'away_team':game['away_team'],
        'scheduled_tipoff':pd.Timestamp(game['game_date']).tz_localize('America/New_York').isoformat(),
        'season_start':int(game['season_start']),'is_playoffs':int(game['is_playoffs']),
        'status':'historical_replay','identity_source':'local Games.csv (retrospective)'}


def feature_row(game,cutoff,games,stats,schedule=None):
    if instant(cutoff)>=instant(game['scheduled_tipoff']):raise ValueError('cutoff_not_pregame')
    day=local_day(cutoff)
    prior=games[(games.game_date.dt.normalize()<day)&(games.game_id!=game['game_id'])].copy()
    priorstats=stats[stats.game_id.isin(prior.game_id)].copy()
    if prior.empty:raise ValueError('no_prior_history')
    tip=pd.Timestamp(game['scheduled_tipoff']).tz_convert('America/New_York').tz_localize(None)
    target=dict(game,game_date=tip,gameType='Playoffs' if game['is_playoffs'] else 'Regular Season',
                home_abbr=TEAM_ID_TO_ABBR[game['home_team_id']],away_abbr=TEAM_ID_TO_ABBR[game['away_team_id']],
                home_win=np.nan,point_margin=np.nan,total_points=np.nan,homeScore=np.nan,awayScore=np.nan)
    # Empty target rows only anchor shift(1). They contain no fictional result.
    placeholders=[]
    for side,other,is_home in [('home','away',1),('away','home',0)]:
        row={c:np.nan for c in priorstats.columns}
        row.update(game_id=game['game_id'],gameId=game['game_id'],game_date=tip,gameDate=tip,
            season_start=game['season_start'],team_id=game[side+'_team_id'],teamId=game[side+'_team_id'],
            opponent_team_id=game[other+'_team_id'],opponentTeamId=game[other+'_team_id'],home=is_home,
            gameType=target['gameType'],win=np.nan)
        placeholders.append(row)
    joined=pd.concat([prior,pd.DataFrame([target])],ignore_index=True)
    st=pd.concat([priorstats,pd.DataFrame(placeholders)],ignore_index=True)
    base,columns=build_game_dataset(joined,st)
    extra,groups,audit=extra_features(pd.DataFrame([target]),priorstats)
    row=base[base.game_id==game['game_id']].merge(extra,on='game_id',validate='one_to_one')
    # Known scheduled games can determine rest/load without imaginary boxscores.
    for side in ['home','away']:
        tid=game[side+'_team_id'];known=[]
        for g in schedule or []:
            if g['game_id']==game['game_id'] or tid not in [g['home_team_id'],g['away_team_id']]:continue
            if g['season_start']!=game['season_start']:continue
            if not g.get('source') or instant(g['source']['captured_at'])>instant(cutoff):continue
            date=pd.Timestamp(g['scheduled_tipoff']).tz_convert('America/New_York').tz_localize(None)
            if day<=date.normalize()<tip.normalize():known.append((date,g['home_team_id']==tid,g['game_id']))
        if known:
            completed=[(r.game_date,bool(r.home),r.game_id) for r in priorstats[priorstats.team_id==tid].itertuples() if r.season_start==game['season_start']]
            calendar=sorted(completed+known);dates=[d.normalize() for d,_,_ in calendar]
            rest=min(7,(tip.normalize()-dates[-1]).days)
            row[f'{side}_rest_days']=rest;row[f'{side}_back_to_back']=int(rest<=1)
            row[f'{side}_games_played_season']=len(calendar)
            row[f'cold_{side}_current_games']=len(calendar)
            row[f'schedule_{side}_games_in_three_days']=1+sum(0<(tip.normalize()-d).days<=2 for d in dates)
            row[f'schedule_{side}_games_in_four_days']=1+sum(0<(tip.normalize()-d).days<=3 for d in dates)
            road=1 if side=='away' else 0
            if road:
                for _,home,_ in reversed(calendar):
                    if home:break
                    road+=1
            row[f'schedule_{side}_consecutive_away']=road
            audit[0][side]['scheduled_load_game_ids']=[gid for _,_,gid in known]
            audit[0][side]['schedule_assumption']='scheduled preceding games occur as currently listed; no projected outcome/statistics'
    for suffix in ['rest_days','back_to_back','games_played_season']:
        row['diff_'+suffix]=row['home_'+suffix]-row['away_'+suffix]
    return row,{'baseline':columns,**groups},audit[0],prior


class Predictor:
    def __init__(self,production=None,experiment=None,runtime_root=None):
        from .runtime_assets import DEMO_ROOT, RuntimeAssetError, asset_error, verify_runtime_file
        self.demo_context=None;self.runtime_root=None
        if runtime_root is None and production is None:
            try:production=latest('production','model.json')
            except ValueError:runtime_root=DEMO_ROOT
        try:
            if runtime_root is not None:
                self.runtime_root=Path(runtime_root)
                self.production=verify_runtime_file(self.runtime_root,'pure/model.json')
                context_path=verify_runtime_file(self.runtime_root,'context.json')
                self.demo_context=json.loads(context_path.read_text(encoding='utf8'))
                self.source_paths=[context_path]
            else:
                self.production=Path(production)
                self.source_paths=[ROOT/'data/raw/Games.csv',ROOT/'data/raw/TeamStatistics.csv']
            self.metadata=json.loads(self.production.read_text(encoding='utf8'))
            for info in self.metadata['models'].values():
                path=Path(info['path'])
                if not path.is_absolute():path=self.production.parent/path
                if self.runtime_root:
                    verify_runtime_file(self.runtime_root,path.relative_to(self.runtime_root).as_posix())
                if not path.is_file() or digest(path)!=info['sha256']:raise asset_error(str(path))
                info['path']=str(path.resolve())
            if self.demo_context:
                self.games=pd.DataFrame();self.stats=pd.DataFrame()
            else:self.games,self.stats=history()
        except RuntimeAssetError:
            raise
        except (OSError,ValueError,KeyError,TypeError) as exc:
            raise asset_error(str(exc)) from exc
        self.experiment=Path(experiment) if experiment else None
        self._replays={}
        self.schedule=[]

    def prepare_features(self,game,cutoff,replay=False):
        if self.demo_context is None:
            return feature_row(game,cutoff,self.games,self.stats,self.schedule if not replay else None)
        context=self.demo_context
        keys=['game_id','home_team_id','away_team_id','scheduled_tipoff']
        if replay or any(game.get(key)!=context['game'][key] for key in keys) or instant(cutoff)!=instant(context['cutoff']):
            raise ValueError('demo_snapshot_scope_mismatch: use python -m v2.pregame_system.demo')
        return (pd.DataFrame([context['feature_values']]),context['groups'],context['history_audit'],
                pd.DataFrame({'game_date':[pd.Timestamp(context['history_latest_game'])]}))

    def replay_models(self,cutoff):
        if self.experiment is None:
            from .runtime_assets import asset_error
            try:self.experiment=latest('experiments','dataset.pkl')
            except ValueError as exc:raise asset_error('historical replay needs full training data; demo uses a frozen feature snapshot') from exc
        day=local_day(cutoff);key=str(day)
        if key in self._replays:return self._replays[key]
        frame=pd.read_pickle(self.experiment);train=frame[frame.game_date.dt.normalize()<day]
        if len(train)<100:raise ValueError('insufficient_training_history')
        groups=json.loads(self.experiment.with_name('feature_groups.json').read_text())
        cols=groups['baseline'];models={};folder=new_run('replay_models')
        for task,(target,name,_,_) in TASKS.items():
            model=build_model(task,name);model.fit(train[cols],train[target]);p=folder/(task+'.joblib');joblib.dump(model,p)
            models[task]={'path':str(p),'sha256':digest(p),'feature_columns':cols,'variant':'baseline_replay'}
        meta={'version':'retrospective-replay-'+folder.name,'models':models,'training_rows':len(train),
              'training_cutoff':(pd.Timestamp(day)+pd.Timedelta(hours=12)).tz_localize('America/New_York').isoformat(),
              'training_max_game_date':str(train.game_date.max()),'trained_at':now(),
              'selection_policy':'fixed V1 model classes and baseline columns; no V2 future-season selection',
              'retrospective_model_design':True}
        save(folder/'model.json',meta);self._replays[key]=meta
        return meta

    def predict(self,game,cutoff,*,replay=False,optional=None):
        generated=self.demo_context['cutoff'] if self.demo_context else now()
        if instant(cutoff)>instant(generated):raise ValueError('cutoff_in_future')
        if not replay and instant(generated)>=instant(game['scheduled_tipoff']):raise ValueError('live_snapshot_after_tipoff')
        if not replay and game.get('status')!='scheduled':raise ValueError('game_not_scheduled')
        source=game.get('source')
        if source and instant(source['captured_at'])>instant(cutoff):raise ValueError('schedule_not_known_at_cutoff')
        meta=self.replay_models(cutoff) if replay else self.metadata
        if instant(meta['training_cutoff'])>instant(cutoff):raise ValueError('model_trained_after_cutoff')
        if optional:raise ValueError('unvalidated_optional_inputs_not_supported')
        row,groups,audit,prior=self.prepare_features(game,cutoff,replay)
        for side in ['home','away']:
            if audit[side]['recent_games']<3:raise ValueError('insufficient_team_history')
        result={};used={}
        for task,info in meta['models'].items():
            if digest(info['path'])!=info['sha256']:raise ValueError('model_integrity_mismatch')
            columns=info['feature_columns']
            if not set(columns)<=set(row):raise ValueError('feature_schema_mismatch')
            model=joblib.load(info['path']);value=float(predict_for_task(model,task,row[columns])[0])
            if not np.isfinite(value):raise ValueError('nonfinite_prediction')
            result[task]=value;used[task]=clean(row[columns].iloc[0].to_dict())
        result['perspective']='home';result['is_forecast']=True
        result['away_win_probability']=1-result['win_probability']
        result['expected_home_score']=(result['expected_total']+result['expected_margin'])/2
        result['expected_away_score']=(result['expected_total']-result['expected_margin'])/2
        inputs={'game':game,'cutoff':cutoff,'features':used,'model_version':meta['version'],
                'history_audit':audit,'optional_inputs':optional or {},
                'feature_version':{'adapter_sha256':digest(Path(__file__)),
                    'v2_features_sha256':digest(BASE/'features.py'),'v1_features_sha256':digest(ROOT/'src/courtvision/features.py')},
                'source_hashes':{str(p):digest(p) for p in self.source_paths}}
        inputs['feature_schema_hash']=content_hash({k:list(v) for k,v in used.items()})
        snapshot={'snapshot_id':None,'purpose':'audit_eval','official_prediction':False,'cutoff':cutoff,'generated_at':generated,
          'historical_replay':replay,'originally_captured_forecast':False if replay else True,
          'model_version':meta['version'],'training_cutoff':meta['training_cutoff'],
          'model_hashes':{k:v['sha256'] for k,v in meta['models'].items()},
          'input_hash':content_hash(inputs),'history_latest_game':str(prior.game_date.max()),
          'data_completeness':{'team_history':True,'roster_model_inputs':False,'timestamp_qualified_market':False},
          'generation_context':{'availability_adjustment':None,'long_absence_second_discount':False,
              'future_scheduled_load_used_without_future_results':any(audit[s].get('scheduled_load_game_ids') for s in ['home','away'])},
          'historical_source_semantics':'retrospectively acquired boxscores; dates before cutoff ET date; before noon also withhold yesterday; no original ingestion-time or per-row publication-time proof'}
        if self.demo_context:
            snapshot.update(demo_runtime=True,originally_captured_forecast=False,purpose='offline_demo',
                historical_source_semantics='frozen real pregame features; simulated demo clock; never an official publication')
        folder=new_run('snapshots');snapshot['snapshot_id']=folder.name
        save(folder/'inputs.json',inputs);save(folder/'snapshot.json',snapshot);save(folder/'prediction.json',result)
        save(folder/'model_reference.json',meta)
        save(folder/'integrity.json',{'prediction_sha256':digest(folder/'prediction.json'),
            'input_hash':snapshot['input_hash'],'model_reference_sha256':digest(folder/'model_reference.json')})
        return {'game':game,'snapshot':snapshot,'prediction':result,'history':audit,'feature_values':clean(row.iloc[0].to_dict()),'path':str(folder)}
