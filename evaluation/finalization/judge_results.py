"""Verify one homogeneous final Judge cohort and aggregate actual labels."""
from collections import Counter
import statistics
from .common import *
from . import run,judge_final
from .judge import schema
from .contracts import ExactValidator

RECOVERY=BASE/'judge-final-v1-provider-recovery-01'
def load_labels():
    index=read(RECOVERY/'index.json');qualified={r['sample_id'] for r in read(HERE/'qualification.json')['rows'] if r['qualified_e2e']}
    if set(r['sample_id'] for r in index['rows'])!=qualified:raise ValueError('judge_cohort_incomplete_or_wrong')
    cfg=read(BASE/'judge-final-v1/contract.json');new_cfg=read(RECOVERY/'contract.json')
    if cfg!=new_cfg:raise ValueError('judge_recovery_protocol_changed')
    samples={s['sample_id']:s for s in run.verify()['samples']};labels=[]
    for row in index['rows']:
        path=ROOT/row['artifact_path']
        if digest(path.read_bytes())!=row['artifact_file_hash']:raise ValueError('judge_artifact_changed')
        a=read(path);s=samples[row['sample_id']]
        editorial=read(BASE/'samples'/s['sample_id']/'editorial/artifact.json')['parsed_output']
        if a['context_hash']!=s['context_hash'] or a['editorial_hash']!=digest(editorial):raise ValueError('judge_context_article_binding')
        request=read(path.parent/'request.json')
        if request['request_hash']!=digest(request['request']) or request['request_hash']!=a['request_hash']:raise ValueError('judge_request_binding')
        if len(request['request']['messages'])!=2 or request['request'].get('tools') or request['request'].get('functions'):raise ValueError('judge_tools_history')
        visible=json.loads(request['request']['messages'][1]['content']);check_visible(visible)
        expected_view=judge_final.compact_context(run.ctx_for(s))
        if visible['frozen_context']!=expected_view or visible['editorial']!=editorial:raise ValueError('judge_request_wrong_sample')
        if a['raw_schema_status']=='PASSED' and not a.get('error'):
            ExactValidator(schema()).validate(a['parsed_output']);labels.append((row,a))
    return index,labels

def summarize():
    index,labels=load_labels();rubric=read(ROOT/'evaluation/golden_set/v1/rubric.json')['pregame'];gates={};soft={};tags=Counter()
    for k in rubric['hard_gates']:
        counts=Counter(a['parsed_output']['hard_gates'][k]['value'] for row,a in labels)
        gates[k]={'PASS':counts['PASS'],'FAIL':counts['FAIL'],'NA':counts['NA'],'n_labels':len(labels)}
    for k in rubric['soft_metrics']:
        values=[a['parsed_output']['soft_metrics'][k]['score'] for row,a in labels]
        soft[k]={'n':len(values),'mean':round(statistics.mean(values),4) if values else None,'median':statistics.median(values) if values else None,'distribution':dict(Counter(values))}
    for row,a in labels:tags.update(a['parsed_output']['error_tags'])
    failures=[row for row in index['rows'] if not row['accepted']]
    return {'revision':'judge-final-v1','recovery_run':'provider-recovery-01','model':'deepseek-v4-flash','thinking':'disabled','temperature':0.1,'max_tokens':3500,
       'n_golden':20,'n_qualified_generation':index['n_eligible'],'n_judge_attempted':len(index['rows']),'n_accepted_labels':len(labels),
       'provider_success':sum(r['provider_status']=='SUCCEEDED' for r in index['rows']),'parse_success':sum(r['parse_status']=='PASSED' for r in index['rows']),
       'schema_success':sum(r['schema_status']=='PASSED' for r in index['rows']),'quote_exact_pass':sum(a.get('quote_audit_status')=='PASSED' for row,a in labels),
       'quote_warnings':sum(a.get('quote_audit_status')!='PASSED' for row,a in labels),'hard_gates':gates,'soft_metrics':soft,'error_tags':dict(tags),
       'no_hard_fail_count':sum(all(x['value']!='FAIL' for x in a['parsed_output']['hard_gates'].values()) for row,a in labels),
       'not_overall_quality_pass_threshold':True,'execution_fail_or_hold_content_NA':20-index['n_eligible'],'failures':failures,
       'source_index_hash':digest(index),'model_calls':0,'human_labels':0,'human_judge_agreement':None,
       'limitations':['Same provider/model family as generator; uncalibrated.','Scores conditional on14 gate-qualified generations, not20 or300.','Pro diagnostic scores excluded; no best-of-N.']}

def main():
    report=summarize();write(HERE/'closeout-judge-recovery-01/judge-summary.json',report);print(json.dumps(report,ensure_ascii=True))
if __name__=='__main__':main()
