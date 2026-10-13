"""Human task preparation. Never fills responses, annotations or predictions."""
import json,sys,html
from pathlib import Path
from xml.sax.saxutils import quoteattr
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from asset_scanner import ROOT,EVAL,read_json,connect,canonical_hash,sha
from build_eval_baseline import save,text

HERE=Path(__file__).resolve().parent

def config_xml(stage,rubric):
    tags=['<View style="max-width:1080px;margin:auto;line-height:1.6;overflow-wrap:anywhere;">',
        '<Header value="固定资料 · 独立人工阅读"/>','<Text name="stage_notice" value="$stage_notice"/>',
        '<Header value="Context"/>','<Text name="context_summary" value="$context_summary"/>',
        '<Collapse><Panel value="展开完整Context（与模型可见文件完全一致）"><Text name="context" value="$context_json"/></Panel></Collapse>']
    if stage=='postgame':tags+=['<Header value="冻结的赛前判断 / 关键假设"/>','<Text name="pregame_basis" value="$pregame_basis_display"/>',
        '<Header value="最终赛果"/>','<Text name="ground_truth" value="$ground_truth_display"/>',
        '<Collapse><Panel value="展开赛后箱分与允许证据"><Text name="postgame_evidence" value="$postgame_evidence_json"/></Panel></Collapse>']
    tags+=['<Header value="Model Output"/>','<Text name="model_output" value="$model_output_display"/>','<Header value="Hard Gates（事实边界，不计算Overall Pass）"/>']
    for name,rule in rubric['hard_gates'].items():
        tags+=['<Header size="5" value='+quoteattr(rule['label']+' · '+name)+'/>',
            '<Choices name='+quoteattr(name)+' toName="model_output" choice="single-radio" showInLine="true" required="true"><Choice value="PASS"/><Choice value="FAIL"/>'+('<Choice value="NA"/>' if 'na' in rule else '')+'</Choices>',
            '<TextArea name='+quoteattr(name+'_evidence')+' toName="model_output" rows="2" placeholder="若FAIL请粘贴片段或简述原因；其余可留空"/>']
    tags+=['<Header value="Soft Scores（1弱 / 3中 / 5强；2与4是中间档，不是Pass阈值）"/>']
    for name,rule in rubric['soft_metrics'].items():
        tags+=['<Header size="5" value='+quoteattr(rule['label']+' · '+name)+'/>',
            '<Header size="6" value='+quoteattr('1 '+rule['1']+' ｜ 3 '+rule['3']+' ｜ 5 '+rule['5'])+'/>',
            '<Choices name='+quoteattr(name)+' toName="model_output" choice="single-radio" showInLine="true" required="true">'+''.join('<Choice value="'+str(n)+'"/>' for n in range(1,6))+'</Choices>']
    tags+=['<Header value="Error Tags（可多选，没问题留空）"/>','<Choices name="error_tags" toName="model_output" choice="multiple">'+''.join('<Choice value='+quoteattr(v)+'/>' for v in rubric['error_tags'])+'</Choices>',
        '<Header value="Optional Note（仅重要问题时填写）"/>','<TextArea name="annotator_note" toName="model_output" rows="3" placeholder="重要补充；不要求长评"/>','</View>']
    return '\n'.join(tags)

def context_summary(c):
    lines=[c['matchup']['away_team']+' @ '+c['matchup']['home_team']+' · '+c['matchup']['game_date'],
        '下面是历史事件重建，不是当前确认阵容。盘口、伤病、首发与目标availability均未提供。']
    for side in ('team_a','team_b'):
        t=c[side];r=t['record'];recent=t['recent_form']['recent_5'];prev=t['previous_season']
        lines.append(t['team_name']+'：赛前已观察战绩 '+str(r.get('wins_before_game'))+'胜/'+str(r.get('losses_before_game'))+'负；近5窗口实际 '+str(recent['sample_games'])+' 场；上一赛季 '+str(prev.get('sample_games'))+' 场（'+prev['status']+'）。')
    lines.append('完整原始Context在下方折叠区；摘要不是另一套模型输入。');return '\n'.join(lines)

def boxscore(sample):
    gameid=sample['target_game_id'];bare=gameid.removeprefix('nba:')
    with connect(ROOT/'v2/data/player_knowledge.sqlite') as c:
        rows=[dict(r) for r in c.execute('SELECT player_id,player_name,team_id,participation_status,starter,starter_source,minutes_seconds,points,assists,rebounds,turnovers,steals,blocks,missing_status FROM player_game_facts WHERE game_id IN (?,?)',(gameid,bare))]
    return {'source':'player_knowledge.sqlite target-game facts (postgame only)','players':rows,
        'limitations':['No play-by-play or interview supplied. Boxscore does not prove exact game events or causes.','Null minutes/status remain unknown; no target availability backfill.']}

