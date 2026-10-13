"""Sampling-only process. Outcome strata never reach generation or human UI."""
import csv,json,random,sys,copy
from collections import Counter,defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'golden_set/v1'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'execution'))
from asset_scanner import EVAL,read_json,sha,canonical_hash
from control import exclusive,now
from evaluation_integrity import verify_closedbook
from select_samples import phase,features,outcome
from validate_closed_book_context import validate
from counterfactual import record,apply

def quotas(counts,n):
    total=sum(counts.values());q={k:int(n*v/total) for k,v in counts.items()}
    remainder=n-sum(q.values())
    order=sorted(counts,key=lambda k:(-(n*counts[k]/total-q[k]),str(k)))
    for k in order[:remainder]:q[k]+=1
    return q

def benchmark():
    destination=EVAL/'benchmark/v1'
    if destination.exists():raise ValueError('benchmark destination exists; no overwrite')
    verify_closedbook(EVAL);cfg=read_json(EVAL/'golden_set/v1/sampling_config.json')
    sampling={'seed':20261012,'representative':240,'challenge':60,'total':300,'phase_definition':cfg['phase_definition'],
        'outcome_strata':'Sampling backend only: abs margin <=5 / <=14 / >14; never model-visible.',
        'performance_independent':True,'model_outputs_consulted':False,'risk_anchors':0}
    # Lock the sampling specification before any outcome-bucket reads.
    exclusive(destination/'sampling_config.json',sampling)
    pool=[];excluded=[]
    for row in map(json.loads,(EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()):
        if row['season']!='2025-26' or row['game_type']!='regular_season':continue
        ctx=read_json(EVAL/row['context_path']);prov=read_json(EVAL/row['provenance_path'])
        issues=validate(ctx,prov,context_path=EVAL/row['context_path'],truth_path=EVAL/row['ground_truth_path'],expected_context_hash=row['context_sha256'])
        if issues:excluded.append({'sample_id':row['sample_id'],'issues':issues});continue
        # Authorized private sampling use only. Keep neither actual numbers nor winners in selection.
        gt=read_json(EVAL/row['ground_truth_path']);bucket=outcome(gt,cfg)
        if gt['target_game_id']!=row['target_game_id']:raise ValueError('sampling identity conflict')
        ph,ordinal=phase(ctx)
        allowed=('sample_id','target_game_id','game_date','season','game_type','home_team','away_team','home_team_id','away_team_id','context_path','context_sha256','context_completeness')
        pool.append({**{k:row[k] for k in allowed},'season_phase':ph,'hidden_outcome_bucket':bucket,'challenge_features':list(features(ctx,cfg))})
    if len(pool)<300:raise ValueError('insufficient eligible candidates')
    rng=random.Random(sampling['seed']);groups=defaultdict(list)
    for row in sorted(pool,key=lambda x:x['sample_id']):groups[(row['season_phase'],row['hidden_outcome_bucket'])].append(row)
    selected=[]
    for key,n in quotas({k:len(v) for k,v in groups.items()},240).items():
        for row in rng.sample(groups[key],n):selected.append({**row,'selection_bucket':'representative'})
    used={r['sample_id'] for r in selected};options=[r for r in pool if r['sample_id'] not in used and r['challenge_features']];rng.shuffle(options)
    reasons=Counter();teams=Counter(str(r[k]) for r in selected for k in ('home_team_id','away_team_id'))
    # Diversity of observable challenges only, not numeric prediction error.
    while len(selected)<300 and options:
        index=max(range(len(options)),key=lambda i:(sum(reasons[k]==0 for k in options[i]['challenge_features']),sum(teams[str(options[i][k])]==0 for k in ('home_team_id','away_team_id'))))
        row=options.pop(index);selected.append({**row,'selection_bucket':'challenge'});reasons.update(row['challenge_features']);teams.update(str(row[k]) for k in ('home_team_id','away_team_id'))
    if len(selected)!=300 or len(teams)!=30:raise ValueError('selection quality constraint not met')
    selected.sort(key=lambda r:(r['game_date'],r['sample_id']))
    with (destination/'selection.csv').open('x',encoding='utf-8',newline='') as f:
        fields=['sample_id','target_game_id','game_date','home_team','away_team','season_phase','selection_bucket','context_path','context_sha256']
        writer=csv.DictWriter(f,fieldnames=fields);writer.writeheader();writer.writerows({k:r[k] for k in fields} for r in selected)
    manifest={'created_at':now(),'parent_dataset_revision':cfg['parent_dataset_id'],'sampling_config_hash':canonical_hash(sampling),'samples':selected,
        'eligible_pool':len(pool),'excluded':excluded,'selection_csv_sha256':sha(destination/'selection.csv'),'model_calls':0,
        'representative_strata':dict(Counter(str((r['season_phase'],r['hidden_outcome_bucket'])) for r in selected if r['selection_bucket']=='representative')),
        'phase_counts':dict(Counter(r['season_phase'] for r in selected)),'team_counts':dict(teams),'challenge_counts':dict(reasons),
        'limit':'Structural closed-book coverage; not strict point-in-time archive, random simple sample, Human Golden labels or evaluated300.'}
    manifest['selection_hash']=canonical_hash(manifest);manifest['revision']='benchmark-selection-v1-'+manifest['selection_hash'][:20]
    exclusive(destination/'selection_manifest.json',manifest)
    with (EVAL/'reports/benchmark-v1-selection.md').open('x',encoding='utf-8') as f:
        f.write('# Benchmark V1 selection candidate\n\n300 selected / '+str(len(pool))+' eligible; 240 representative + 60 challenge; 30 teams; 0 LLM calls.\n\nSampling specification frozen before private outcome strata reads. CSV omits hidden outcomes. Private manifest retains buckets for sampling audit; neither is a Pregame UI import. Model performance/Bad Case output never used in ranking.\n\nPhase counts: '+json.dumps(manifest['phase_counts'])+'\n\n'+manifest['limit'])
    return {'selected':300,'eligible':len(pool),'teams':30,'phase_counts':manifest['phase_counts'],'llm_calls':0}

def synthetic():
    destination=EVAL/'datasets/counterfactual_candidate_v1'
    if destination.exists():raise ValueError('synthetic destination exists; no overwrite')
    selected=read_json(EVAL/'golden_set/v1/selection_manifest.json')['samples'];selected.sort(key=lambda s:(s['game_date'],s['sample_id']));mutations=[]
    for base in selected[:5]:
        ctx=read_json(EVAL/base['context_path'])
        star=ctx['team_a']['player_history']['players'][0]['player_name'] if ctx['team_a']['player_history']['players'] else 'SYNTHETIC_HOME_CORE'
        changes=[('availability','/team_a/availability',{'status':'synthetic','players':[{'name':star,'availability':'out','impact_assumption':'major home contributor'}]},'Compared with a separate synthetic AVAILABLE control, home win probability should not materially increase absent a compensating mechanism. Direction is a diagnostic assumption, not historical truth.'),
            ('rest','/schedule_context/team_a',{'days_rest':0,'back_to_back':True,'status':'synthetic','assumption':'completed previous-day game'},'Compared with synthetic rested control, no unexplained home confidence uplift; effect can be overridden with an explicit basketball mechanism.'),
            ('venue','/matchup/venue',{'home_court_advantage':'away','status':'synthetic','assumption':'only venue switched, identities unchanged'},'Venue-only reversal should be reflected or explained; no exact required numerical delta.'),
            ('recent_form','/team_a/recent_form',{'status':'synthetic','recent_5':{'sample_games':5,'win_pct':1.0,'net_rating':{'value':20.0}},'assumption':'one-factor stronger observed form'},'Stronger form alone should not create an unexplained reduction in home confidence; long-term strength may dominate.'),
            ('market','/market',{'status':'synthetic','home_spread':-8.5,'total':230.5},'Independent Forecast unchanged when synthetic market withheld; review may reconsider; deterministic ATS direction can change. No command to follow market.')]
        for kind,path,value,relation in changes:
            keys=path.strip('/').split('/');parent=ctx
            for key in keys[:-1]:parent=parent[key]
            original=copy.deepcopy(parent.get(keys[-1]));rec=record(base['sample_id'],kind,path,original,value,'SYNTHETIC ONLY. This is not a report about a real injury, schedule, venue or market.',relation)
            control=copy.deepcopy(ctx)
            if kind=='availability':control['team_a']['availability']={'status':'synthetic','players':[{'name':star,'availability':'available','impact_assumption':'major home contributor'}]}
            elif kind=='rest':control['schedule_context']['team_a']={'days_rest':2,'back_to_back':False,'status':'synthetic'}
            elif kind=='market':control['market']={'status':'synthetic','home_spread':-3.5,'total':230.5}
            # Mutation log original matches the control, not silently the historical record.
            parent=control
            for key in keys[:-1]:parent=parent[key]
            rec=record(base['sample_id'],kind,path,copy.deepcopy(parent.get(keys[-1])),value,rec['notes'],relation)
            control['source_type']='synthetic_counterfactual';changed=apply(control,rec)
            if canonical_hash(control)==canonical_hash(changed):raise ValueError('no-op synthetic mutation')
            case={'mutation':rec,'base_context_hash':base['context_sha256'],'control_context':control,'mutated_context':changed,
                'validation_scope':'Explicit synthetic extension, not valid Frozen Closed-book V1 or real injury/market truth; LLM calls 0.'}
            exclusive(destination/(rec['mutation_id']+'.json'),case);mutations.append({'mutation_id':rec['mutation_id'],'kind':kind,'sha256':sha(destination/(rec['mutation_id']+'.json'))})
    exclusive(destination/'manifest.json',{'created_at':now(),'count':len(mutations),'synthetic':True,'llm_calls':0,'cases':mutations,'human_ground_truth':False})
    with (EVAL/'reports/counterfactual-candidate-v1.md').open('x',encoding='utf-8') as f:f.write('# Synthetic metamorphic candidates\n\n25 pairs: 5 each availability/rest/venue/form/market. No model calls. Controls may include explicitly invented conditions; not historical observations. Relations are reviewable directional hypotheses, not exact outcome labels. Do not use this extension as Golden V1 Context or real injury truth.')
    return {'synthetic_pairs':len(mutations),'llm_calls':0}

if __name__=='__main__':
    command=sys.argv[1]
    print(json.dumps(benchmark() if command=='benchmark' else synthetic(),ensure_ascii=True))
