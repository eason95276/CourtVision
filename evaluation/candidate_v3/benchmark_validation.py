"""Static300 validation/freeze. Private mappings/provenance never model input."""
import csv,json,sys
from collections import Counter,defaultdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3.execution_v3 import v2
from asset_scanner import EVAL,read_json,sha,canonical_hash
from control import exclusive,now
from validate_closed_book_context import validate
from closed_book_anonymize import identity_leaks,anonymize

def run():
    folder=EVAL/'benchmark/v1';selection=read_json(folder/'selection_manifest.json');cfg=read_json(folder/'sampling_config.json')
    body={k:v for k,v in selection.items() if k not in ('selection_hash','revision')}
    if canonical_hash(body)!=selection['selection_hash'] or sha(folder/'selection.csv')!=selection['selection_csv_sha256'] or canonical_hash(cfg)!=selection['sampling_config_hash']:raise ValueError('selection freeze tamper')
    index={r['sample_id']:r for r in map(json.loads,(EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines())}
    freeze=read_json(EVAL/'reports/closed-book-freeze-manifest.json');rows=[];relationships=defaultdict(list)
    teams=Counter();phases=Counter();coverage=Counter()
    for selected in selection['samples']:
        sample=index[selected['sample_id']];ctxpath=EVAL/sample['context_path'];ctx=read_json(ctxpath);prov=read_json(EVAL/sample['provenance_path'])
        for key in ('context_path','provenance_path','anonymous_context_path','ground_truth_path'):
            p=EVAL/sample[key]
            if sha(p)!=freeze['files'][sample[key]]:raise ValueError('frozen file drift')
        anonymous=read_json(EVAL/sample['anonymous_context_path']);mappingpath=EVAL/'datasets/anonymized/mappings'/(sample['anonymous_sample_id']+'.json')
        mapping=read_json(mappingpath)
        if sha(mappingpath)!=freeze['files'][str(mappingpath.relative_to(EVAL))]:raise ValueError('mapping drift')
        issues=validate(ctx,prov,context_path=ctxpath,truth_path=EVAL/sample['ground_truth_path'],expected_context_hash=sample['context_sha256'])
        anonymous_issues=validate(anonymous,prov,mapping=mapping,expected_context_hash=canonical_hash(anonymous))
        residual=identity_leaks(anonymous,mapping);rebuilt,_=anonymize(ctx)
        if rebuilt!=anonymous:anonymous_issues.append('anonymous_transform_not_reproducible')
        row={'sample_id':sample['sample_id'],'context_hash':sample['context_sha256'],'normal_identity_binding':not issues,'temporal_event_date_valid':not issues,
            'anonymous_identity_leaks':residual,'anonymous_issues':anonymous_issues,'issues':issues,'gt_physically_separate':ctxpath.parent!=(EVAL/sample['ground_truth_path']).parent,
            'normal_variant_contains_real_names_by_design':True,'completeness':selected['context_completeness']}
        rows.append(row);teams.update(str(selected[k]) for k in ('home_team_id','away_team_id'));phases.update([selected['season_phase']]);coverage.update([str(selected['context_completeness']['available_components'])+'/7'])
        for proof in prov['fields'].values():
            for event in proof['source_events']:relationships[event['game_id']].append(sample['sample_id'])
    ids=[r['sample_id'] for r in rows]
    if len(ids)!=300 or len(set(ids))!=300 or any(r['issues'] or r['anonymous_issues'] or r['anonymous_identity_leaks'] for r in rows):raise ValueError('300 static validation failed')
    linked=[{'source_event_id':event,'sample_ids':sorted(set(samples))} for event,samples in relationships.items() if len(set(samples))>1]
    report={'created_at':now(),'n':300,'validated':300,'invalid':0,'unique_teams':len(teams),'team_counts':dict(teams),
        'max_team_appearances':max(teams.values()),'min_team_appearances':min(teams.values()),'phase_counts':dict(phases),'completeness_counts':dict(coverage),
        'shared_history_source_events':len(linked),'cross_sample_relations_note':'Overlapping prior histories make samples non-independent; no iid significance claim.',
        'identity_scan_scope':'Normal intentionally contains true identity; anonymous has0 explicit residuals, statistical fingerprints/parameter-memory remain possible.',
        'strict_point_in_time':False,'model_calls':0,'gt_content_opened':0,'gt_checks':'Byte hash and directory separation only; no outcomes read for validation.',
        'sampling_seed':cfg['seed'],'model_performance_used_for_selection':False,'season':'2025-26','type':'regular_season','rows':rows}
    exclusive(EVAL/'reports/benchmark-v1-readiness.json',report)
    exclusive(folder/'cross-sample-relations.json',{'relations':linked,'count':len(linked)})
    freeze_body={'created_at':now(),'parent_dataset_revision':selection['parent_dataset_revision'],'selection_revision':selection['revision'],
        'n':300,'selection_hash':selection['selection_hash'],'files':{name:sha(folder/name) for name in ('selection.csv','selection_manifest.json','sampling_config.json','cross-sample-relations.json')},
        'validation_report_sha256':sha(EVAL/'reports/benchmark-v1-readiness.json'),'model_calls':0,'status':'STATIC_READY','strict_pit':False}
    freeze_body['manifest_hash']=canonical_hash(freeze_body);freeze_body['benchmark_revision']='benchmark-v1-'+freeze_body['manifest_hash'][:20];exclusive(folder/'freeze_manifest.json',freeze_body)
    with (EVAL/'reports/benchmark-v1-readiness.md').open('x',encoding='utf-8') as f:
        f.write('# Benchmark V1 static readiness\n\n300/300 validated;30 teams;0 model calls.\n\n'+json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False,indent=2)+'\n\nFrozen revision: '+freeze_body['benchmark_revision']+'\n\nNot300 model evaluations, Human labels, strict PIT or memory-contamination proof.')
    print(json.dumps({'validated':300,'teams':len(teams),'min_team_n':min(teams.values()),'max_team_n':max(teams.values()),'phases':dict(phases),'cross_history_events':len(linked),'revision':freeze_body['benchmark_revision']}))
if __name__=='__main__':run()
