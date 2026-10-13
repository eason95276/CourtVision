import json,sys,urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts'))
from asset_scanner import EVAL,read_json,sha
from build_eval_baseline import save,text
HERE=Path(__file__).resolve().parent
m=read_json(HERE/'selection_manifest.json');summary=read_json(HERE/'sampling_summary.json');projects=read_json(HERE/'label_studio_projects.json')
cases=ET.parse(HERE/'tests.xml').getroot().findall('.//testcase')
tests={'passed':sum(c.find('failure') is None and c.find('error') is None and c.find('skipped') is None for c in cases),
    'failed':sum(c.find('failure') is not None or c.find('error') is not None for c in cases),'skipped':sum(c.find('skipped') is not None for c in cases),
    'scope':'18 Golden V1 + 48 prior isolated preparation/closed-book/replay audit cases; no production full suite or formal evaluator'}
rubric=read_json(HERE/'rubric.json');prep=read_json(HERE/'annotation_preparation.json');dry=read_json(HERE/'generation_dry_run.json')
with urllib.request.urlopen('http://127.0.0.1:8625/',timeout=15) as r:site=r.status
report={'selection':summary,'parent_dataset_id':m['parent_dataset_id'],'golden_set_revision':m['golden_set_revision'],
    'selection_manifest_hash':m['selection_manifest_hash'],'sampling_config_hash':m['sampling_config_hash'],'selection_manifest_file_sha256':sha(HERE/'selection_manifest.json'),
    'created_at':m['created_at'],'primary_items':40,'rubric_revision':prep['rubric_revision'],
    'pregame_rubric':{'hard_gates':list(rubric['pregame']['hard_gates']),'soft_metrics':list(rubric['pregame']['soft_metrics']),'error_tags':rubric['pregame']['error_tags']},
    'postgame_rubric':{'hard_gates':list(rubric['postgame']['hard_gates']),'soft_metrics':list(rubric['postgame']['soft_metrics']),'error_tags':rubric['postgame']['error_tags']},
    'guides':['golden_set/v1/PREGAME_ANNOTATION_GUIDE.md','golden_set/v1/POSTGAME_ANNOTATION_GUIDE.md'],
    'projects':projects,'live_task_verification':read_json(HERE/'live_task_verification.json'),
    'generation_dry_run':{'jobs_validated':dry['jobs_validated'],'revision':dry['generation_revision'],'execution_ready':False,'model_calls':0,'outputs_written':0,
        'blockers':['Offline generation model ID/revision/backend not selected; no API substituted for network-disabled execution.','Postgame awaits real same-sample frozen Pregame response/assumptions.']},
    'tests':tests,'protection':read_json(EVAL/'reports/golden-v1-protection-result.json'),'production_http_status':site,
    'browser_validation':{'browser':'existing Microsoft Edge via cached Playwright CLI','pregame_task_page_js_errors':0,'postgame_task_page_js_errors':0,
        'task_page_warnings_each':1,'pregame_has_final_fact_panel':False,'postgame_has_final_fact_panel':True,'checked_score_radios':0,'horizontal_overflow':False,
        'submitted_annotations':0,'note':'Read-only previews only. Login page third-party telemetry requests failed; authenticated task pages rendered normally. Postgame screenshot kept private to avoid showing outcomes to future Pregame annotator.'},
    'not_decided':['LLM-as-a-Judge design','Judge model','Judge prompt','Judge calibration','Quality Pass threshold','Human-Judge Agreement metric','Final regression decision rules'],
    'human_confirmation_needed':['Accept the 20-game V1 selection; changes require V2.','Select an actually offline generation backend/model or explicitly revise the no-network contract for hosted model calls in a future authorized task.','Provide a real second annotator only if doing genuine independent overlap.'],
    'annotations_created':0,'outputs_created':0,'independent_second_annotator_created':False,'agreement_calculated':False}
