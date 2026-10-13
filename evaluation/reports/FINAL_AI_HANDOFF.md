# CourtVision「第25秒」最终 AI 交接与求职审计

核验日期：2026-10-13，北京时间。对象：`25th-second` 主工作区的当前代码、冻结输出、SQLite、真实评测与本地人评服务。本文件可独立阅读，不要求接收者有仓库访问能力。

## 0. 给接手顾问的结论

**CourtVision Evaluation 工程 V1 已 FINALIZED，足够作为 AI 应用评测／AI Quality／初级 Evaluation Engineer 的投递项目。现在应停止产品功能施工，转向人工评价、投递和面试。**

这不代表生产产品零缺陷、文章通过专家认证或预测准确率提升。最大的缺口是实际 Human labels 为0，同模型家族 Judge 尚未校准。若候选人不能自己解释下文的分母、数据合同和两个 evaluator bug，仅拥有这些代码也不等于掌握评测能力。

本轮没有调用 LLM、没有新生成预测/文章、没有修改 Golden选择、没有写人工标签、没有 commit/push。三条独立只读审计分别复核产品链、数据/版本指标、Judge/Human/Phoenix；本轮仅修一个小型 closeout 接线问题和文档表述。不是无限续建计划。

## A. 产品是什么：目标、实现和演示分开

CourtVision 面向中文 NBA 用户。比赛开始前，把球队状态、历史与近期表现、球员知识、当前名单、伤病、首发/预计首发、赛程、对位和公开消息作为分析材料，先形成不含盘口的判断，再参考市场形成锁定预测，由 Writer 写成「第25秒」统一声音的文章。winner、赢家概率、主队视角分差、总分是不同字段；胜负方向不等于 ATS 方向。

真实网页默认生产链：

```text
注册比赛/任务
→ current roster preflight（证明球队归属，不证明出场）
→ Generation Input + production Pure baseline +历史/Player Knowledge
→ fresh AutonomousWebResearcher（Research Skill）
→ evidence与AnalystContext
→ Independent Forecast（先剔除盘口/Residual/市场依赖信息）
→ 固定尝试Market Collector、资格校验、Market Review
→ locked FinalPrediction
→ Editorial Skill / Writer /必要事实边界 /publication receipt
→ append-only Prediction Version
→服务端选择current/locked → API →前端同版本读取
```

未取得确认首发、可用状态或分钟限制时应保留 UNKNOWN；Agent 可以作有条件的预计，但不能升级成「官方确认」。当前队员归属不能只靠过去 Player Knowledge；某人未被伤病表列出不等于 Available。历史数据和模型特征也不能自动变成已发生事实。

比赛结束后，产品另行同步终场事实、箱分与参与状态，以最后合法发布的赛前版本为基准生成不可变 deterministic Postgame record，再通过单独 Writer/Skill/artifact生成文章。Writer失败不能回滚基础复盘；不得把赛后DNP、分钟、结果倒灌到赛前输入。

### 当前必须承认的生产差距

产品目标是**约开赛前20分钟启动 FINAL_PREGAME**。但当前生产 `scheduler_policy.py` 和 `official_final.py` 的常量仍为 **5分钟**，并不是已经改好了20分钟。

当前正式库有5个 Prediction、7个 Postgame；Prediction 中4个 EARLY_PREVIEW、1个旧记录无stage。**成功发布的 FINAL_PREGAME 为0。** 已有FINAL任务累计11 cancelled、2 expired、2 missed、1 pending；pending是过去的2026-10-07任务，不证明当前 daemon正常。锁定一个合法 EARLY 版本也不等于生成过FINAL。此轮没有重验daemon/浏览器或改生产调度。

该差距需要写清，但不是必须为求职再开Scheduler施工的理由。不得宣传「线上T-20 FINAL已稳定闭环」。

### 产品、评测、作品集三种证据

| 层 | 当前真实状态 | 可以证明什么 |
|---|---|---|
| 网页生产代码 | 有roster/Research/模型/Market/Writer/版本/Reader/Postgame链 | 工程合同和能力实现；不自动证明稳定上线 |
| Final隔离评测 | 历史冻结Context、独立候选adapter、实际API输出 | 在该评测合同下的执行/内容评分；非生产等价 |
| 湖勇作品集页面 | 有逐版本、hash绑定的人工赛前/赛后sidecar | 展示与人工编辑质量；不是模型自动质量实验 |

