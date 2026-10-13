"""Evidence matrices, not a resume or fabricated personal experience."""
import json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,read_json
from build_eval_baseline import text,save

tools=[
 ('Label Studio','IMPLEMENTED / SMOKE TESTED / formal human eval NOT YET USED','golden_set/v1/label_studio_projects.json; reports/workbench-blindness-audit.json','2阶段项目、40个空输出任务与人工Rubric工作台；0标注','完成人工评测、双盲/双标一致性已达标'),
 ('DeepEval','IMPLEMENTED adapter / SMOKE TESTED / formal eval NOT YET USED','reports/deepeval-smoke.json; scripts/future_eval_adapters.py','安装并接通3个纯Python读取smoke和空输出adapter；未做G-Eval','已用DeepEval证明LLM质量提升/完成LLM Judge评测'),
 ('Promptfoo','SMOKE TESTED / formal QA/red team NOT YET USED','reports/promptfoo-smoke.json; promptfoo/promptfooconfig.yaml','2个本地echo fixture请求通过，不是真实Game QA质量评测','已完成100条Agent红队或线上QA Benchmark'),
 ('Phoenix','SMOKE TESTED / production observability NOT YET USED','reports/phoenix-smoke.json; scripts/phoenix_smoke.py','本地接收并按trace ID核实1条isolated OTLP trace','生产Research→Forecast→Editorial全链追踪及性能优化已验证')]
text(EVAL/'reports/tool-capability-matrix.md','# Tool capability reality\n\n| Tool | State | Evidence | Safe wording | Overclaim |\n|---|---|---|---|---|\n'+'\n'.join('| '+' | '.join(r)+' |' for r in tools)+'\n\n安装/历史smoke回执不等于现在服务持续运行；本轮未重跑模型evaluator。Label Studio真实任务读取在本轮核验，其他工具以保存回执为准。')
claims=[
 ('1231场Closed-book数据','VERIFIED construction / PARTIAL safety','closed-book-freeze-manifest.json; dataset-adversarial-audit.json','1231普通+1231匿名，6162文件，955009来源事件核对','构建并冻结1231场历史输入候选、独立隐藏赛果和来源核验','1231场严格PIT/真实未来预测Benchmark'),
 ('匿名Benchmark','PARTIAL','identity-attacks.json; dataset-adversarial-audit.json','explicit leak0；18攻击修复；比分/H2H指纹1231唯一','实现去身份化视图并审计显式身份与统计指纹风险','彻底消除记忆污染/不可反识别'),
 ('Golden Set','READY selection / NOT SUPPORTED labels','golden_set/v1/selection_manifest.json','20场，12+8+0，30队，40任务，0标签','准备20场分层Golden候选及人工标注协议','已有20场专家Gold真值'),
 ('Temporal leakage专项','VERIFIED bounded','temporal-attacks-before.json; temporal-attacks-after.json','45fixture：20→45拒绝；真实来源事件955009核对','建立时间/身份/来源绑定对抗回归，明确数据日期边界','证明任何隐藏未来信息都不可能进入'),
 ('Version freeze','VERIFIED ordinary tamper / PARTIAL trust root','scripts/evaluation_integrity.py; test_adversarial.py','数据/增删/顺序/metadata/manifest变更fixture拒绝','实现锚定hash、文件清单及不可静默覆盖','密码学签名审计/抵御拥有全部本机写权限的攻击者'),
 ('Bad Case','VERIFIED inventory / PARTIAL attribution','secondary-adversarial-audit.json','40事件，3重复观察组，4数值误差，0已验证推理质量归因','归档可追溯失败事件并区分数据、管线、生成和展示问题','40个独立LLM推理失败已复现修复'),
 ('Regression','VERIFIED scoped','test-mutation-audit.json; adversarial/all-tests.xml','8代码mutants被杀；旧target-ID guard删除测试仍绿已补','执行隔离回归与mutation-style测试，发现并补测试盲点','全仓生产/全部语义错误验证完毕'),
 ('Label Studio','VERIFIED setup / NOT SUPPORTED actual labels','workbench-blindness-audit.json','20+20任务，0真实评分','配置赛前/赛后独立Rubric项目和双标导出结构','完成Human Evaluation或Kappa校准'),
 ('DeepEval','PARTIAL','deepeval-smoke.json','3个adapter smoke，无模型Judge','接通评测SDK输入和最小本地smoke','完成G-Eval/正式评分'),
 ('Promptfoo','PARTIAL','promptfoo-smoke.json','2echo fixture','配置HTTP smoke与未来回归adapter','真实Agent红队完成'),
 ('Phoenix','PARTIAL','phoenix-smoke.json','1本地trace','验证本地OTLP接通并设计span映射','生产链observability及降延迟收益'),
 ('LLM-as-a-Judge','PLANNED','golden-set-v1-preparation.json','0Judge调用/无方案','后续计划；不能写成已完成能力','Judge校准及显著提升'),
 ('Human Evaluation','READY / NOT SUPPORTED execution','label_studio_projects.json','0标注；没有B','人工评测工作台与量表准备','双人Agreement或真实标注已完成'),
 ('Forward Validation','PLANNED eval / PARTIAL production receipts','current-eval-baseline.json; prediction_versions.sqlite','来源未知cohort，未认证严格forward评测','区分历史质量评测与未来预测验证','用历史命中率证明forward uplift')]