def guides(stage,rubric):
    title='PREGAME' if stage=='pregame' else 'POSTGAME'
    out=['# '+title+' Annotation Guide','',
        '无真实模型输出时不评分、不提交。当前全部awaiting_generation；空内容不是1分。先读资料，再读输出，再逐项判断。',
        '评分对象是这篇输出，不是模型历史命中率、球队喜好或文风是否合个人口味。2/4是中间档；不计算Overall Pass。',
        '观点、阵容适配和带限定的推断不需要逐句证据；可核验硬事实必须来自所给材料。',
        '赛前标注只看Pregame项目，不打开Postgame项目或selection_manifest。赛后要先有真实冻结赛前输出再评价假设复盘。','', '## Hard Gates']
    for name,r in rubric['hard_gates'].items():
        out+=['','### '+name+' · '+r['label'],'','PASS：'+r['pass'],'','FAIL：'+r['fail']]
        if 'na' in r:out+=['','NA：'+r['na']]
        out+=['','FAIL尽量给具体片段/解释；没有查出问题不是保证绝对真实。多个Gate可以记录同一问题，但不重复算总分。']
    out+=['','## Soft Metrics']
    for name,r in rubric['soft_metrics'].items():
        out+=['','### '+name+' · '+r['label'],'','测：'+r['measures'],'','不测：'+r['not'],'','1：'+r['1'],'','3：'+r['3'],'','5：'+r['5'],'','极小例子：'+r['example']]
    out+=['','## Tags / Note','','标签用于定位问题，不是自动失败规则。Note可空。没有真实第二人不填写annotator_b，不把自我复标称为独立双标。',
        'Pregame预测碰巧正确不等于泄漏；Postgame只根据箱分不能断言某节/某回合如何发生。']
    text(HERE/(title+'_ANNOTATION_GUIDE.md'),'\n'.join(out))

def main():
    m=read_json(HERE/'selection_manifest.json');rubric=read_json(HERE/'rubric.json');rev='human-rubric-v1-'+canonical_hash(rubric)[:20]
    imports={'pregame':[],'postgame':[]}
    for sample in m['samples']:
        c=read_json(EVAL/sample['context_path']);ctxjson=json.dumps(c,ensure_ascii=False,indent=2);gt=read_json(EVAL/sample['ground_truth_path'])
        metadata={'sample_id':sample['sample_id'],'dataset_revision':m['parent_dataset_id'],'golden_set_revision':m['golden_set_revision'],'rubric_revision':rev}
        data={'sample_id':sample['sample_id'],'context':c,'context_json':ctxjson,'context_summary':context_summary(c),
            'model_output':None,'model_output_status':'awaiting_generation','model_output_display':'尚未生成模型回答。当前仅浏览资料，请勿填写或提交评分。',
            'stage_notice':'等待真实输出；所有控件无默认评分。不要在没有文章时评分。'}
        imports['pregame'].append({'data':{**data,'stage':'pregame'},'meta':{**metadata,'stage':'pregame'}})
        post={**data,'stage':'postgame','frozen_pregame_prediction':None,'important_assumptions':None,
            'pregame_basis_display':'等待同一样本的真实Pregame输出与冻结判断；没有替代旧模型数值。',
            'ground_truth':gt,'ground_truth_display':'主队 '+str(gt['home_points'])+' / 客队 '+str(gt['away_points'])+'；胜方 '+gt['winner_side']+'；主队减客队分差 '+str(gt['actual_margin'])+'；总分 '+str(gt['actual_total']),
            'postgame_evidence':boxscore(sample)}
        post['postgame_evidence_json']=json.dumps(post['postgame_evidence'],ensure_ascii=False,indent=2)
        imports['postgame'].append({'data':post,'meta':{**metadata,'stage':'postgame'}})
    for stage in ('pregame','postgame'):
        save(HERE/(stage+'_import.json'),imports[stage]);text(HERE/(stage+'_config.xml'),config_xml(stage,rubric[stage]));guides(stage,rubric[stage])
    save(HERE/'annotation_preparation.json',{'rubric_revision':rev,'tasks':{'pregame':20,'postgame':20},'annotations_created':0,'outputs_created':0,
        'source_note':'Outcome strata / challenge reasons / anchors are absent from Pregame data and metadata. Annotation export joins private strata afterward.'})
    print('Prepared 20 + 20 tasks; 0 outputs, 0 annotations')

if __name__=='__main__':main()
