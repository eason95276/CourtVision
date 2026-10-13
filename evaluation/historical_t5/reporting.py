"""Coverage and policy comparison only; no model, outcome or Human evaluation."""
import json
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from .metadata import ROOT,AUDIT
from .assemble import OUTPUT
from evaluation.research_shadow.runner import read,write
from evaluation.research_shadow.core import sha

def bj(t):return datetime.fromisoformat(t.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
def render():
    manifest=read(OUTPUT/'manifest.json');samples=manifest['samples'];contexts=[read(s['context_path'])for s in samples]
    if len(samples)!=3 or not all(s['validation']['passed']for s in samples):raise ValueError('three_validated_snapshots_required')
    if not manifest['protection']['unchanged']:raise ValueError('protected_assets_not_verified')
    counts={field:dict(Counter(s['coverage'][field]for s in samples))for field in samples[0]['coverage']}
    result={**manifest,'coverage_totals':counts,'official_injury_games':sum(s['injury_rows']>0 for s in samples),'official_injury_rows':sum(s['injury_rows']for s in samples),
       'legal_market_games':sum(s['coverage']['market']!='UNAVAILABLE'for s in samples),
       'confirmed_lineup_games':sum(s['coverage']['confirmed_lineup']!='UNAVAILABLE'for s in samples),
       'source_expected_lineup_games':sum(s['coverage']['expected_lineup']!='UNAVAILABLE'for s in samples),
       'news_temporal_a':0,'news_temporal_b':sum(s['research_temporal_b']for s in samples),'official_injury_temporal_a':3,
       'high_quality_snapshot_count':sum(s['high_quality_t5_snapshot']for s in samples),
       'quality_definition':'Declared before assembly: valid identity and prior-event inputs, source-bound roster candidates, official injury, legal timestamped market; source lineup or documented unknown. Market missing prevents high-quality complete-product claim.',
       'tests':{'pytest_passed':18,'pytest_failed':0,'pytest_skipped':0,'real_context_validator_passed':3},
       'limitations':['latest official injury edition not exhaustively indexed; latest-known within bounded scan',
         'historical CSV/ESPN events are canonical retrospectively collected history, not exact original API revision snapshots',
         'previous season has 81 Houston/80 Lakers observed valid paired games; estimates are not official NBA advanced ratings',
         'full target-date roster archives not found; identity candidates are partial and not availability',
         'untimed market benchmark rows rejected; a source article can mention odds without proving book quote time',
         'Temporal B original-source metadata does not cryptographically prove no unreported later material change',
         'three selected samples cannot establish 1231-game feasibility or source coverage for Golden20',
         'no model/Judge/Human evaluation; no silent replacement of existing frozen Human target'],
       'answers':{
         '1_high_quality_t5_snapshots':'0/3 under declared complete-product criterion; 3/3 validated partial reconstructions',
         '2_injury':'3/3 official two-team reports, 31 statuses; latest-known bounded discovery only',
         '3_market':'0/3 legal timestamped quotes in existing assets; no closing-time fabrication',
         '4_confirmed_lineup':'1/3 games, 2/6 teams, pre-cutoff original media report; not boxscore',
         '5_expected_lineup':'1/3 games, 2/6 teams, source_expected only; Houston/Toronto lead lacks original timestamp proof',
         '6_team_recent_h2h':'3/3 deterministic records/rest/H2H/player history; recent10 legitimately short; previous metrics partial for two games',
         '7_research':'3/3 with ordinary Temporal B source evidence, 6 articles; official injury Temporal A counted separately',
         '8_hard_fields':'book quote time, complete roster archive, exact lineups for all teams, exhaustive last report discovery, material article revision history',
         '9_old_strict_0_of_1231':'Not a false audit. Strict original capture-as-was contract differs from archive reconstruction. New rules recover official documents and prior events, but market/lineup asset gaps are real; three cannot identify dominant cause across1231.',
         '10_expand_golden20':'Conditional, not automatic: first approve proof policy and missing-market handling; prioritize coverage reconnaissance before rebuilding20',
         '11_sources_needed':'official dated PDFs, timestamped original lineup reporters/team posts, bookmaker or vendor timestamped historical quotes, target-date transaction/roster archives, canonical historical event sources',
         '12_main_human_input':'No direct replacement now. Requires new input/target revision, adaptation contract, missing-market rubric decision, human source review and locked-context model execution; old labels/outputs remain bound to old inputs'},
       'implementation_files':{str(p.relative_to(ROOT)):sha(p.read_bytes())for p in(ROOT/'evaluation/historical_t5').glob('*.py')}}
    lines=['# Historical T-5 Replay Dataset Pilot · 3场', '',f"完成于 {bj(manifest['created_at'])}（北京时间）。仅数据重建；0 Forecast / Market Review / Editorial / Prediction / Judge / Human / Postgame调用。",'',
      '**结果：3/3 数据包通过验证，但完整产品口径下的高质量 T-5 Snapshot 为 0/3，主要硬缺口是现有历史盘口缺报价时间。不是把部分成功写成全部 COMPLETE。**',
      '', '## 1. 样本与时间', '',
      '从现有Golden V1只投影公开身份/日期/路径字段，按时区感知的scheduled tipoff排序选最早三场。未用赛果、模型成功/失败、V1–V4表现或检索难易选择；没有改Golden顺序、Baseline或Label Studio。',
      '', '| sample_id | 比赛 | Game ID | 开球北京时间 | T-5 cutoff北京时间 |', '|---|---|---|---|---|']
    for s in samples:lines.append('| '+ ' | '.join([s['sample_id'],s['game'],s['game_id'],bj(s['scheduled_tipoff']),bj(s['cutoff_at'])])+' |')
    lines+=['', '时间证据：Games.csv原始无时区gameDateTimeEst保留；采用America/New_York并逐场用官方赛前PDF的ET比赛时钟确认。10月用UTC-4，11月5日用UTC-5，不固定加12小时。名义时间为分钟精度，不宣称实际第一跳球时刻。',
      '', '因此本pilot最早是火箭@猛龙（当地18:30），早于湖人@森林狼（21:30）；不沿用旧Baseline首场顺序来猜真实时间。',
      '', '## 2. Coverage', '', 'C=COMPLETE；P=PARTIAL；U=UNAVAILABLE。C必须按明确观察范围理解，不代表所有字段或所有原始信息均已找到。',
      '', '| 字段 | 火箭@猛龙 | 湖人@森林狼 | 热火@掘金 |', '|---|---|---|---|']
    for field in counts:lines.append('| '+field+' | '+' | '.join({'COMPLETE':'C','PARTIAL':'P','UNAVAILABLE':'U'}[s['coverage'][field]]for s in samples)+' |')
    lines+=['', '- Team record：从target之前完成的有效配对比赛计算W/L、主客战绩、streak，实际场数与元数据应有场数一致才C。',
      '- Recent：只用本赛季；实际不足5/10场标P，不拿上一季/季后赛填窗口。',
      '- Previous season：ORtg/DRtg/net/pace为注明公式的boxscore估计，不冒充NBA官方advanced ratings；N和每指标有效观测数保留。',
      '- Player history的C指本场球队既有赛前已完成比赛在PK中覆盖；不是满10场，也不是完整名单或当前可用性。',
      '- Roster保持P：历史出场只是身份候选；官方injury报告能证明当时列名球队归属和所列状态，不证明全名单/所有未列球员Available。',
      '', '| 比赛 | 主/客本季实际N | 主/客上季N | H2H实际N | 主/客历史球员数 |', '|---|---:|---:|---:|---:|']
    for s,c in zip(samples,contexts):
        v=lambda g:c[g]['value']
        lines.append('| '+s['game']+' | '+f"{v('team_records')['home']['observed_n']}/{v('team_records')['away']['observed_n']}"+' | '+f"{v('previous_season_metrics')['home']['observed_n']}/{v('previous_season_metrics')['away']['observed_n']}"+' | '+str(v('head_to_head')['observed_n'])+' | '+f"{len(v('player_history')['home']['players'])}/{len(v('player_history')['away']['players'])}"+' |')
    lines+=['', '## 3. 官方Injury Report：3/3，31条', '', '| 比赛 | 正文report time UTC | 距cutoff分钟 | 记录数 | PDF |', '|---|---|---:|---:|---|']
    for s,c in zip(samples,contexts):
        injury=c['injury']['value'];lines.append('| '+s['game']+' | '+s['injury_report_time']+' | '+str(s['injury_age_minutes'])+' | '+str(s['injury_rows'])+' | [官方版本]('+injury['source_url']+') |')
    lines+=['', '每份PDF保留正文时间、URL、document hash、页码、球队、玩家原名、官方状态与reason。Questionable/Probable/Available等不转成Out；未列名不等于Available。',
      '', '**Latest-known，不是声称最新-ever：**旧2025文件名 `_05PM.pdf` 的正文是17:30；新式 `_05_30PM.pdf` 访问失败不能据此说旧报告没有。按正文时间选择，并探测末段旧小时/5分钟候选路径；当前官方索引不提供完整历史目录，403也不能证明从未存在。因此保留latest_edition_exhaustively_proven=false。',
      '', '视觉核对发现旧通用提取函数会让页眉“Injury Report”污染跨页球队上下文，遗漏爱德华兹等记录。**只在独立pilot读取器修复**：湖人@森林狼9→12条，热火@掘金12→13条；火箭@猛龙6条不变。同时保留LeBron/DaRon原始拼写。生产解析器未修改，不能把pilot修复称为生产修复。',
      '', '## 4. 首发', '', '- 湖人@森林狼：双方confirmed_before_cutoff；来源SI赛前报道及其中记者名单，published=2025-10-30T01:03:30Z、modified=01:04:13Z，早于cutoff01:25Z。没读目标Box Score。',
      '- 热火@掘金：双方source_expected；原文明确PROJECTED STARTERS，published=2025-11-05T19:11:33Z、modified=20:20:45Z，均早于cutoff2025-11-06T01:55Z。没有升级确认。',
      '- 火箭@猛龙：unknown。USA Today原文读取受阻；辅助Reader能读出probable五人名单，但只得到日期级/第三方时间，不能证明满足截止。保留lead，不靠历史分钟排名或目标starter回填。',
      '- Agent expected未生成，因为本轮禁止模型；不是把Unknown强制改预测名单。',
      '', '## 5. Market：0/3', '', '只查现有CSV和历史market snapshot资产，未读取目标赛果、ATS/Total settlement或prediction error。候选closing数据无可信quote timestamp，不能造captured_at或把closing标签当T-5时间。',
      '', '热火掘金SI预览确有DraftKings参考，但它不是本轮既有timestamped market资产，且书商实际quote time未保留；没有把文章发布时间冒充书商报价时间。若以后允许“媒体发布时已观察到的报价”这一独立TEMPORAL_B类型，需明确新协议，而不是静默升级。',
      '', '## 6. Research：新闻A=0、B=6；官方文档A另计3', '', '每场都有原始赛前文章的合格B证据（2/1/3篇）。满足原文可信publication<=cutoff、无证据表明关键正文在cutoff后实质修改；无需当日本地snapshot。DateModified晚于cutoff、provider-only日期、无原始时间或postgame路由进入C/拒绝。',
      '', 'Source acquisition优先official injury、lineup、late availability，再rotation/coach/matchup；没有让season preview先抢预算。相关文章/推荐/脚本和图片caption不作为主文事实。报告不复制全文；原始响应、正文hash、原文URL和短证据保留。',
      '', 'B是带置信度的普通Research evidence，不是逐句语义认证，也不能证明出版社从未无标记修订；A/B/C用于新pilot，旧production strict replay不改。',
      '', '合格来源：']
    for c in contexts:
        for e in c['research']['value']['evidence']:lines.append('- ['+e['title'].replace('|','/')+']('+e['source_url']+')；published='+e['published_at']+'；updated='+str(e.get('updated_at'))+'；TEMPORAL_B。')
    lines+=['', '## 7. 与同三场Current Closed-book比较', '', '| 组 | 旧Closed-book | T-5新增/差异 |', '|---|---|---|',
      '| Injury | unavailable | 3份官方时戳报告、31状态/原因 |',
      '| Market | unavailable | 仍不可用；没有为增加覆盖伪造时间 |',
      '| Lineup | 历史轮换候选，不是当前首发 | 1场确认、1场预计、1场unknown |',
      '| Roster | 历史球员/轮换 | 官方injury列名身份补充；仍无完整archive |',
      '| Research | unavailable | 6篇普通B赛前来源 |',
      '| Team/recent/H2H/player/rest | 本来已有多组历史事实 | 主客战绩、streak、3-in-4、精确T5及口径；不是全部新增 |',
      '', '特别差异：旧recent5在赛季早期可能拼入上一季/季后赛以满5；本pilot当前赛季不足5时保持实际N。上季Metrics仍受source缺场限制，不能把82期待场数当有效观测。旧context/manifest/选择均未修改。',
      '', '## 8. 12个最终问题', '',
      '1. 高质量完整T5：0/3；已验证的部分重建：3/3。预先质量定义要求可验证市场、official injury和合法基础数据，而非临时调低。',
      '2. Injury：3/3、31条；latest-known和完整索引证明分开。',
      '3. Market：0/3，真实时间资产缺口。',
      '4. Confirmed lineup：1/3场、2/6队。',
      '5. Expected lineup：1/3场、2/6队；没有agent生成。',
      '6. Team/recent/H2H：3/3能确定性计算；recent窗口因赛季初不足而P；上季两场有80/81有效N。',
      '7. Research：3/3有B新闻，共6篇；A伤病文档3份单列。',
      '8. 真难字段：原始书商quote time、完整当日roster、所有球队精确首发来源、穷尽最终injury版本、文章实质修订史。',
      '9. Strict replay 0/1231：不能说旧审计错了，也不能凭3场断言“主要都因过严”。旧要求还原原始采集/响应，和本轮archive reconstruction定义不同；新许可恢复官方文档及已经发生的统计，但盘口/首发资产缺口仍真实。',
      '10. Golden20：值得有条件继续覆盖侦察，不建议立刻大批重建或运行模型；先决定市场缺失和TEMP_B口径，人工检查pilot。没有自动扩20。',
      '11. 扩20来源：官方按时报告PDF、可靠记者/球队赛前首发原文、带时间版本的书商/供应商historical odds、当日交易/名单archive、可校验历史事件数据库。',
      '12. 主Human输入：现在不能直接替换冻结目标。需另建输入/target revision、明确哪些评分在market缺失时NA、验证适配器和source confidence，并重新绑定模型输出；旧Human/模型产物继续属于旧输入。Context不泄漏也不证明LLM预训练不知道历史赛果，后续评测仍要区别。',
      '', '## 9. 验收与保护', '', '18项pytest passed /0 failed /0 skipped；3个真实context validator passed。验证目标排除、未来事件、时区/DST、官方报告时间、lineup身份和basis、市场时间、源引用、样本N及跨页状态；没有跑模型/评委或人工评分。',
      '',f"{manifest['protection']['protected_files']}个既有source/输出/Dataset/Golden文件before/after hash一致；正式{manifest['protection']['prediction_versions']} Prediction和{manifest['protection']['postgame_versions']} Postgame payload/hash保持不变。母库只读，无迁移，无重训，无production/Skill变更，无commit/push。",'',
      '构建器不实例化旧Ledger（它先读全表再过滤）。本pilot先投影身份、按允许的pre-target IDs才读取CSV统计，SQL WHERE先排除全部三目标和未来日期；GT目录仅做字节hash保护，不解析目标内容。目标结果/箱分/分钟/赛后starter从未用于Context。',
      '', '普通工程修正：canonical Context hash与file-byte hash分开；SQLite tuple/JSON array用同一JSON合同比较；write-once审计文件不被重写。失败组装草稿保留在独立审计目录，未覆盖旧研究/正式版本。',
      '', '### 文件', '']
    for s in samples:lines.append('- ['+s['sample_id']+' context](<'+Path(s['context_path']).as_posix()+'>)；hash='+s['context_sha256'])
    lines+=['', f'原始采集/拒绝/诊断：[audit](<{AUDIT.as_posix()}>)。代码仅新增在evaluation/historical_t5；pilot目录含context、validation、comparison、selected PDF和manifest。',
            '', '**停止：三场数据重建完成；等待人工判断，不扩20、不运行任何模型、不接production。**']
    write(ROOT/'evaluation/reports/historical-t5-pilot-3games.json',result)
    path=ROOT/'evaluation/reports/historical-t5-pilot-3games.md'
    with path.open('x',encoding='utf-8')as f:f.write('\n'.join(lines)+'\n')
    print(json.dumps({'report':str(path),'validated':3,'high_quality':0,'official_injury':3,'market':0,'confirmed_games':1,'expected_games':1,'research_B':6}))

if __name__=='__main__':render()