save(EVAL/'reports/resume-evidence-matrix.json',claims)
text(EVAL/'reports/resume-evidence-matrix.md','# Resume evidence matrix — not a final resume\n\n| Claim | Status | Evidence | Numbers | Safe wording | Overclaim |\n|---|---|---|---|---|---|\n'+'\n'.join('| '+' | '.join(c)+' |' for c in claims)+'\n\nVERIFIED是有界证据，不代表生产全链质量或因果收益。匿名统计指纹、强ACL与真实模型/人工执行仍缺。')
cap=[('DATA','DONE / PARTIAL','冻结输入、源引用核对已完成；原档案缺失和日期冲突仍有边界'),('BENCHMARK','READY','候选/20场选样有版本；尚无真实输出和Gold标签'),('HUMAN EVAL','READY / PARTIAL','Rubric与项目有；0标签、强访问盲性未建立'),('AUTO EVAL','PARTIAL','adapter/smoke；无真实质量评分'),('JUDGE','NOT STARTED','不设计/不调用'),('REGRESSION','DONE scoped','有界对抗/变异回归；非全仓生产认证'),('RED TEAM','DONE infrastructure / PARTIAL agent','45时间类+18身份类fixture；真实Agent动态红队未做'),('OBSERVABILITY','PARTIAL','Phoenix1本地trace；生产插桩未做'),('FORWARD VALIDATION','NOT STARTED evaluation','未来常规赛评测未认证；季前赛shadow不能替代')]
text(EVAL/'reports/evaluation-capability-map.md','# Evaluation capability map\n\n| Layer | State | Actual boundary |\n|---|---|---|\n'+'\n'.join('| '+' | '.join(r)+' |' for r in cap)+'\n\n下一步优先访问隔离/无共享历史的真实运行器，然后真实输出和人工标注；不跳到Judge。')
questions=[
 ('为什么Historical不是Forward？','输入是事后重建，模型可能记住结果；本项目Strict审计A/B均0。','historical-replay-feasibility.json','历史胜率等于未来预测能力'),
 ('为什么改Closed-book？','固定过去事件供质量/推理回归，不证明当时真的采集。','closed-book-design.md','closed-book等于严格盲回测'),
 ('匿名化解决什么？','去直接名字/ID/日期；不消除精确统计指纹。','dataset-adversarial-audit.json','零记忆污染'),
 ('怎么防目标行？','严格日期<target与目标ID排除，两条独立条件；新增reschedule-ID测试。','test_adversarial.py','只shift一次就够'),
 ('同日比赛怎么处理？','全部排除，不猜先后；日期精度明确。','closed_book_data.py','精确到真实开球前5分钟'),
 ('来源时间真实吗？','受控事件时间，不是原始采集PIT；独立读CSV/SQL验证声明。','dataset-adversarial-audit.json','采集证明齐全'),
 ('延期比赛发现了什么？','2场过期Jan25的teamId0占位与真实March行共存；当前无提前引用，库存资格扫描曾因此误排2场。','temporal-source-conflicts.json','发现2场就说所有样本已泄漏或擅改冻结母集'),
 ('Ground Truth分目录就安全吗？','只保证逻辑分离，不是OS权限；本机管理员可以读，需要受限dispatcher。','safe_model_input.py','物理分目录就是安全沙箱'),
 ('模型request如何不带metadata？','只序列化已验证context，忽略wrapper指令/评测metadata。','safe_model_input.py','任意SDK对象都可直接喂模型'),
 ('为什么Golden只有20？','小规模人工准备集，20games40items；不是统计显著性保证。','selection_manifest.json','20场证明通用质量'),
 ('为何不是随机20？','固定seed+阶段/队覆盖/难点/隐藏分差分层。','sampling_config.json','代表自然场景比例的无偏估计'),
 ('为什么没Risk Anchor？','映射常规赛的是预测误差极值；质量案件多为其他比赛/无稳定身份。','selection_manifest.json','拿误差最大的3场冒充推理风险'),
 ('7/7是什么？','七组可读资料，不保证满N或来源完整。','closed-book-benchmark-preparation.json','数据质量满分'),
 ('为什么不放市场伤病？','原时间/来源资格不完整；保持unavailable。','closed_book_contexts','没有就猜一份'),
 ('如何复现抽样？','排序ID与固定PRNG；本轮3次官方replay，5个seed敏感性，不改V1。','sampling-adversarial-audit.json','只跑一次称可复现'),
 ('Outcome stratification会泄漏吗？','可用于private选样，但不入Pregame任务；会改变样本分布，不能估总体命中率。','pregame_import.json','抽样用GT就是绝对无偏'),
 ('跨样本能污染吗？','后一个context合法历史包含前一个目标结果；Golden有一对方向依赖。','dataset-adversarial-audit.json','每个样本单独合法就可共享对话'),
 ('如何发现精确重复？','内容hash、匿名归一化、日期球队身份分开；同比分不等于同比赛。','dataset-adversarial-audit.json','同一比分直接合并'),
 ('near duplicate如何定义？','18数值维度候选距离，不自动合并或设质量阈值。','near-duplicate-audit.json','低距离就是重复造假'),
 ('hash能防什么？','锚定manifest、列举文件、字节变化；不是签名，不防所有本机文件同改。','evaluation_integrity.py','防任意root攻击者'),
 ('旧测试盲点是什么？','删target-ID排除旧测试仍绿；新增错日期目标ID用例杀死。','test-mutation-audit.json','passed数量证明全面'),
 ('做了多少攻击？','45时间/结构/绑定fixture旧拒20，修后45；18身份fixture旧拒2，修后18。','temporal-attacks-after.json','真实Agent线上攻击都通过'),
 ('坏fixture与mutation有什么不同？','前者改输入；后者临时改规则再运行真实测试，代码副本不入生产。','mutation_audit.py','把setup error算作杀死mutant'),
 ('45/45是否充分？','仅这些攻击被拒；数值语义需要可信hash/原始源，不能独立证明所有事实。','attacks.py','所有未知泄漏均已解决'),
 ('Bad Case是LLM失败吗？','40是事件/修复，有重复、管线、展示、数值误差；推理因果未认证。','secondary-adversarial-audit.json','40个独立模型推理错误'),
 ('Label Studio是否用了？','2项目40任务真实导入、0标签；配置与执行分开。','workbench-blindness-audit.json','已完成专家标注'),
 ('Human blind是否强隔离？','同admin可访问Postgame API并按sample_id关联；Community配置不够ACL。','workbench-blindness-audit.json','双盲已保证'),
 ('有第二标注人吗？','没有。schema支持未来B；不造Kappa，不把自我重标当独立双标。','DOUBLE_ANNOTATION_WORKFLOW.md','双人一致性已验证'),
 ('DeepEval实际做什么？','3本地读取metric smoke+SDK adapter，没G-Eval或真实Judge。','deepeval-smoke.json','正式评测完成'),
 ('Promptfoo实际做什么？','2个echo HTTP fixture，不是Game QA质量测试。','promptfoo-smoke.json','100条红队完成'),
 ('Phoenix实际做什么？','1条isolated OTLP trace并数据库核验，无生产全链插桩。','phoenix-smoke.json','性能/成本优化已证实'),
 ('未来offline模型怎么选？','当前model_id/revision为空，网络禁用契约不能默默转托管API。','generation_profile.json','已经完成断网DeepSeek推理'),
 ('为何现在不设计Judge？','先有真实输出和人工标签，再定模型/量表校准；当前未启动。','golden-set-v1-preparation.json','没标注却已有Judge Agreement'),
 ('数值baseline可信吗？','可描述，但3个不同GT字段口径冲突须说明，不能同LLM质量混算。','secondary-adversarial-audit.json','全部结果已同源验证'),
 ('下周一哪些能写？','数据构建、版本绑定、对抗测试、工作台准备有实证；质量提升、Judge、人工一致性无。','resume-evidence-matrix.md','把周末计划写成经历')]
out=['# Interview evidence pack — project facts, not invented personal experience']
for i,(q,f,e,danger) in enumerate(questions,1):out+=['',f'## {i}. {q}','',f'Facts / answer outline: {f}',f'Evidence: evaluation/{e}',f'Dangerous overclaim: {danger}']
text(EVAL/'reports/interview-evidence-pack.md','\n'.join(out));print('Evidence docs generated; interview questions',len(questions))