仅有湖勇的两份manual sidecar是GAME-SPECIFIC。Reader机制通用，不能因此说所有未来比赛都有相同人工文案。Postgame自动Writer已有热火/猛龙、76人/尼克斯、湖人/勇士三份真实产物，也不能当作Postgame Golden评测通过。

## B. 为什么进入 Evaluation

产品早期出现身份、统计口径、资料不足、schema/repair、文章泛化、用户文案泄漏等问题。仅靠看一篇精修文章无法判断问题来自模型、数据、Prompt、validator还是adapter。因而评测重点转为：固定考试卷、隔离输入、完整保留失败、分层指标、可追溯RCA和回归，而非反复临时挑比赛或凭感觉优化。

核心问题是：「在给定材料与业务合同下，Agent能否可靠完成一次分析；若失败，先在哪一门失败；若生成成功，内容是否具体、有依据？」它不是「篮球预测多准」的同义词。

## C. Evaluation 演进：为什么做、发现什么、得到什么

以下按工程逻辑顺序整理，V1–V4主要执行收据在北京时间10月11–12日，Final及本轮核验在10月13日。较早报告是历史快照，不因文件名包含final就覆盖最新结果。

1. **资产盘点与Replay audit。** 盘点模型、球队/球员数据、盘口、产物、ID和时间字段。严格PIT审查发现「今天恢复旧网页」不证明当时已获取，严格快照不能把普通历史数据冒充live。因此保留strict replay入口，同时建设更实际的closed-book重建模式，不把缺网页历史版本变成整个项目永远停工。
2. **固定Context与匿名变体。** 形成1,231个正常Context及一一对应匿名版本，目标结果/匿名映射/选择私有标签与模型输入分离。资料池含1,228场2025–26常规赛和3场2026–27季前赛样例。匿名降低身份提示，但统计指纹及模型参数记忆仍可能存在，不能称完全去污染。
3. **Benchmark与Golden。** 300条static Benchmark覆盖30队，仅做静态准备/验证，没有跑300场LLM。Golden20从1,228常规赛池以固定seed20261010选12 representative +8 challenge，覆盖30队，赛季阶段6 early/7 mid/7 late。阶段按观察到的赛前场次定义，不是联盟官方场次。原计划最多3风险anchor，但合格anchor不足，转移到challenge；不按某预测模型错得多就选题。
4. **Rubric。** 将严重事实边界和内容分级拆开，固定5 hard gates、6 soft1–5指标；没有擅设overall质量阈值。没有已完成人工Gold labels，Golden在这里先是固定评价目标。
5. **V1真实两阶段执行。** 按正式Forecast/Writer配置做stateless请求，首次成功API/可解析JSON仍可能失败schema。uncertainties嵌套、历史身份不满足live要求、repair保留冲突等使20场最终0/20。生产已有单次schema repair被保存为独立attempt，不偷偷改原回答。
6. **V2历史合同适配。** 显式区分historical_candidate、当前membership、首发与分钟估计；精确同侧ID/资料引用，不把过去出场升级为今天确认。执行提升到5/20；这一变化包含合同修正，不是证明篮球推理变好。
7. **V3紧凑输出/结构恢复。** 合法引用enum、有限字段预算、对位状态、metadata与内容的区分使执行14/20，但也暴露浮点schema对合法概率的误拒。不能把wrapper错算成模型错。
8. **V4 Decimal修正。** 对multipleOf使用十进制精确检查，修正0.56/0.58误拦。聚合仍14/20，样本集合却有4个旧失败恢复、4个旧成功回归；保留paired regression，而非只报总数相同或只挑新成功文章。
9. **历史Final Pregame资料补齐。** 先完成strict T-5三场pilot，后按产品实用需求形成新Historical Final Pregame Golden20：参考约T-20，盘口用合理历史赛前reference，缺quote时点/开闭盘标签不删样本。有限恢复官方伤病与赛前媒体，UNKNOWN保留，不从目标赛后箱分补首发。旧strict pilot不重写。
10. **唯一Final候选。** `golden-pregame-system-final-v1`用新的冻结输入，隔离执行Independent →历史Market Review →Editorial。没有fresh Research工具、聊天历史或Ground Truth。这批Pure为UNAVAILABLE，而且`production_equivalence=False`；不是线上全部模型/采集器的等价运行。
11. **Final执行、回收与独立qualification。** 20场产生15篇，5场生成失败；一次closeout发现accepted对象在校验异常后未清空。适配器分支修正并补回归，但不改原记录。1篇因当前严格guard合同hold，正式合格14/20。该句是「未提供market/Pure」的缺失说明，没有实际盘口泄漏证明；不能把它当篮球模型预测错。
12. **DeepEval、Judge、人评准备。** 历史V1–V4为80记录×8=640；Final另跑20×8=160。Pro Judge发生格式/引文/402问题，保留失败；固定Flash非思考协议在账户恢复后评价14篇，所有请求/回答冻结，未挑分。Label Studio独立，只展示Context/Editorial/Rubric，不展示Judge建议或Forecast中间稿。
13. **Bad Case、Regression、Observability。** 保存87事件组及47归档合同案例，重放包括预期拒绝；Phoenix持久化调用尝试与acceptance。系统修复不是给成功率改分，且不是所有失败都归因给LLM。
14. **Metamorphic与Agent Reach决策。** 做10对真实synthetic条件实验检查方向响应，不称准确率/因果。Exa/RSS三场shadow额外发现176全局独有URL，但严格历史资格和fetch瓶颈没有证明下游价值，未正式接入production。
15. **Finalization与本轮查漏。** 工程V1收口，质量真实性/人工校准明确待完成。本轮重新追溯实际产物，修closeout在开始人评后误报的前提，澄清产品T-20/代码T-5、Pure缺失、Kappa尚未实现和引文非语义认证，不新建评测平台。

