import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'execution'))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,read_json,canonical_hash
from control import exclusive,now

def main():
    rows=read_json(EVAL/'reports/golden-pregame-baseline-v1-full-rca.json')['rows'];fixes=[]
    definitions=[
        ('HISTORICAL_SCOPE','E. HISTORICAL_EVAL_ADAPTER','Live current identity cannot be proved by prior observations.','Historical same-side observed candidate only; no confirmed/source_expected/current identity upgrade.','Frozen history is present; live roster is intentionally absent.','Candidate evidence grounds a possibility, not current membership.',['positive','wrong side','unsupported','no confirmed upgrade']),
        ('STATUS_SCHEMA','C. PROMPT_SCHEMA_CONTRACT','Runtime cross-field status and evidence constraints were not fully reflected in JSON Schema.','Explicit analytical status for inference; no_clear empty; partial names permitted only with scope and grounded citations.','Applies to every context with uncertain lineup, not hard-coded teams.','Raw-schema metric changes definition; report separately and do not call all uplift model reasoning.',['status/list','partial lineup','missing/wrong citations']),
        ('SEMANTIC_LOCK','F. REPAIR_PIPELINE','Path-based preservation rejects unchanged nested uncertainties migrated to root.','Canonical lossless field migration; text, numbers and list members locked; reject genuine deletion.','Schema shape migration independent of sample identity.','Metadata completion must not invent facts; bounded one repair.',['migration','deletion','number/text/list mutants']),
        ('JSON_MODE','G. PROVIDER_FORMATTING','No response_format; one content string ends at invalid EOF with finish stop.','Official json_object mode; no regex recovery; log parse/provider failures without ordinary retry.','Wire-format setting for all calls.','JSON mode is not schema enforcement and can return empty content.',['malformed JSON','wire protocol','no retry'])]
    for ident,kind,current,desired,general,risk,tests in definitions:
        fixes.append({'fix_id':ident,'affected_samples':[r['sample_id'] for r in rows if r['primary_root_cause']==kind],
            'root_cause':kind,'current_behavior':current,'desired_behavior':desired,'why_general':general,'risk':risk,'regression_tests_needed':tests,
            'changes_production_semantics':False,'changes_candidate_contract_semantics':True})
    plan={'created_at':now(),'system_revision':'golden-pregame-system-candidate-v2','fixes':fixes,'shared_production_changes':[],
        'comparison_limit':'Paired execution reliability experiment; contract changed, not an isolated model-quality intervention. No GT consulted for fixes.'}
    exclusive(EVAL/'candidate_v2/candidate_fix_plan.json',plan)
    text='# Candidate V2 fixed design\n\n'+plan['comparison_limit']+'\n\n'
    for f in fixes:text+='## '+f['fix_id']+'\n\n'+json.dumps(f,ensure_ascii=False,indent=2)+'\n\n'
    with (EVAL/'candidate_v2/candidate_fix_plan.md').open('x',encoding='utf-8') as file:file.write(text)
if __name__=='__main__':main()
