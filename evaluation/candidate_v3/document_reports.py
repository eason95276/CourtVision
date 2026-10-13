"""Readable final handoff/evidence files derived only from existing receipts."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from evaluation.candidate_v3 import analysis_tools as a,final_workspace as h
from asset_scanner import EVAL,read_json,canonical_hash
from control import exclusive,now

def write(name,text):
    path=EVAL/'reports'/name
    if path.exists():
        if path.read_text(encoding='utf-8')==text:return
        raise ValueError('existing report differs; do not overwrite '+name)
    with (EVAL/'reports'/name).open('x',encoding='utf-8') as f:f.write(text)

def run():
    dash=read_json(EVAL/'reports/execution-quality-dashboard.json');meta=read_json(EVAL/'reports/metamorphic-evaluation.json');traces=read_json(EVAL/'reports/phoenix-evaluation-workload.json')
    target=h.target();sdk=read_json(EVAL/'reports/deepeval-deterministic-regression.json');benchmark=read_json(EVAL/'reports/benchmark-v1-readiness.json')
    table='| 指标 | V1 | V2 | V3 | V4 |\n|---|---:|---:|---:|---:|\n'
    for key,label in [('provider_success','Provider'),('parse_success','JSON Parse'),('raw_schema_compliance','Raw Schema'),('repair_trigger','Repair Trigger'),('repair_recovery','Repair Recovery'),('forecast_accepted','Forecast'),('editorial_success','Editorial'),('e2e_success','E2E')]:
        table+='| '+label+' | '+' | '.join(str(x['metrics'][key]['n'])+'/'+str(x['metrics'][key]['d']) for x in dash['revisions'])+' |\n'
    comparisons=json.dumps(dash['comparisons'],ensure_ascii=False,indent=2)
    final=next(x for x in dash['revisions'] if x['revision']==target['target_revision'])
    flat=sum(x['complete'] and x['home_probability_delta']==0 for x in meta['rows'])
    sections=[
        ('1. 从V2 5/20开始发生了什么','保留V1/V2，完成15场RCA；创建独立V3，完整重跑20；发现十进制校验实现bug后创建且仅创建一次V4，再完整20。最终单revision人工目标已冻结。没有V5、质量重生成或挑文章。'),
        ('2. 15个失败真正原因','8场9个字段超长，最多127字符：schema有上限但Prompt没有可操作的简洁中文预算；严格原文锁无法同时修长度。6场球员确在冻结同侧，但V2只补全空引用，没有补部分覆盖。1场未知alias：schema未枚举合法ID。工程RCA有备选解释，不是独立Human标签。'),
        ('3. V3做了什么','原长度上限未增大；明确中文、字段预算和信息职责；精确已选球员→冻结指针解析，不新增球员、不用current roster或GT；本地合法ID enum；仅lossless空白/字段迁移的canonical恢复。没有假装字符串规则能证明任意同义改写安全。'),
        ('4. V3真实结果','14/20 E2E，13/20 raw schema，20/20 parse；6次repair均失败，40次API。出现合法0.56/0.58被jsonschema浮点multipleOf误拒，已归为VALIDATOR，不叫模型推理失败。'),
        ('5. 是否做V4，为什么','做了，且这是最后一次。多个样本及明确数学合同支持；Decimal精确multipleOf只修校验，不改schema、模型、Prompt、caps或预测数字。V4 14/20 E2E、14/20 raw schema、18/20 parse，4次repair均失败，38次API。未知ID、额外字段/保留冲突和2个parse失败未遮掩。'),
        ('6. 最终Human Target Revision',target['target_revision']+'。V3/V4都14/20；按已公开执行规则，V4没有V3已证明的合法数字误拒，合同更一致。未看文章好坏挑revision。这里critical指已证明的硬合同correctness错误；所有普通E2E回归仍单列，不借定义隐藏。'),
        ('7. 20场最终成功/失败','14 SUCCESS、6 EXECUTION_FAILURE；全部20在manifest里。14篇第一次成功文章待人工评；6项NO_CONTENT、Soft Metrics=NA。没有不同revision拼接。具体失败：\n\n'+json.dumps(final['failures'],ensure_ascii=False,indent=2)),
        ('8. V1→V2→V3→V4完整指标表',table+'\n各revision合同已变化，raw schema按各自版本且先于normalization；E2E只到候选验收及EditorialDraft schema，不到production hard-fact publication。不证明单一Prompt因果提升或预测准确率。'),
        ('9. Repair Recovery变化','0/11→0/8→0/6→0/4，仍未改善。部分结构问题可通过独立确定性canonical恢复，但不能记作LLM repair recovery。自然语言同义压缩安全性未解决；对额外字段/证据修复的保留合同仍过严或不可满足。下一轮需人工决定，当前不放水。'),
        ('10. Regression情况',comparisons+'\nV4相对V3有4个E2E成功倒退，相对V2有3个；V3相对V2有2个。V1无成功不等于没有组件回归。没有只跑旧失败或排除新失败。'),
        ('11. DeepEval正式使用情况','真实SDK evaluate()、BaseMetric、LLMTestCase运行80条记录×8项=640个确定性指标。0 Judge，socket出网在SDK评估期间禁用。初次因Windows控制台GBK汇总错误，UTF-8后成功重执行；不是重跑模型。SDK composite32/80不是Human Quality Pass Rate；指标逐项报。input temporal仅事件日期/target排除，不是严格PIT或文章逐句事实认证。'),
        ('12. 300 Benchmark状态','benchmark-v1-579efb89bd9b601f1b41正式静态冻结。300/300，30队，240 representative+60 challenge，early77/mid147/late76，单队14～28次；normal/anonymous/hash/identity/temporal/物理GT隔离通过。2752共享历史事件，非iid。匿名显式身份0残留不消除统计指纹/参数记忆。没有300 LLM输出。'),
        ('13. Counterfactual真实结果','25组审计，availability/rest各5可作为明确虚拟条件任务，真实跑10对20次Forecast-only；8对完成，2对失败不出分母：8/10方向一致，完成条件下8/8。unexpected flip0；market5组仅做输入排除的静态不变性，非模型实验。venue5控制不明、form5替换多维、不合task的market5不跑。'+str(flat)+'对完成样本概率未变；方向不反向不能证明充分敏感性或条件利用。预设0.02 material tolerance及0/.05敏感性报告，不按结果改关系。非真实比赛准确率/因果效应。'),
        ('14. Promptfoo真实状态','BLOCKED。当前没有稳定可隔离的真实Game QA endpoint；production server只有AI-analysis/market生成入口，旧8683是fixture。0正式红队case、0红队模型调用。没有为写简历临时造echo端点。'),
        ('15. Phoenix真实状态','98模型调用+40非模型验收span，共138条SQLite独立证明持久化。p50 '+str(traces['latency_p50_seconds'])+'s，p95 '+str(traces['latency_p95_seconds'])+'s；按阶段token/status/latency/hash有记录，evaluation workload非production monitoring。最终metamorphic acceptance在result journal，非声称全链live DAG。'),
        ('16. Bad Case / Regression Set','87个事件组：40历史inventory+47个真实失败revision/sample chain，raw/repair症状不重复算case，不是87独立根因；39 legacy仍OTHER_UNATTRIBUTED不强行归成LLM reasoning。固定regression47案例，原output只读replay，27可被目标历史adapter接受；这不是27新文章，也不是生产fix率。历史inventory旧误差/outcome只用于独立bookkeeping，未进入Pregame优化请求。'),
        ('17. Label Studio人工标注现在怎么开始','http://127.0.0.1:8684/projects/1/data 。一个project、一个当前task，先Lakers @ Timberwolves，最终V4全文已直接显示，Context可折叠，Rubric定义/选项/必填未改变。使用原本地账号。人工Submit后运行：\n\n```powershell\n& evaluation/deepeval/.venv/Scripts/python.exe evaluation/candidate_v3/final_workspace.py freeze\n& evaluation/deepeval/.venv/Scripts/python.exe evaluation/candidate_v3/final_workspace.py open\n```\n\n当前冻结后才能下一场。未来任务不在该DB；失败项仅确认execution_failure。Postgame关闭；不展示Forecast中间结果或GT。管理员可看本机文件/主动导入，不是强ACL安全沙箱。登录页vendor tracking资源曾有错误；标注页0error、1MobX warning。'),
        ('18. 当前可写简历数字','1231 normal+1231匿名Context构建；20场四revision paired执行；V1 E2E0/20→V4 14/20，明确“改变执行合同”；80记录640 DeepEval非Judge指标；300静态冻结；10 synthetic pairs真实20请求；138 evaluation spans；87事件组/47回归案例。限定口径详见monday-resume-numbers.md。'),
        ('19. 仍不能写的数字','Human Quality Pass Rate/Mean Soft Score/Agreement/Kappa/Human-Judge Agreement、真实Forward预测uplift、严格PIT回测、300模型评测、正式Promptfoo红队、production Phoenix监控、repair恢复有效或所有问题解决。'),
        ('20. 面试最值得讲的3个故事','①0/20不是API挂：分provider/parse/schema/acceptance，历史候选与live current identity合同不可满足。②同20 paired：不排失败、不混revision，发现V3浮点bug与V4新regression，修复受上限约束。③可信度边界：SDK/trace不等于Judge/内容质量；10对synthetic有2失败和4概率flat，报告分母和限定范围。'),
        ('21. 测试结果','最终suite数字与syntax/diff/保护证据在mega-final-verification.json和mega-final-safe-tests.xml。早期320 passed/1 skipped，新增7资产冻结案例单独7 passed；不相加冒充最终重复计数。Windows symlink权限缺失跳过，production全仓未跑。工作台目录首次缺失、官方文档解析依赖、GBK控制台、旧浏览器session失效均做最小修复，未触发模型质量重跑。'),
        ('22. API调用量/token','本轮98：主Forecast40、Repair10、Editorial28、synthetic Forecast20。V3 40、V4 38、metamorphic20。'+json.dumps(traces['token_usage'])+'。不估计美元/人民币金额。与V1/V2先前64累计162，不能把历史64混作本轮消耗。'),
        ('23. 生产保护结果','最终check对照mega-protection-before.json。保护5 Prediction/7 Postgame的version hash、published references、source/protocol/schema/Skills/静态文件、V1/V2历史产物和母集/Golden。8625未部署候选、未发布新版本；不commit/push。正式事实DB内容可能被自身同步服务更新，不以整个DB文件hash声称冻结。'),
        ('24. 下一步人工最该做什么','先真实读第一篇、提交Rubric并冻结，按时间推进。先确认14篇的硬事实/对位/边界质量，记录人工Bad Case；不要因为执行70%就写质量70%。6个失败和repair0仍需后续方案，当前按授权停在V4。')]
    text='# CourtVision Mega Evaluation Handoff\n\n截至2026-10-12北京时间。授权执行已结束；无新模型调用计划。\n\n'
    for heading,body in sections:text+='## '+heading+'\n\n'+body+'\n\n'
    capability={'Evaluation Data':'DONE bounded construction/static checks','Execution Evaluation':'PARTIAL complete experiments,14/20 target, repair0','Human Evaluation':'READY14 articles,20 status tasks,0 labels',
        'LLM-as-a-Judge':'NOT_STARTED skeleton only, no model/prompt/threshold decision','Regression':'DONE47 archived cases, not complete semantic coverage',
        'Red Team':'PARTIAL legacy fixture, real Promptfoo BLOCKED','Observability':'DONE evaluation workload only','Forward Validation':'NOT_STARTED formal regular-season cohort'}
    text+='## CURRENT PROJECT STATUS\n\n| 能力 | 当前状态 |\n|---|---|\n'+''.join('| '+k+' | '+v+' |\n' for k,v in capability.items())+'\n没有整体“完成百分比”，没有人工质量claim。'
    write('mega-evaluation-handoff.md',text)
    if not (EVAL/'reports/evaluation-capability-map.json').exists():exclusive(EVAL/'reports/evaluation-capability-map.json',{'created_at':now(),'capabilities':capability,'human_scores':None,'judge_scores':None})
    write('evaluation-capability-map-v4.md','# Current Evaluation capability map\n\n'+json.dumps(capability,ensure_ascii=False,indent=2)+'\n\nStates are scoped, not project completion percentages.')
    write('monday-resume-numbers.md','# Monday resume numbers · 2026-10-12\n\n## A. 强烈建议写\n\n- 同20场、4个revision的真实执行回归；0/20→14/20执行成功，合同改动与回归保留，不称预测能力提升。\n- 80条真实记录×8项，640次DeepEval自定义确定性指标，0LLM Judge。\n- 138条真实Evaluation span（98模型+40验收），独立SQLite持久化核对，p50/p95与token分阶段统计。\n\n## B. 可以写但限定口径\n\n- 构建并冻结1231 normal/1231 anonymous historical Context，不是严格PIT/去记忆污染证明。\n- 300 benchmark selection/static validation，30队覆盖，不是300模型评测。\n- 10对真实synthetic实验，8/10完成方向一致（8/8 conditional）；2对失败，4组概率flat，不证明真实因果或准确率。\n- 87事件组、47结构回归案例，27 archived outputs适配接受；不是87根因/27模型重生成。\n- 14篇人工待评，工作台20-status/1visible/0labels；不是专家Gold标签已完成。\n\n## C. 暂时不能写\n\nHuman pass/Mean Soft Score/Agreement/Kappa/Judge calibration/Forward accuracy uplift/300LLM评测/Promptfoo真实红队/Phoenix生产监控/repair recovery improved（仍0）。\n')
    write('final-evaluation-technical-report.md','# Evaluation technical report\n\n'+table+'\n'+dash['limits']+'\n\nTarget '+target['target_revision']+'; one frozen revision,14 content/6 no-content; request/model/config/Skill/prompt/schema/response hashes and source code pinned. 300 static freeze and25 synthetic candidates are different populations;10 pairs actually executed.\n\nDeterministic SDK metric scopes: '+json.dumps(sdk['counts'],indent=2)+'\n\nNo content scoring, no production acceptance certification, no strict PIT, no probabilistic inference from20 reused samples. Benchmark cross-sample relation inventory explicitly retained. Regression table and event sources underreports/regression/bad_cases.\n\nReproduction: existing revisions cannot reexecute; pure readers/test suite and human workspace CLI are safe. New generation requires a new revision and new authorization; noV5 in current task. JSON mode supported but raw parse failed2/20 inV4; exact decimal validator removes legal tick false-rejections without rounding decisions. General paraphrase repair remains unsupported; current guard protects exact lexical content.\n')
    return {'target':target['target_revision'],'sections':24,'capabilities':capability}
if __name__=='__main__':print(json.dumps(run(),ensure_ascii=True))