## D. FINAL VERIFIED METRICS

| 指标 | 当前核验值 | 正确解释／历史关系 |
|---|---:|---|
| Normal / anonymous |1,231 /1,231 |匿名是同场变体，不是额外1,231独立比赛 |
| 冻结文件hash |6,162/6,162一致 |包含分离资产的字节核验，不读GT来优化输出 |
| Static Benchmark |300、30队 |0模型生成；与Golden重叠6条且共享来源事件，非独立holdout |
| Golden |20、30队 |12代表+8挑战；Human Gold labels0 |
| V1 E2E |0/20 |historical，保留 |
| V2 E2E |5/20 |historical，保留 |
| V3 /V4 E2E |14/20 /14/20 |聚合相同但样本有回归；非同一成功集合 |
| Final原标记成功 |15/20 |15篇真实产出、5生成失败，不回写 |
| Final严格qualification |14/20 |另1篇hold；不是14/20胜负准确率 |
| Final provider /parse /raw schema |20/20、18/20、7/20 |原始schema35%不能藏在恢复后成功率后面 |
| Final修复 |2/5恢复 |唯一一次修复；其余3保留失败 |
| Final generation calls |55 |20 Forecast+5repair+15market+15Editorial |
| 历史DeepEval |80×8=640 |V1–V4，historical，不替代Final分母 |
| Final DeepEval |20×8=160 |128属性通过、32失败；8项全部通过6/20，不等于E2E |
| Final Judge |14/14有效 |同一最终协议，不混Pro／旧版本 |
| Judge hard gates |每项14 PASS、0 FAIL |70个判定；未经Human校准，不称0真实错误 |
| Editorial quote匹配 |154 |128正文、13lead、13其他字段；不是154独立事实认证 |
| Human |14 ready、1 open、0 labels |8686/project1；0自动predictions；逐篇解锁 |
| Bad-case |87事件组 |40legacy+47跨版本失败链；39未归因，不是87根因已解决 |
| Regression |47/47符合预期 |27接受、20拒绝，涉及20独立样本；不是47个修复成功 |
| Historical Phoenix |138 spans |98调用尝试+40非LLM acceptance |
| Final Phoenix |85 spans |55生成+15Pro诊断+1Flash402+14恢复；79API返回、6次402 |
| Phoenix总重验 |223 spans |138+85，评测观测，不是生产监控 |
| Metamorphic |10对/20calls；8完成、2未完成 |方向8/10，条件8/8；4完成对概率不变；不是准确率或因果 |
| Market静态不变量 |5 |不是另5对模型实验 |
| Agent Reach shadow |3场、176全局新URL |0合格hard facts、0认证matchup信息；旧strict策略下结果 |
| Final发布测试 |68通过 |Final release历史收据，0失败/skip |
| 本轮最新测试 |73通过 |增加5个人评生命周期fixture回归，0失败/skip；不把重复运行累加 |
| 正式产品版本 |5 Prediction、7 Postgame |不可变payload/hash保持，FINAL_PREGAME成功发布0 |
| 本轮LLM/生成/标注写入 |0/0/0 |只读审计、低风险小修和交接 |

