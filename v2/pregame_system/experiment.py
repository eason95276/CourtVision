"""Chronological paired ablations and separate all-history production artifacts."""
import json
import itertools
import joblib
import numpy as np
import pandas as pd
from . import ROOT
from .features import history,dataset
from .storage import new_run,save,digest,now
from courtvision.models import build_model,predict_for_task
from courtvision.metrics import classification_metrics,regression_metrics

TASKS={'win_probability':('home_win','logistic_regression',5,'log_loss'),
       'expected_margin':('point_margin','linear_regression',8,'mae'),
       'expected_total':('total_points','linear_regression',8,'mae')}


def configurations(groups):
    names=['matchup','schedule','cold'];result={'baseline':groups['baseline']}
    for n in range(1,4):
        for selection in itertools.combinations(names,n):
            result['+'.join(selection)]=groups['baseline']+[c for name in selection for c in groups[name]]
    return result


def select(metrics,task):
    primary=TASKS[task][3];rows=metrics[(metrics.task==task)&metrics.season.isin([2022,2023,2024])]
    baseline=rows[rows.variant=='baseline'].set_index('season')[primary]
    candidates=[]
    for variant,part in rows.groupby('variant'):
        loss=part.set_index('season')[primary];improvement=1-loss/baseline
        if variant!='baseline' and improvement.mean()>=.01 and (improvement>0).sum()>=2:
            candidates.append((float(improvement.mean()),variant))
    return max(candidates)[1] if candidates else 'baseline'


def run():
    folder=new_run('experiments');games,stats=history();frame,groups,audit=dataset(games,stats)
    frame.to_pickle(folder/'dataset.pkl');save(folder/'feature_groups.json',groups)
    save(folder/'history_audit.json',audit)
    configs=configurations(groups);metrics=[];predictions=[]
    for task,(target,model_name,window,primary) in TASKS.items():
        for variant,columns in configs.items():
            for season in [2022,2023,2024,2025]:
                train=frame[(frame.season_start<season)&(frame.season_start>=season-window)]
                test=frame[frame.season_start==season]
                model=build_model(task,model_name);model.fit(train[columns],train[target])
                values=predict_for_task(model,task,test[columns])
                scores=classification_metrics(test[target],values) if task=='win_probability' else regression_metrics(test[target],values)
                metrics.append(dict(task=task,variant=variant,season=season,rows=len(test),train_rows=len(train),**scores))
                early=(test.home_games_played_season<10)|(test.away_games_played_season<10)
                if early.any():
                    es=classification_metrics(test.loc[early,target],values[early]) if task=='win_probability' else regression_metrics(test.loc[early,target],values[early])
                    metrics[-1].update({'first10_'+k:v for k,v in es.items()})
                for gid,y,p in zip(test.game_id,test[target],values):
                    predictions.append(dict(game_id=gid,season=season,task=task,variant=variant,actual=float(y),prediction=float(p)))
            print(task,variant,'completed',flush=True)
    table=pd.DataFrame(metrics);table.to_csv(folder/'metrics.csv',index=False)
    pred=pd.DataFrame(predictions);pred.to_csv(folder/'predictions.csv',index=False)
    selection={task:select(table,task) for task in TASKS}
    metadata={'created_at':now(),'development_seasons':[2022,2023,2024],
       'diagnostic_only_season':2025,'diagnostic_is_new_unseen_holdout':False,
       'selection':selection,'selection_rule':'mean paired primary-loss improvement >= 1%, better in >=2/3 dev seasons',
       'unavailable_ablations':{'roster':'no reliable longitudinal player availability/minutes matrix',
           'market':'archive labels lack historical publication time; benchmark only'},
       'source_hashes':{str(p.relative_to(ROOT)):digest(p) for p in [ROOT/'data/raw/Games.csv',ROOT/'data/raw/TeamStatistics.csv']}}
    save(folder/'experiment.json',metadata)
    production=train_production(frame,configs,selection,metadata)
    save(folder/'production_reference.json',{'directory':str(production)})
    # Error rows are diagnostic artifacts, not a claim about why a model missed.
    bad=pred[pred.apply(lambda r:r['variant']==selection[r['task']],axis=1)].copy()
    bad['absolute_error']=(bad.actual-bad.prediction).abs()
    bad.sort_values('absolute_error',ascending=False).groupby('task').head(20).to_csv(folder/'bad_cases.csv',index=False)
    return folder


def train_production(frame,configs,selection,experiment):
    folder=new_run('production');models={}
    for task,(target,model_name,_,_) in TASKS.items():
        columns=configs[selection[task]];model=build_model(task,model_name)
        model.fit(frame[columns],frame[target]);path=folder/(task+'.joblib');joblib.dump(model,path)
        models[task]={'path':str(path),'sha256':digest(path),'feature_columns':columns,'variant':selection[task]}
    # All games on the last included date must have finished before this bound.
    last=pd.Timestamp(frame.game_date.max()).normalize()+pd.Timedelta(days=1,hours=12)
    cutoff=last.tz_localize('America/New_York').isoformat()
    save(folder/'model.json',{'version':'prediction-v2-'+folder.name,'trained_at':now(),
        'training_cutoff':cutoff,'training_max_game_date':str(frame.game_date.max()),
        'training_min_game_date':str(frame.game_date.min()),'training_rows':len(frame),
        'models':models,'experiment':experiment,'history_completeness':'available local competitive games only'})
    return folder


if __name__=='__main__':print(run(),flush=True)
