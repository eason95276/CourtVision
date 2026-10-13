"""Per-case RCA + protection. Never opens target Ground Truth content."""
import json,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v2 import run as v2
from control import exclusive,now
from asset_scanner import EVAL,ROOT,read_json,sha,canonical_hash
from pydantic import ValidationError

def protection():
    refs=[]
    for base in ('v2/pregame_system','v2/postgame_system','v2/agent','v2/frontend/static','agent/skills'):
        refs.extend(p for p in (ROOT/base).rglob('*') if p.is_file() and p.suffix in ('.py','.js','.css','.html','.md'))
    refs.append(ROOT/'v2/frontend/server.py')
    for base in (v2.b.BASE,v2.BASE,EVAL/'candidate_v2'):
        refs.extend(p for p in base.rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix not in ('.pyc',))
    for name in ('golden-pregame-baseline-v1.json','golden-pregame-baseline-v1.md','golden-pregame-v1-v2-regression.json','golden-pregame-v1-v2-regression.md'):
        refs.append(EVAL/'reports'/name)
    return {'production_and_frozen':v2.b.root_protection(),'files':{str(p.relative_to(ROOT)):sha(p) for p in set(refs)}}

def get_value(obj,path):
    for k in path:obj=obj[k]
    return obj

def run():
    v2.verify();exclusive(EVAL/'reports/mega-protection-before.json',protection())
    rows=[];lengths=[];causes=Counter()
    for sample in v2.b.selection():
        folder=v2.BASE/'samples'/sample['sample_id'];chain=read_json(folder/'chain-manifest.json')
        if chain['chain_status']=='SUCCEEDED':continue
        original=read_json(folder/'forecast/artifact.json');raw=original['parsed_output'];ctx=v2.b.context_for(sample);details=[]
        try:v2.c.CandidateForecast.model_validate(raw)
        except ValidationError as exc:
            for err in exc.errors(include_url=False):
                if err['type']=='string_too_long':
                    value=get_value(raw,err['loc']);detail={'field':list(err['loc']),'length':len(value),'limit':err['ctx']['max_length'],'excess':len(value)-err['ctx']['max_length'],'exact_text':value}
                    details.append(detail);lengths.append(detail)
        index=v2.c.catalog(ctx);allowed={e['id'] for e in index['entries']};citations=[];unknown=[]
        def walk(x,path=''):
            if isinstance(x,dict):
                for k,val in x.items():
                    if k in ('evidence_ids','fact_ids') and isinstance(val,list):unknown.extend({'path':path+'/'+k,'id':i} for i in val if i not in allowed)
                    walk(val,path+'/'+k)
            elif isinstance(x,list):
                for i,val in enumerate(x):walk(val,path+'/'+str(i))
        walk(raw)
        for item in (raw.get('pregame_plan') or{}).get('lineups',[]):
            names=item.get('players',[])+item.get('candidates',[]);side=item['side'];group='context.team_a' if side=='home' else 'context.team_b'
            for name in names:
                member=index['names'][side].get(v2.c.name_key(name))
                if member and group not in item.get('evidence_ids',[]) and member['id'] not in item.get('evidence_ids',[]):
                    citations.append({'name':name,'side':side,'exists_in_same_frozen_side':True,'exact_pointer':member['paths'],'expected_id':member['id'],'actual_ids':item.get('evidence_ids',[])})
        if details:
            primary='PROMPT_CONTRACT';why='Length caps are explicit in schema but V2 prompt lacked operational per-field budgets and Chinese-output instruction. Generated English paragraphs concatenate qualifications, mechanisms and caveats into bounded scalar fields.'
            contributing=['MODEL_GENERATION: ignores schema maxLength','REPAIR_PIPELINE: exact text lock forbids shortening, so repair cannot satisfy two simultaneous constraints']
            fix='Chinese compact per-field targets below original caps; JSON schema remains unchanged. Exact canonical formatting only; do not certify free paraphrase.'
        elif citations:
            primary='EVIDENCE_CONTRACT';why='All rejected names exist on the frozen side. Non-empty IDs cover a subset; V2 only fills wholly missing IDs. This checks representation coverage, not absence of the candidate evidence.'
            contributing=['EVALUATION_ADAPTER: does not complete partial per-player references','MODEL_GENERATION: selected more names than its citation list covers']
            fix='Resolve each already selected exact same-side name to its frozen pointer and append missing known IDs; retain raw IDs/text/names. Unknown/wrong-side names still fail.'
        else:
            primary='PROMPT_CONTRACT';why='Output includes an alias absent from the per-sample catalog; prompt advertises a list but schema uses unconstrained evidence strings.'
            contributing=['MODEL_GENERATION: unknown alias','SCHEMA_CONTRACT: legal IDs not enumerated']
            fix='Enumerate legal per-sample IDs locally/in schema, use simple section aliases in prompt; reject any unknown ID, no fuzzy match.'
        causes[primary]+=1
        repair=read_json(folder/'repair/artifact.json') if (folder/'repair/artifact.json').exists() else None
        rows.append({'sample_id':sample['sample_id'],'game':sample['away_team']+' @ '+sample['home_team'],'failure_symptom':chain['error'],
            'first_failing_gate':'RAW_SCHEMA' if original['schema_status']=='FAILED' else 'HISTORICAL_EVIDENCE_ACCEPTANCE',
            'model_output_related':{'lengths':details,'missing_player_coverage':citations,'unknown_ids':unknown},
            'contract_requirement':{'forecast_schema_hash':read_json(v2.BASE/'contract.json')['forecast_schema_hash'],'source':'evaluation/candidate_v2/contracts.py:validate_historical / preserve_semantics','same_side_identity_required':True},
            'root_cause':primary,'root_cause_reason':why,'contributing_factors':contributing,'recommended_generic_fix':fix,
            'regression_risk':'Do not allow unknown names, unknown IDs, current identity upgrade, text/number changes or silently enlarge limits.',
            'repair_if_any':{'schema_status':repair['schema_status'],'error':repair['error']} if repair else None,
            'evidence':[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in [folder/'forecast/artifact.json',folder/'forecast/request.json',folder/'chain-manifest.json']],
            'confidence':'high for observed mechanism; medium for prompt causality; not independent Human RCA'})
    report={'created_at':now(),'revision':v2.REV,'failed_samples':len(rows),'primary_distribution':dict(causes),'rows':rows,
        'length_analysis':{'violations':len(lengths),'field_counts':dict(Counter('.'.join(map(str,x['field'])) for x in lengths)),'max_excess':max(x['excess'] for x in lengths)},
        'provider_json_mode_already_enabled':True,'gt_content_opened':0}
    exclusive(EVAL/'reports/candidate-v2-failure-rca.json',report)
    text='# Candidate V2 fifteen-case failure RCA\n\nEngineering attribution with alternatives; not a sole-cause experiment or Human label. Original caps retained in V3.\n\n'+json.dumps({k:v for k,v in report.items() if k!='rows'},ensure_ascii=False,indent=2)+'\n'
    for row in rows:text+='\n## '+row['sample_id']+' · '+row['game']+'\n\n'+json.dumps(row,ensure_ascii=False,indent=2)+'\n'
    with (EVAL/'reports/candidate-v2-failure-rca.md').open('x',encoding='utf-8') as f:f.write(text)
    print(json.dumps({'failures':len(rows),'root_causes':dict(causes),'lengths':report['length_analysis']},ensure_ascii=True))
if __name__=='__main__':run()