### Final Judge的六项均分

材料利用4.6429；对位具体性4.7143；推理连贯4.2143；不确定性处理4.0000；结论清晰4.9286；信息密度4.0000。每项分母14、尺度1–5，没有overall加权分。3篇有redundant_output tag。

Judge请求model=`deepseek-v4-flash`，返回model=`deepseek-flash`，temperature0.1、max_tokens3500、thinking disabled。provider没有给可独立锁定的immutable模型revision。所有请求只有system/user两消息；同sample Context压缩视图、锁定预测和文章匹配，0tools/functions。Quote检查验证字面存在；自由文本`context_reference`没有语义认证。

旧Pro15次：10个API回答，其中5 parse失败、5引文失败，另5次402，accepted0；Flash原1次402。恢复后14份才是当前评分cohort。原失败不删除、原无回答请求hash相同，不能拿Pro低分/Flash高分挑一个「最好结果」。

### 资料覆盖不是全面完整

Historical Final20有spread/total各20；official injury19 COMPLETE+1 PARTIAL、238状态行；research19来源覆盖13场；40个球队首发条目中2confirmed、10source_expected、28unknown。Roster仍partial，Pure全unavailable。开闭盘标签不明，保存historical_pregame_market_reference，不冒称closing或精确T-20 quote。当前模型请求没有重新联网；资料收集和模型执行是分开的。

## E. 能力状态

| 能力 | 状态 |
|---|---|
| 固定数据/匿名视图/静态Benchmark/Golden |DONE |
| Rubric与分层execution评测 |DONE |
| Final真实API候选、失败保存、引用/版本/hash合同 |DONE，有限执行成功14/20 |
| Final DeepEval与真实Judge |DONE，均有上述边界 |
| Bad-case/RCA/归档回归/Phoenix |DONE，不声称所有问题解决 |
| Human工作台、导出、basic agreement/within-1接线 |READY |
| Human labels、Human–Judge agreement、专家语义验证 |NOT DONE |
| Cohen Kappa计算 |NOT IMPLEMENTED，不是只等标签就自动算 |
| Human–Human IAA |NOT DONE，单个标注者不算双人一致性 |
| 产品Pregame/Postgame |PARTIAL原型，能力与部分真实产物存在 |
| 生产自动T-20 FINAL |NOT DONE，现有默认T-5且发布FINAL0 |
| Postgame Golden内容评测 |NOT DONE，明确不属V1正式范围 |
| Forward验证/新独立holdout/Promptfoo真实红队 |OPTIONAL FUTURE，当前不能宣称完成 |
| Agent Reach production集成 |NOT DONE且本轮DROP，不因工具多就应接 |

Finalized指工程V1停止条件完成，不表示这些PARTIAL/NOT DONE被自动升级。

## F. SAFE TO CLAIM：推荐四条简历

- 面向多阶段分析Agent，构建冻结数据与评测合同，整理1,231条历史Context、300条静态Benchmark及覆盖30队的20场Golden Set，隔离模型输入、赛果和匿名映射。
- 设计5类硬门和6项内容指标，分层记录provider、parse、schema与验收；完成20场Final隔离链路实验、14篇真实结构化Judge和160项确定性自动检查，保留失败分母。
- 整理87组失败事件，定位历史身份合同、浮点validator和accepted异常分支等问题；重放47个归档合同案例，27接受、20拒绝均符合预期，不把评测器误报归因给模型。
- 搭建独立人工工作台及basic agreement/within-1接线，以尝试级追踪关联调用、token、延迟、hash和失败阶段，持久化85条Final workload spans；人工标注与校准明确待完成。

