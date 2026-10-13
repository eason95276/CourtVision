"""Evidence-based overnight RCA, not labels or basketball correctness scoring."""
import csv,json,sys
from collections import Counter
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'execution'))
import run_baseline_v1 as baseline
from asset_scanner import EVAL,read_json,sha,canonical_hash
from control import exclusive,now


def historical_names(context):
    return {side:{p['player_name'] for key in ('player_history','recent_rotation_candidates')
        for p in context['team_a' if side=='home' else 'team_b'][key].get('players',[]) if p.get('recorded_appearances',0)>0}
        for side in ('home','away')}


def analyze():
    baseline.assert_contract();rows=[]
    for sample in baseline.selection():
        folder=baseline.BASE/'samples'/sample['sample_id'];final=read_json(folder/'final-record.json')
        context=baseline.context_for(sample);names=historical_names(context)
        if sample['sample_id']==baseline.ts.SAMPLE:
            path=baseline.recovery.PARENT;a=read_json(path);packet=a['exact_model_visible_request'];out=json.loads(json.loads(a['raw_response'])['choices'][0]['message']['content'])
            error={'loc':['analysis_core','uncertainties'],'msg':'Extra inputs are not permitted'};errs=[error]
        else:
            path=folder/'forecasting/artifact.json';a=read_json(path);packet=read_json(folder/'forecasting/request.json')['exact_model_visible_request'];out=a['parsed_output'];errs=a.get('validation_errors',[])
        evidence=[{'source_path':str(path.relative_to(baseline.ROOT)),'sha256':sha(path),'field':'raw_response/model content',
            'context_path':sample['context_path'],'context_hash':sample['context_sha256']},
            {'source_path':str((folder/'final-record.json').relative_to(baseline.ROOT)),'sha256':sha(folder/'final-record.json'),'field':'failure_code/failure_stage'}]
        kind=final['failure_type'];detail={};confidence='high';secondary=[]
        if kind=='FORECAST_ACCEPTANCE_ERROR':
            name=final['failure_code'].split(':',1)[1]
            lineup=next(x for x in out['pregame_plan']['lineups'] if name in x.get('players',[])+x.get('candidates',[]))
            side=lineup['side'];supported=name in names[side]
            primary='E. HISTORICAL_EVAL_ADAPTER' if supported else 'B. MODEL_GENERATION'
            detail={'failed_name':name,'declared_side':side,'basis':lineup['basis'],'same_side_historical_observation':supported,
                    'current_roster_in_frozen_context':False,'evidence_ids':lineup.get('evidence_ids',[])}
            claim=('Frozen输入在该侧确有此人的此前出场记录，模型以agent_expected引用历史；live current_member只接受current roster/合格current web资料，历史adapter没有提供对应身份作用域。'
                if supported else '被拒姓名未被该侧冻结历史观察支持；不能仅因资料不完整而把unsupported姓名当合法historical candidate。')
            secondary=['B. MODEL_GENERATION: 输入interpretation明确不证明target roster，但模型仍填写live-shaped lineup。',
                       'C. PROMPT_SCHEMA_CONTRACT: agent_expected没有显式历史身份scope；current/historical资格不能从同一basis字段区分。']
            alternatives='不是已证实的真实球队归属错误；没有查询GT/当日名单。API/JSON/schema已成功。缺current roster是数据设计边界，不能把缺失自动叫污染。'
            confidence='high' if supported else 'medium'
            code='v2/pregame_system/pregame_estimates.py:98-151';field='pregame_plan.lineups.players / basis / context.side.historical player observation'
        elif kind=='LOCKED_CONTENT_PRESERVATION_ERROR':
            primary='F. REPAIR_PIPELINE';claim='修复schema通过后，保留校验按原core路径要求字段存在；原nested uncertainties被schema禁止，导致同义结构迁移也被当作丢失。'
            secondary=['C. PROMPT_SCHEMA_CONTRACT: 原始uncertainties位置错误仍是模型输出与schema不符；该错误与恢复不可接受是两个阶段。']
            detail={'initial_field':'analysis_core.uncertainties','raw_schema_status':'FAILED','repair_schema_status':'PASSED','acceptance_error':final['failure_code']}
            alternatives='不是provider/JSON失败；不是修复预测数字被改（实际error为dropped_core_field）；不把未执行的evidence gate写成失败。'
            code='v2/pregame_system/forecasting_workflow.py:286-358';field='analysis_core.uncertainties -> root.uncertainties'
        elif kind=='PARSE_ERROR':
            primary='G. PROVIDER_FORMATTING';claim='返回model-content文本在EOF位置不能组成完整JSON；Baseline只用Prompt约束格式，未启用response_format。这里归入输出格式链层，不能证明HTTP传输截断或篮球推理错误。'
            secondary=['C. PROMPT_SCHEMA_CONTRACT: 当前正式请求未启用provider JSON output enforcement。']
            raw=json.loads(a['raw_response'])['choices'][0]['message']['content']
            try:json.loads(raw)
            except json.JSONDecodeError as exc:detail={'error':exc.msg,'line':exc.lineno,'column':exc.colno,'pos':exc.pos,'length':len(raw),'at_eof':exc.pos==len(raw),'finish_reason':a.get('finish_reason'),'response_format_present':'response_format' in packet}
            alternatives='finish_reason=stop，不能直接归为max_tokens截断。未看到HTTP错误或重试。具体是provider还是模型序列化环节的起因仍不能从单次日志完全区分。'
            confidence='medium';code='evaluation/execution/run_baseline_v1.py:128-141';field='choices[0].message.content / JSON decoder EOF'
        else:
            primary='C. PROMPT_SCHEMA_CONTRACT'
            if any('matchup_status_conflict' in e['msg'] for e in errs):
                core=out['analysis_core'];detail={'matchup_status':core['matchup_status'],'key_matchups_n':len(core['key_matchups']),
                    'classification_values':[x['classification'] for x in core['key_matchups']]}
                claim='发送给模型的JSONSchema给出status枚举与独立列表，却未表达跨字段关系；Skill把证据不足与no_clear_matchup关联，模型同时给出有条件inference对位，runtime则把这组状态拒绝。'
                secondary=['B. MODEL_GENERATION: 输出确实包含no_clear_matchup和非空列表。','F. REPAIR_PIPELINE: 单次schema-only修复未改变该结构状态；恢复内容锁不允许任意改既有status/list内容。']
                alternatives='不是没有任何分析材料，也不代表不能作推断；关键是输出status与analytical list的契约。未做反事实实验，不能因8/8共现断言Prompt单独造成全部错误。'
                code='v2/pregame_system/forecasting_workflow.py:89-105';field='analysis_core.matchup_status / key_matchups';confidence='medium'
            else:
                ls=out['pregame_plan']['lineups'];detail={'lineups':[{'side':x['side'],'basis':x['basis'],'players_n':len(x.get('players',[])),
                    'distinct_n':len(set(x.get('players',[]))),'evidence_ids_present':'evidence_ids' in x} for x in ls]}
                claim='双方实际都列5名不同球员，缺少evidence_ids；请求JSONSchema允许该字段省略/default=[]，runtime complete却要求agent_expected的非空引用。Schema可满足与production可接受不是同一条件。'
                secondary=['B. MODEL_GENERATION: 本次输出遗漏了可用的引用元数据。','E. HISTORICAL_EVAL_ADAPTER: 历史候选身份与完整首发事实应分开。']
                alternatives='不是球员人数不足；不能说Context完全没有引用可选，目录确有section级IDs。current身份验收尚未执行，不能把它补为已发生的第二个错误。'
                code='v2/pregame_system/pregame_estimates.py:10-33';field='pregame_plan.lineups[].evidence_ids';confidence='high'
        evidence.append({'source_path':code,'exact_relevant_field':field,'schema_errors':errs})
        row={'sample_id':sample['sample_id'],'game':context['matchup']['away_team']+' @ '+context['matchup']['home_team'],
            'first_failure_gate':('JSON_PARSE' if kind=='PARSE_ERROR' else 'CURRENT_IDENTITY_ACCEPTANCE' if kind=='FORECAST_ACCEPTANCE_ERROR' else 'FORECAST_SCHEMA'),
            'observed_failure_type':kind,'context_sufficient':{'historical_team_analysis':True,'current_identity':False,'qualification':'7/7 is structural coverage, not verified current roster or complete archive'},
            'prompt_requirement':{'prompt_hash':baseline.ts.load_profile()['forecast_prompt_hash'],'description':'同冻结Forecast Skill与schema JSON；允许分析，当前名单不明不得补确认为事实'},
            'schema_requirement':{'schema_hash':read_json(baseline.BASE/'system-contract.json')['forecast_validation_schema_hash'],'field':field},
            'validator_requirement':{'source':code,'field':field},'model_behavior':detail,
            'repair_behavior_if_any':{'triggered':final['repair_triggered'],'status':final['repair_status'],'schema_status':final['repair_schema_status'],
                'preservation_status':final['repair_content_preservation_status'],'observed_error':final.get('failure_code')},
            'primary_root_cause':primary,'root_cause_statement':claim,'secondary_contributing_factor':secondary,'why_not_other_layers':alternatives,
            'confidence':confidence,'evidence':evidence,'content_quality':'NA','ground_truth_consulted':False}
        rows.append(row)
    report={'created_at':now(),'system_revision':baseline.REV,'sample_count':20,'source_report_hash':sha(baseline.REPORT_JSON),
        'classification_scope':'Engineering RCA by current agent, not independent human labels or experimentally proved sole causality.',
        'primary_distribution':dict(Counter(r['primary_root_cause'] for r in rows)),'rows':rows}
    exclusive(EVAL/'reports/golden-pregame-baseline-v1-full-rca.json',report)
    csv_path=EVAL/'reports/golden-pregame-baseline-v1-full-rca.csv'
    with csv_path.open('x',encoding='utf-8-sig',newline='') as f:
        keys=['sample_id','game','first_failure_gate','observed_failure_type','primary_root_cause','confidence','root_cause_statement','why_not_other_layers']
        w=csv.DictWriter(f,keys);w.writeheader();w.writerows({k:r[k] for k in keys} for r in rows)
    lines=['# Golden Pregame Baseline V1 · Full 20-case RCA','',report['classification_scope'],'',
        'failure symptom != root cause。高置信度表示合同/日志事实明确，不表示已用独立干预证明Prompt是唯一因果因素。未读取GT或判篮球预测对错。','',
        '| sample | game | first gate | primary layer | confidence |','|---|---|---|---|---|']
    for r in rows:lines.append(f'| {r["sample_id"]} | {r["game"]} | {r["first_failure_gate"]} | {r["primary_root_cause"]} | {r["confidence"]} |')
    for r in rows:
        lines+=['','## '+r['sample_id'],'',r['root_cause_statement'],'','Context: '+json.dumps(r['context_sufficient'],ensure_ascii=False),
            '', 'Observed fields: '+json.dumps(r['model_behavior'],ensure_ascii=False),
            '', 'Secondary: '+'; '.join(r['secondary_contributing_factor']),'','Alternatives / limits: '+r['why_not_other_layers'],
            '', 'Repair: '+json.dumps(r['repair_behavior_if_any'],ensure_ascii=False),'','Sources: '+json.dumps(r['evidence'],ensure_ascii=False)]
    with (EVAL/'reports/golden-pregame-baseline-v1-full-rca.md').open('x',encoding='utf-8') as f:f.write('\n'.join(lines)+'\n')
    print(json.dumps({'samples':20,'distribution':report['primary_distribution'],'llm_calls':0},ensure_ascii=True))
    return report


if __name__=='__main__':analyze()
