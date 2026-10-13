"""Requalify frozen output without rewriting records or calling a model."""
from .common import *
from .contracts import CandidateForecast
from v2.pregame_system.forecasting_workflow import validate_independent_market_boundary

def main():
    old=read(BASE/'contract.json')['source_hashes']['run.py']
    write(HERE/'runtime-correction.json',{'created_at':now(),'reason':'clear tentative accepted on any independent validation exception; no model rerun or prompt/schema/Skill change',
      'files':{'run.py':{'original_hash':old,'corrected_hash':digest((HERE/'run.py').read_bytes()),'original_snapshot':'backups/run-original-frozen.py'}},
      'historical_contract_not_rewritten':True,'historical_manifests_not_rewritten':True,'not_a_new_generation_revision':True})
    rows=[]
    for p in sorted((BASE/'samples').glob('*/chain-manifest.json')):
        r=read(p);qualified=r['chain_status']=='SUCCEEDED';error=None
        if qualified:
            try:validate_independent_market_boundary(CandidateForecast.model_validate(read(p.parent/'accepted-forecast.json')))
            except Exception as e:qualified=False;error=type(e).__name__+':'+str(e)
        rows.append({'sample_id':r['sample_id'],'raw_chain_status':r['chain_status'],'qualified_e2e':qualified,'qualification_error':error,'original_manifest_hash':r['manifest_hash']})
    result={'n':20,'raw_e2e':sum(r['raw_chain_status']=='SUCCEEDED' for r in rows),'qualified_e2e':sum(r['qualified_e2e'] for r in rows),'rows':rows,
       'model_calls':0,'raw_artifacts_unchanged':True,'known_guard_false_positive':'No market or Pure baseline was provided... is an absence disclosure, not an observed market assertion; production guard is unchanged.'}
    write(HERE/'qualification.json',result)
    eligible={r['sample_id'] for r in rows if r['qualified_e2e']};tasks=read(HERE/'human/tasks.json');filtered=[t for t in tasks if t['meta']['sample_id'] in eligible]
    write(HERE/'human/tasks-qualified.json',filtered)
    ready=read(HERE/'human/ready.json');ready['prepared_articles']=len(filtered);ready['qualification_hash']=digest(result);ready['raw_articles']=len(tasks)
    write(HERE/'human/ready-qualified.json',ready);print(json.dumps({k:result[k] for k in ('n','raw_e2e','qualified_e2e')}))
if __name__=='__main__':main()
