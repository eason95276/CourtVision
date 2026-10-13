"""Normalize actual Label Studio human records, not auto-generated labels."""
import argparse,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from asset_scanner import read_json
HERE=Path(__file__).resolve().parent

def individual(annotation,rubric):
    result={r['from_name']:r['value'] for r in annotation['result']};hard={};soft={}
    for name,rule in rubric['hard_gates'].items():
        value=result.get(name,{}).get('choices',[])
        if len(value)!=1 or value[0] not in ({'PASS','FAIL','NA'} if 'na' in rule else {'PASS','FAIL'}):raise ValueError('Incomplete/invalid hard gate: '+name)
        evidence=result.get(name+'_evidence',{}).get('text',[]);hard[name]={'value':value[0],'evidence':'\n'.join(evidence) if evidence else None}
    for name in rubric['soft_metrics']:
        scores=result.get(name,{}).get('choices',[])
        if len(scores)!=1 or scores[0] not in ['1','2','3','4','5']:raise ValueError('Incomplete/invalid soft score: '+name)
        soft[name]=int(scores[0])
    user=annotation['completed_by'];user=str(user['id'] if isinstance(user,dict) else user)
    return {'annotator_id':user,'annotation_id':annotation['id'],'created_at':annotation['created_at'],'hard_gates':hard,'soft_metrics':soft,
        'error_tags':result.get('error_tags',{}).get('choices',[]),'annotator_note':'\n'.join(result.get('annotator_note',{}).get('text',[])) or None}

def normalize(tasks,manifest,rubrics):
    bysample={r['sample_id']:r for r in manifest['samples']};labels=[];disagreements=[]
    for task in tasks:
        annotations=[a for a in task.get('annotations',[]) if not a.get('was_cancelled')]
        if not annotations:continue
        if not task['data'].get('model_output'):raise ValueError('Do not accept scores against awaiting_generation output')
        meta=task['meta'];sample=meta['sample_id'];stage=meta['stage'];row=bysample[sample]
        if meta['golden_set_revision']!=manifest['golden_set_revision'] or meta['dataset_revision']!=manifest['parent_dataset_id']:raise ValueError('Annotation revision mismatch')
        if stage=='postgame' and task['data'].get('frozen_pregame_prediction') is None:raise ValueError('Postgame assumption review requires true frozen Pregame basis')
        humans=[individual(a,rubrics[stage]) for a in annotations];humans.sort(key=lambda x:x['annotator_id'])
        ids=[a['annotator_id'] for a in humans]
        if len(ids)!=len(set(ids)):raise ValueError('Same-person re-annotation needs explicit batch selection; not annotator_b')
        if len(humans)>2:raise ValueError('More than two people; explicit adjudication workflow required')
        label={'sample_id':sample,'stage':stage,'dataset_revision':meta['dataset_revision'],'golden_set_revision':meta['golden_set_revision'],
            'rubric_revision':meta['rubric_revision'],'sampling_strata':{k:row[k] for k in ('selection_bucket','season_phase','hidden_outcome_bucket')},'annotator_a':humans[0]}
        if len(humans)==2:
            label['annotator_b']=humans[1]
            for field in ('hard_gates','soft_metrics','error_tags'):
                if humans[0][field]!=humans[1][field]:disagreements.append({'sample_id':sample,'stage':stage,'field':field,'annotator_a':humans[0][field],'annotator_b':humans[1][field],'adjudicated':None})
        labels.append(label)
    return labels,disagreements

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('raw_export');p.add_argument('output_directory');a=p.parse_args();dest=Path(a.output_directory).resolve()
    if HERE not in dest.parents:raise ValueError('Annotation output must stay within this evaluation workspace')
    tasks=read_json(a.raw_export);labels,disagreements=normalize(tasks,read_json(HERE/'selection_manifest.json'),read_json(HERE/'rubric.json'))
    dest.mkdir(parents=True,exist_ok=False)
    for name,data in [('raw-label-studio.json',tasks),('golden-labels.json',labels),('disagreements.json',disagreements)]:
        with (dest/name).open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)
    print('Real records normalized:',len(labels),'No agreement/overall-pass calculation')
