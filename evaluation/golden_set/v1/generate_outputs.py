"""Dry-run job plans only. This file has no model API or production imports."""
import argparse,json,sys,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from asset_scanner import ROOT,EVAL,read_json,canonical_hash,sha
from build_eval_baseline import save,text

HERE=Path(__file__).resolve().parent

def exclusive_json(path,payload):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:
        json.dump(payload,f,ensure_ascii=False,indent=2);f.flush()

def dry_run():
    selection=read_json(HERE/'selection_manifest.json');skills={}
    for role in ('forecasting','editorial','postgame'):
        source=ROOT/'agent/skills'/role/'SKILL.md';target=HERE/'skill_snapshots'/(role+'.txt');target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and sha(target)!=sha(source):
            # Keep the previously frozen skill, do not replace it with today's skill.
            pass
        elif not target.exists():shutil.copyfile(source,target)
        skills[role]={'path':str(target.relative_to(HERE)),'sha256':sha(target)}
    prompts={
        'pregame':'只使用提供的固定Context。禁止联网、工具、Research和调用生产服务；不得用记忆中的本场赛果或后续事实。一般篮球机制可用于推理，但不引入Context之外的具体硬事实。赛前先形成独立判断，再按冻结写作规则组织分析；不能把历史候选写成确认首发。',
        'postgame':'只使用固定Context、真实冻结赛前输出与给定赛后证据。区分原判断和实际结果；不能用赛后信息重写赛前立场。没有逐回合资料不能补造比赛过程。禁止联网、工具、Research和生产服务。'}
    prompt_hashes={}
    for stage,prompt in prompts.items():
        path=HERE/'prompt_snapshots'/(stage+'.txt');path.parent.mkdir(parents=True,exist_ok=True)
        if not path.exists():text(path,prompt)
        prompt_hashes[stage]=sha(path)
    profile={'model_id':None,'model_revision':None,'backend':'operator_selected_offline_local_model_required',
        'generation_parameters':{'temperature':0.2,'top_p':1.0,'max_output_tokens':4096,'seed':20261010},
        'tools':[],'network_allowed':False,'research_enabled':False,'execution_enabled':False,
        'skill_snapshots':skills,'prompt_hashes':prompt_hashes,
        'stage_plan':{'pregame':['independent context-only forecast','freeze forecast and assumptions','editorial with locked forecast'],
            'postgame':['read same-sample frozen pregame response','read allowed postgame evidence','write postgame review']},
        'note':'No generation model chosen silently. Model/revision/local backend must be pinned in a new generation revision before execution.'}
    profile_path=HERE/'generation_profile.json'
    if profile_path.exists():profile=read_json(profile_path)
    else:save(profile_path,profile)
    generation_revision='golden-generation-prep-v1-'+canonical_hash(profile)[:20]
    jobs=[]
    for stage in ('pregame','postgame'):
        for task in read_json(HERE/(stage+'_import.json')):
            d=task['data'];model_inputs={'context':d['context']}
            if stage=='postgame':model_inputs.update(frozen_pregame_prediction=None,important_assumptions=None,ground_truth=d['ground_truth'],postgame_evidence=d['postgame_evidence'])
            missing=['pinned_offline_generation_model']
            if stage=='postgame':missing.append('real_same_sample_frozen_pregame_output')
            jobs.append({'sample_id':d['sample_id'],'stage':stage,'dataset_revision':selection['parent_dataset_id'],'golden_set_revision':selection['golden_set_revision'],
                'generation_revision':generation_revision,'model_inputs':model_inputs,'input_hash':canonical_hash(model_inputs),'prompt_hash':profile['prompt_hashes'][stage],
                'skill_hashes':{r:profile['skill_snapshots'][r]['sha256'] for r in (('forecasting','editorial') if stage=='pregame' else ('postgame',))},
                'parameters':profile['generation_parameters'],'tools':[],'network_allowed':False,'research_enabled':False,
                'execution_ready':False,'missing_prerequisites':missing,'output_path':'outputs/'+generation_revision+'/'+stage+'/'+d['sample_id']+'.json'})
    for job in jobs:
        assert job['tools']==[] and job['network_allowed'] is False and job['research_enabled'] is False
        assert job['input_hash']==canonical_hash(job['model_inputs'])
        if job['stage']=='pregame':assert 'ground_truth' not in job['model_inputs']
    save(HERE/'generation_dry_run.json',{'generation_revision':generation_revision,'jobs':jobs,'jobs_validated':len(jobs),'model_calls':0,'outputs_written':0,'execution_ready':False})
    print(json.dumps({'jobs_validated':len(jobs),'generation_revision':generation_revision,'execution_ready':False,'model_calls':0,'outputs_written':0}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dry-run',action='store_true',required=True);p.parse_args();dry_run()