数字优先级：Golden20、Rubric5+6、Judge14、Final160、Bad-case87/回归47。300static和1,231资料池作为次级规模；spans作为观测证据。不要用最高Judge均分或全部hard PASS作为质量提升主标题。测试数量适合技术问答，不值得挤掉方法和RCA。

## G. DO NOT CLAIM

- 跑了300场LLM、1,231场严格历史live重放或完全去记忆污染。
- 14/20是胜负/ATS准确率、上线可靠性、完整production等价评测或专家质量通过率。
- 只改Prompt就把质量从0提升到70%，或V4→Final明显提高预测能力。
- 154事实已语义证实、Judge高分等于Human Gold、完成agreement/Kappa/双人IAA。
- 87独立根因全部解决、47个bug修好；归档拒绝通过只是正确拒绝。
- 当前系统稳定自动T-20 FINAL、存在成功正式FINAL，或Postgame Golden已完成。
- 223条spans都是成功LLM回答，或Phoenix是生产监控。
- Agent Reach提升了正式分析质量、已生产化；人工portfolio文章是Writer原始质量。
- 完全独立手写/专家级能力/公开一键复现，除非候选人能自己证明。项目由AI协作施工，资产仍有dirty/untracked和环境依赖。

## H. 至少五个面试故事

1. **历史身份与live合同。** 过去出场/首发不证明今天在队、健康或首发。原production合同误用于历史数据导致失败；改成显式历史候选，同侧引用，未确认保持未知。演示为什么这些是数据/适配问题，不是篮球推理差。
2. **Decimal/multipleOf误拦。** 合法0.56/0.58概率被二进制浮点schema拒绝。用Decimal精确校验与回归修validator，不调整预测值。指标按raw门和最终验收分开。
3. **accepted-state异常。** 临时accepted赋值后后续guard抛错，变量未清空使流程继续。修异常分支、用fixture断言无后续Writer；原15成功不回写，sidecar严格qualification14。该guard又对缺失说明过严，不能简单说出现真实盘口泄漏。
4. **修复本身会破坏内容。** uncertainties位置、引用顺序、schema annotation与真正的预测/球员/分钟不同。旧lexical保留和schema可能冲突；Final只承诺数值与选人保留，压缩文字不叫无损语义。一次repair失败就保留，不无限重试。
5. **Judge也需要评测。** Pro输出预算被thinking占用、JSON/引用失败、402；冻结失败后用统一非思考协议技术恢复，不挑分。最终全部放行只证明此Judge的判定，不证明能识别错误；需要Human检查false negatives。
6. **300静态和真实20的区别。** 小规模可解释评测比堆大数更有价值，shared来源/Golden重叠说明不是独立holdout；多版本回归总数相同仍有样本倒退。
7. **Agent Reach不接入的决策。** 多176URL不等于增加事实，历史资格/URL预算/early-stop可能抵消收益；shadow分母、来源与时效规则分开，未证明下游价值便停止。

## I. 面试官视角的直接判断

1. **展示的能力：** 数据合同/泄漏边界、测试集设计、Rubric、执行分层、受控恢复、版本追踪、错误归因、回归和观测。Human评测经验尚不能写完成。
2. **强于调API Demo：** 不只展示成功页，能拿出冻结考试卷、失败尝试、原输出、修复范围与条件指标；还能推翻自己先报的15/20。
3. **工程多但低面试收益：** 继续精修网页、维护睡眠daemon、全季数据补齐、更多搜索平台和严格网页历史取证；除非目标JD直接要求，收益低。
4. **值得写的数字：** 第F节的受控规模与方法结果；不使用未经校准的高分冒充性能收益。
5. **不建议写：** 74,545原资产盘点、各种累计大测试数、223当成功模型调用、100%metamorphic而隐去失败2对、所有hard PASS当准确率。
6. **最可能追问：** Golden怎么选；GT分离却为何仍可能有记忆；70%测什么；修复是否改意义；Judge与作者同模型偏差；Human0为什么finalized。
7. **讲不清会露怯：** raw schema7/20与E2E14/20；15篇为何只评14；47/47含20拒绝；640/160与138/85；production-equivalent=false及Pure unavailable；T-20目标与T-5实现。
8. **最影响竞争力的缺项：** 独立Human标签和disagreement RCA。当前70个硬判定全PASS、高soft集中，尚不能证明Judge判别能力。
9. **只再花2小时：** 按盲评工作流先评前6–8篇，不看Judge分，不按模型表现挑篇；导出basic agreement及具体分歧。再用两个RCA函数练讲，不写新平台。
10. **什么都不补能否投：** 能开始投递，定位AI应用评测/AIQuality校招或初级工程；不是单凭此项目就能证明高级基础模型科研评测能力。候选人必须能独立解释并复跑关键证据。