save(EVAL/'reports/golden-set-v1-preparation.json',report)
rows='\n'.join('| '+str(i+1)+' | '+r['game_date']+' | '+r['away_team']+' @ '+r['home_team']+' |' for i,r in enumerate(m['samples']))
text(EVAL/'reports/golden-set-v1-preparation.md',
    '# Golden Set V1 Preparation\n\n'+
    '**当前是选样与标注环境准备，不是已标注Golden真值。没有文章，尚不能正式评分。作为盲标者不要查看private selection CSV/manifest中的逐场结果分层。**\n\n'+
    '## 20场组成\n\n12 Representative + 8 Challenge + 0 Risk Anchor。原3个锚点名额全部转为Challenge；能精确映射的3个常规赛Bad Case都是数值预测误差极值，本轮不按哪个模型预测错了挑样。40-case审计在selection_manifest。\n\n'+
    '| # | 来源日期 | 对阵 |\n|---|---|---|\n'+rows+'\n\n'+
    '## 分布\n\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n\n'+
    'Phase=双方已观察本季赛前场数的均值+1：<=20 early，21–60 mid，>60 late；不是官方完整场序。分差分层仅用于private抽样：绝对分差<=5 close、6–14 medium、>=15 blowout。20场均7/7结构，但上一赛季partial并未消失；不得称为资料满分。\n\n'+
    '## Rubric\n\n赛前5个Hard Gate：时间泄漏、事实一致、无依据事实、身份混淆、数值/明确逻辑错误（最后一个可NA）。6个1–5软指标：Context利用、具体对位、推理连贯、不确定性处理、结论清楚、信息密度。\n\n'+
    '赛后5个Hard Gate：最终事实一致、编造事件、赛前赛后混淆、适用结算错误（可NA）、无依据事实。6个1–5软指标：假设复盘、差异归因、证据支撑、事后偏见控制、学习价值、清晰与密度。每Gate可附证据/评论，Error Tags多选，Note可空。无Overall Pass/阈值。\n\n'+
    '每个软指标的测/不测/1-3-5锚点/极小例子见两个ANNOTATION_GUIDE。观点不需要逐句硬证据；硬事实必须由给定材料支持。\n\n'+
    '## Label Studio\n\n两个独立项目已导入，各20任务、0标注、0预测；输出null。Pregame只有原Context、等待生成提示和控件，不含Ground Truth/target箱分/隐藏结果分层。Postgame允许真实赛果与箱分，但赛前冻结判断与模型回答尚未生成，未用旧模型补位。\n\n'+
    'Pregame http://127.0.0.1:8681/projects/2/data；Postgame http://127.0.0.1:8681/projects/3/data（先完成赛前盲批次再打开）。项目maximum_annotations=2、历史/协同预测显示关闭。Community管理员仍可访问两个项目，不能宣称强ACL双盲。只有A时不要伪造B补进度。\n\n'+
    '真实浏览器检查：两个任务页可读，Context可折叠完整保留，无横向溢出、无默认评分、0任务页JS错误；每页1条上游警告。只浏览，没有Submit。登录页第三方统计请求失败未改上游架构。Postgame截图不公开，避免提前泄漏赛果。\n\n'+
    '## 版本 / runner\n\n'+m['golden_set_revision']+'\n\nselection hash '+m['selection_manifest_hash']+'\n\n'+
    '完整采样重跑复现同selection/hash，不替换比赛。40条dry-run job及JSON schema通过，参数、prompt与三个runtime Skill快照固定；Research不加载、无模型API执行入口。具体离线生成model/revision未选，执行不可用；Postgame还需真实Pregame判断。不能把托管API假装断网本地模型。未来输出需完整model/参数/输入/prompt/Skill/版本hash，排他写入不覆盖。\n\n'+
    '## 测试与保护\n\n'+json.dumps(tests,ensure_ascii=False)+'\n\n'+json.dumps(report['protection'],ensure_ascii=False)+
    f'\n\n生产8625 HTTP {site}；5 Prediction /7 Postgame及已发布artifact/hash不变，父冻结数据全部字节不变。0 LLM调用、0文章、0标注、0第二人、0Agreement；无生产修改，无commit/push。\n\n'+
    '## 未决定\n\nLLM-as-a-Judge、Judge模型/Prompt/Calibration、Quality Pass threshold、Human-Judge Agreement指标、最终Regression判定规则。下一步人工确认20场和真实生成后端；本轮不进入生成或标注。')
print(json.dumps({'revision':report['golden_set_revision'],'tests':tests,'site':site,'primary_items':40,'annotations':0,'outputs':0},ensure_ascii=False))
