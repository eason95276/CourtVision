"""Paired market benchmarks and all-history production-policy diagnostics."""
import json
import numpy as np
import pandas as pd
from .prediction import latest
from .experiment import TASKS
from .storage import save,new_run
from .market import benchmark_quotes,BOOK_ORDER
from courtvision.models import build_model,predict_for_task
from courtvision.metrics import classification_metrics,regression_metrics


def run():
    run=new_run('evaluation');experiment=latest('experiments','dataset.pkl');frame=pd.read_pickle(experiment)
    groups=json.loads(experiment.with_name('feature_groups.json').read_text())
    rows=[];policy_predictions=[]
    for season in [2022,2023,2024,2025]:
        train=frame[frame.season_start<season];test=frame[frame.season_start==season]
        for task,(target,name,_,_) in TASKS.items():
            model=build_model(task,name);model.fit(train[groups['baseline']],train[target])
            p=predict_for_task(model,task,test[groups['baseline']])
            scores=classification_metrics(test[target],p) if task=='win_probability' else regression_metrics(test[target],p)
            rows.append(dict(task=task,season=season,training_policy='all_available_history',train_rows=len(train),test_rows=len(test),**scores))
            policy_predictions.extend({'game_id':gid,'task':task,'prediction':float(v)} for gid,v in zip(test.game_id,p))
    pd.DataFrame(rows).to_csv(run/'production_policy_metrics.csv',index=False)
    pd.DataFrame(policy_predictions).to_csv(run/'production_policy_predictions.csv',index=False)
    try:quotes=pd.DataFrame(json.loads(latest('market_collection','quotes.json').read_text(encoding='utf8')))
    except ValueError:quotes=pd.DataFrame()
    results=[];coverage=[]
    if not quotes.empty:
        # Fixed provider priority ordering, separately for each Open/Close label.
        quotes=benchmark_quotes(quotes)
        pred=pd.read_csv(experiment.with_name('predictions.csv'),dtype={'game_id':str})
        predictions=pred[pred.variant=='baseline'].pivot(index='game_id',columns='task',values='prediction')
        paired=quotes.merge(frame[['game_id','season_start','home_win','point_margin','total_points']],on='game_id',validate='many_to_one').merge(predictions,on='game_id',validate='many_to_one')
        paired.to_json(run/'paired_market.json',orient='records',indent=2)
        for (season,label),part in paired.groupby(['season_start','quote_kind']):
            coverage.append({'season':int(season),'quote_kind':label,'games':len(part),'provider_counts':part.provider_name.value_counts().to_dict()})
            for task,marketcol in [('win_probability','home_probability_no_vig'),('expected_margin','expected_home_margin'),('expected_total','total')]:
                target=TASKS[task][0];sub=part.dropna(subset=[marketcol,task,target])
                if sub.empty:continue
                fn=classification_metrics if task=='win_probability' else regression_metrics
                for name,col in [('market',marketcol),('baseline',task)]:
                    results.append(dict(season=int(season),quote_kind=label,task=task,model=name,paired_games=len(sub),**fn(sub[target],sub[col])))
        pd.DataFrame(results).to_csv(run/'market_metrics.csv',index=False)
    save(run/'coverage.json',{'market':coverage,'benchmark_policy':'fixed bookmaker whitelist and order; one book per game for both Open/Close; excludes model providers and Live Odds',
        'book_order':BOOK_ORDER,
        'archive_is_pregame_available_proof':False,'market_feature_ablation':'unavailable: no timestamp-qualified quotes',
        'all_history_training_policy_evaluated':True,'diagnostic_not_unseen_holdout':2025})
    return run

if __name__=='__main__':print(run(),flush=True)