Human值得做，优先于更多工程。Forward不必等才能投递，若以后宣称预测收益/模型改进才必须有未见的未来样本。Postgame现在不补：已有Pregame评测方法足够，新增一套内容/事后偏见分母会分散精力。若某场面试专门问Postgame，最小未来方案是复用3–5份已有冻结结果/文章作hindsight核验，不新增收费生成、不当本轮已做。

## J. Final Gap Audit与停止决定

| 级别 | 问题 | 处理 |
|---|---|---|
| P0主张 | 原产出15与合格14、640/160、spans尝试/响应混淆风险 |已按最新产物收口，不改旧结果 |
| P0主张 | 完整线上等价/T-20已上线/人工精修当自动质量 |交接明确否定；不扩大生产施工 |
| P0接线 | closeout只认1task/0labels，用户标注后误报 |本轮修成合法chronological prefix；严格identity/data/prediction检查保留 |
| P1 | Human0、Judge未校准、缺disagreement分析 |用户标注现有样本，最值得补 |
| P1 | 候选人能否亲自解释合同与RCA |面试准备，不由更多代码替代 |
| P2 | Kappa未实现、forward/独立holdout、生产T-20 |以后有具体需求再做，不阻塞投递 |
| P2 | Postgame内容评测、不同Judge provider |仅在明确岗位/产品需求下做 |
| DROP | 再造candidate、再跑20/300、全历史互联网、全部确认首发、重做UI/调度/增加validators、生产化Agent Reach |明确停止 |

本轮实际小修：closeout默认允许真实标注和已解锁的合法前缀，仍拒绝未知/重复/越序任务、不同version/data、自动predictions；`--initial-workspace`仍可核验最初单任务零标签。5个新增synthetic测试不写入Label Studio。最新73 passed/0failed/0skipped与Final release68分别保留，不能重复累计。

修订过的README/报告/简历/面试材料均先备份。Prediction/Postgame immutablepayload/hash、原Golden/V1–V4/Final回答和Dataset均未重写。此轮未全面跑仓库、未重做源数据真实性审查、未重验production daemon/浏览器，不将这些说成通过。

## K. 接手顾问如何继续

只保留三件值得做的事：

1. 停止CourtVision功能开发，按目标JD整理简历、投递并演练五个RCA故事。
2. 完成盲评Human样本，产生真实基本agreement和具体disagreement，不能补造分数。
3. 如果岗位明确需要，再选一个限制做有限验证；不要把本文件的P2全部变成待施工清单。

### 当前入口与证据位置（理解本文件不依赖打开它们）

主仓库：`<local-workspace> project 4/25th-second`。产品站8625；Final Human8686/project1；Phoenix8682。旧人评8684和其他旧端口不能误当当前目标。

人评：在项目根目录使用 `evaluation/deepeval/.venv/Scripts/python.exe -m evaluation.finalization.human advance`／`calibrate`，真实提交之后再解锁。普通系统Python或兄弟仓库venv可能缺jsonschema，不代表评测逻辑失败。服务停机后可用同环境运行 `... -m evaluation.finalization.human prepare`，复用已有库/账号，不重导已存在任务。凭据只留本机，不分享.env或credentials.json。

最新实验基准：`evaluation/finalization/closeout-judge-recovery-01/verified/metrics.json`、当前派生`metrics.json`；最新本轮审计为`final-audit-20261013-tests.xml`和`final-audit-20261013-final-verification.json`。根`closeout-verification.json`是更早Judge阻塞快照，不是当前状态。Judge原始答案在`artifacts/golden-pregame-system-final-v1/judge-final-v1-provider-recovery-01`。机器版为同目录`FINAL_AI_HANDOFF.json`。

**停止结论：保持工程V1 FINALIZED，停止普通产品施工；把剩余时间投入真实人评、讲清证据和投递。**
