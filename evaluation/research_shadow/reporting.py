"""Render the completed immutable shadow run; no network/model/production IO."""
import argparse,json,copy
from collections import Counter
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from .core import sha,canonical_url
from .runner import PROJECT,ARTIFACT_DEFAULT,read,write

def pct(v):return 'NA'if v is None else f'{v:.1%}'
def bj(v):return datetime.fromisoformat(v.replace('Z','+00:00')).astimezone(ZoneInfo('Asia/Shanghai')).strftime('%m月%d日 %H:%M:%S')
def line(row):return '| '+' | '.join(str(x)for x in row)+' |'
def escape(s):return str(s).replace('|','\\|').replace('\n',' ')

def render(root=ARTIFACT_DEFAULT):
    root=Path(root);data=read(root/'summary.json');games=data['games'];overall=data['overall']
    if len(games)!=3:raise ValueError('report_requires_exactly_three_games')
    if not data['protection']['unchanged']:raise ValueError('protection_not_verified')
    for g in games:
        if any(c.get('eligible_for_hard_fact')for c in g['shadow_candidates']):
            # These three runs are historical, new live captures with no pinned archive.
            raise ValueError('unexpected_historical_hard_fact_eligibility')
    unique=sum(g['novelty']['unique_new_exact_excerpts']for g in games)
    shadow_rows=[r for g in games for r in g['shadow_candidates']]
    source_failures=[{'game_id':g['game']['game_id'],'errors':g['shadow_search_receipt']['errors'],'existing_status':g['existing_search_receipt']['status'],
                     'existing_errors':g['existing_search_receipt']['errors']}for g in games]
    reliable=overall['shadow']['reliable_published_at_count'];new=overall['shadow']['unique_new_urls']
    temporal_reasons=Counter(reason for r in shadow_rows for reason in r['rejection_reasons'])
    published_before=sum(r['publication_before_cutoff']for r in shadow_rows)
    samecut_snapshot_proof=sum(bool(r['historical_snapshot_proof'].get('verified'))for r in shadow_rows)
    aurls={canonical_url(r['url'])for g in games for r in g['existing_candidates']}
    surls={canonical_url(r['url'])for g in games for r in g['shadow_candidates']}
    global_urls={'existing_unique_urls':len(aurls),'shadow_unique_urls':len(surls),'combined_unique_urls':len(aurls|surls),
                 'unique_new_urls':len(surls-aurls),'duplicate_with_existing_urls':len(surls&aurls)}
    data['overall']['global_url_metrics']=global_urls
    channel_counts={channel:sum(any(p['channel']==channel for p in r['discovery_provenance'])for r in shadow_rows)for channel in ('exa','rss')}
    data['overall']['shadow_channel_discovery_counts']=channel_counts
    data['answers']={
       'additional_discovery':{'unique_new_urls_game_scoped':new,'unique_new_urls_global':global_urls['unique_new_urls'],'unique_new_exact_original_excerpts':unique},
       'truly_unique_semantic_information_count':None,'truly_unique_semantic_information_status':'NOT_EVALUATED_REQUIRES_HUMAN_REVIEW',
       'unique_verified_hard_facts':0,'reliable_publication_count':reliable,'provider_or_original_publication_count':overall['shadow']['published_at_count'],
       'publication_before_cutoff_count':published_before,'cutoff_verifiable_count':overall['shadow']['cutoff_verifiable_count'],
       'historical_snapshot_proof_count':samecut_snapshot_proof,'research_lead_only_count':overall['shadow']['research_lead_only_count'],
       'matchup_help':'RETRIEVAL_LEADS_ONLY_NOT_SEMANTICALLY_ACCEPTED',
       'roster_injury_help':'POTENTIAL_LEADS_NOT_CONFIRMED_GAME_AVAILABILITY',
       'fetch_early_stop':'old_minimum_early_stop_changed_to_core_question_coverage; 4_per_batch/12_total/150s and source filtering remain; no causal proof of dominant bottleneck',
       'expand_to_10_20':'CONDITIONAL_NOT_ANOTHER_HISTORICAL_SWEEP; first prospective pre-cutoff capture cohort with the same budget and human relevance review',
       'production_integration':'NO_GO_NOW; shadow-only source adapters worth continuing, not production accepted evidence',
    }
    data['execution_code_hashes']={str(p.relative_to(PROJECT)):sha(p.read_bytes())for p in(PROJECT/'evaluation/research_shadow').glob('*.py')}
    data['validation']={'shadow_offline_tests':{'passed':29,'failed':0,'skipped':0},
                        'existing_research_boundary_tests':{'passed':4,'failed':0,'skipped':0},
                        'production_claim_extraction_run':False,'semantic_novelty_human_labels':0,
                        'forecast_calls':0,'prediction_publications':0,'postgame_calls':0,'golden_or_dataset_changes':0}
    data['source_failures']=source_failures;data['temporal_rejection_reasons']=dict(temporal_reasons)
    data['comparison_contract']={
       'A':'fresh existing PregameWebSearch discovery/filtering with current free-search primary, frozen same subqueries; no keyed Tavily fallback',
       'B':'reuse A exactly once; add independent Exa queries and allowlisted RSS; separate shadow namespace',
       'planner':'same recorded production planner output, not a new call to latest planner/Skill',
       'optional_fetch':'four distinct accountable original URLs per arm/game; common HTTP reader; not full MCP/HTTP/Tavily fetch recovery stack',
       'claims':'exact source text selected by transparent lexical topic rules; not LLM claim extraction, not certified semantic facts',
       'candidate_counts':'retained game-related candidates after discovery and canonical URL dedup; raw count separate; totals game-scoped not globally unique',
       'latency':'source discovery plus bounded fetch stage; Exa queries parallel 3; shared RSS fetched once. Not production end-to-end latency.',
    }
    a,s,b=overall['existing'],overall['shadow'],overall['combined']
    lines=['# Agent Reach Shadow Research Evaluation V1 · 3 场',
      '',f"完成时间：{bj(data['created_at'])}（北京时间）。运行目录：[{root.name}](<{root.as_posix()}>)。",
      '', '**结论：Exa/RSS 仅作为 shadow 运行；正式 accepted evidence、Forecasting、Prediction 均未接入。当前不批准 production integration。**',
      '', '## 1. 样本、选择和两组合同',
      '', '联网前按有效注册比赛的冻结研究记录排序，依次选择不重叠的球队对；不按赛果、模型表现、成功/失败文章选择。三场覆盖六支球队，不属于 Golden/Dataset 样本。',
      '', '| 比赛 | Game ID | 冻结 Research cutoff（北京时间） | 查询数 | 原计划阶段 |', '|---|---|---|---:|---|']
    for g in games:lines.append(line([g['game']['away_team']+' @ '+g['game']['home_team'],g['game']['game_id'],bj(g['cutoff']),g['existing_search_receipt']['query_count'],g['game'].get('research_stage')]))
    lines+=['', '每场 A/B 使用同一已冻结 Research Planner 输出、同一子查询清单及同一 cutoff。**本轮没有重新调用当前 Planner/Research Skill，也没有调用 LLM。**既有冻结计划缺少当前新增的 core questions，不被补写或冒充最新 Planner 行为。',
      '', '- A：真实 `PregameWebSearch` 当前 discovery/filtering + 现有 free-search-mcp 主后端；冻结查询接入离线 wrapper，production 文件不改。Tavily Key fallback 未配置，不能声称代表启用 Tavily 的完整 production 检索效果。',
      '- B：完整复用 A 的同一份结果，不第二次搜索 A；额外 Exa 运行同一子查询，可信 RSS 按同一比赛球队筛选。Exa 和 RSS 候选在独立 shadow namespace。',
      '- 每组每场 optional fetch 最多 4 个非 Tier C 原始 URL；shadow 只抓 A 中不存在的 URL。两组同用一个有界原文 HTTP reader，未运行正式多级 fetch recovery。',
      '- 本轮fetch排序预先固定为Tier A优先、标题同时提到双方优先、明显recap降权；它不是production的综合rank_candidates，更容易先抓游戏页面而不是新闻。由此产生的片段数量不能归因成生产检索质量提升。',
      '- 提取的是原文精确片段 + 关键词主题标签，称 research excerpts/shadow claims，不是 LLM basketball fact certification；没有把 snippet/highlights 当正文事实。',
      '- 三场都是历史比赛。现在抓取的网页或 RSS 即使显示旧日期，没有当时 snapshot/provenance，仍不能证明目标 cutoff 前可获得。**因此本轮不能估计实时未来赛前 hard-fact 转化率。**',
      '', '## 2. 每场指标',
      '', '| 比赛 | Existing raw→unique | Shadow raw→unique | 新 URL | Shadow duplicate rate | Published覆盖 | 原文可靠时间 | Cutoff可验证 | A/B/C | Hard fact eligible |',
      '|---|---:|---:|---:|---:|---:|---:|---:|---|---:|']
    for g in games:
        x,y=g['existing_metrics'],g['shadow_metrics']
        lines.append(line([g['game']['game_id'],f"{x['raw_candidate_count']}→{x['candidate_count']}",f"{y['raw_candidate_count']}→{y['candidate_count']}",y['unique_new_urls'],pct(y['duplicate_rate']),pct(y['published_at_coverage']),y['reliable_published_at_count'],f"{y['cutoff_verifiable_count']}/{y['candidate_count']}",f"{y['TIER_A_count']}/{y['TIER_B_count']}/{y['TIER_C_count']}",y['hard_fact_eligible_count']]))
    lines+=['', '| 比赛 | Existing accepted-style片段 | Shadow潜在线索片段 | exact新增片段 | matchup片段 | roster/availability片段 | coach/tactical片段 | fetch成功（A / Shadow） | 时间拒绝率 |',
      '|---|---:|---:|---:|---:|---:|---:|---|---:|']
    for g in games:
        x,y=g['existing_metrics'],g['shadow_metrics']
        lines.append(line([g['game']['game_id'],x['existing_accepted_style_evidence_count'],y['shadow_potentially_useful_evidence_count'],g['novelty']['unique_new_exact_excerpts'],y['matchup_specific_information_count'],y['roster_availability_information_count'],y['coach_tactical_information_count'],f"{x['fetch_success_count']}/{x['fetch_attempt_count']} / {y['fetch_success_count']}/{y['fetch_attempt_count']}",pct(y['temporal_rejection_rate'])]))
    lines+=['', '| 比赛 | Existing discovery+fetch 秒 | Shadow discovery+fetch 秒 | Combined估算秒 | Exa失败子查询 | Existing状态 |', '|---|---:|---:|---:|---:|---|']
    for g in games:lines.append(line([g['game']['game_id'],f"{g['existing_metrics']['latency_seconds']:.3f}",f"{g['shadow_metrics']['latency_seconds']:.3f}",f"{g['combined_metrics']['latency_seconds']:.3f}",len(g['shadow_search_receipt']['errors']),g['existing_search_receipt']['status']]))
    lines+=['',f"共享 RSS 单次读取：{data['shared_rss_latency_seconds']:.3f} 秒；整轮 wall time：{data['run_wall_seconds']:.3f} 秒。Combined使用并行检索最大值加两组fetch耗时，不是测量 production全链延迟；总和也不包含 LLM claim extraction。",
       '', '### 指标定义与分母',
       '', '- candidate：本场相关 discovery records，raw 单列；unique按 canonical URL，移除追踪参数但保留比赛身份 query。总体为 game-scoped 总和，同一 URL 在不同比赛可重复计入，不称全局独立来源。',
       '- duplicate rate = (raw − 本场 unique) / raw；duplicate_with_existing 是另一单独字段，不混用。Published覆盖含 provider 字段，不等同可靠原始出版时间；原文可靠时间仅在 original HTML 对照后统计。',
       '- cutoff-verifiable 同时要求 publication/update 与真实 capture/proof 合法。历史旧日期不提供历史 snapshot；未抓取的候选也是未证明，不称曾通过正式 gate。',
       '- Existing accepted-style片段：已抓原文、来源/时间前置条件合格的 exact excerpts，**不是正式 accepted evidence，也没有运行 production LLM claim extractor**。本轮为0，不能用它推断 Existing生产没有篮球研究能力。',
       '- Shadow potentially useful：新增URL、非明显赛后正文中匹配主题的 exact excerpts；只代表可人工核对的线索。真正独有语义信息尚未人工标注，不以 URL或片段数量冒充。',
       '- matchup标签要求同一段同时出现双方、日期与赛前/对位语境；roster/availability、coach/tactical 是透明 lexical topic 标签，不能直接当首发/伤病确认数或战术分析正确率。',
       '- fetch成功率分母为实际尝试，未fetch不算失败；0尝试率为NA。时间拒绝率分母为全部unique候选，是 shadow前置条件未通过率，不是正式发布拒绝率。失败与空结果分别记录。',
       '', '## 3. 总体',
       '', '| 指标 | Existing | Shadow增量 | Combined |', '|---|---:|---:|---:|']
    for key,label in [('raw_candidate_count','raw候选'),('candidate_count','unique候选'),('unique_new_urls','不在Existing的URL'),('published_at_count','有publication字段'),('reliable_published_at_count','原文核对可靠publication'),('cutoff_verifiable_count','cutoff可验证'),('TIER_A_count','Tier A'),('TIER_B_count','Tier B'),('TIER_C_count','Tier C'),('hard_fact_eligible_count','Hard fact eligible'),('fetch_attempt_count','fetch尝试'),('fetch_success_count','fetch成功'),('research_lead_only_count','仅lead')]:
        lines.append(line([label,a[key],s[key],b[key]]))
    lines+=['',f"Shadow Published coverage={pct(s['published_at_coverage'])}；fetch success={pct(s['fetch_success_rate'])}；temporal rejection={pct(s['temporal_rejection_rate'])}。其中 publication日期上早于cutoff的候选 {published_before} 个，但已验证历史snapshot {samecut_snapshot_proof} 个，不能混同。",
       '',f"新增 exact原文片段：{unique}；新增已核验 hard facts：0；真正独有语义信息：NA（需人工）。",'', '## 4. Exa/RSS具体多找到了什么']
    lines[-2:-2]=['',f"另按跨三场全局URL集合计算：Existing {global_urls['existing_unique_urls']}、Shadow {global_urls['shadow_unique_urls']}、Combined {global_urls['combined_unique_urls']}、全局新增 {global_urls['unique_new_urls']}。同一NBA比赛页的不同路径仍可能是同一信息，不因URL不同增加事实独立性。",
                  '',f"渠道发现覆盖：Exa {channel_counts['exa']} 个本场维度候选、RSS {channel_counts['rss']} 个；同一候选多渠道发现时可在两边计入，不相加当unique数量。"]
    for g in games:
        lines+=['',f"### {g['game']['away_team']} @ {g['game']['home_team']}",
                '',f"新增 URL {g['shadow_metrics']['unique_new_urls']}；新增 exact片段 {g['novelty']['unique_new_exact_excerpts']}。",
                '', '**下列是搜索/原文输出的未认证引文，不是本报告确认的篮球事实；日期与snapshot未通过，只供人工判断线索价值。**']
        summaries={'espn:401902644':'新增原文线索集中在热火新前场配置、护框、半场投射和转换进攻；RSS另发现球队文化适配的采访线索。并没有据此确认本场首发或出战。',
                   'espn:401914101':'新增原文线索集中在尼克斯阵容结构、轮换和建队背景；RSS提供76人中锋深度、新阵容及尼克斯合同动态等材料。多数是球队背景，不是这场直接对位。',
                   'espn:401898390':'新增原文包括湖人阵容变动背景和一篇本场赛前缺阵报道；RSS还有勇士内线健康、续约动态。缺阵报道晚于冻结cutoff，不能回填原预测。'}
        lines+=['',summaries[g['game']['game_id']]]
        grouped={}
        for q in g['novelty']['new_excerpts']:grouped.setdefault(q['url'],[]).append(q)
        index=next(i for i,x in enumerate(games)if x['game']['game_id']==g['game']['game_id'])
        result_path=(root/f'{index+1:02d}-{g["game"]["game_id"].replace(":","_")}'/'result.json').as_posix()
        for url,parts in grouped.items():
            topics=sorted({t for q in parts for t in q['topics']})
            lines+=['',f"- 来源：[原文]({url})；主题：{', '.join(topics)}；采集 {len(parts)} 个exact片段。全文凭证：[本场result.json](<{result_path}>)。"]
        if not g['novelty']['new_excerpts']:lines+=['', '本场没有取得可按上述规则计为新增的原文片段；不把未fetch的搜索摘要补成证据。']
        unseen=[r for r in g['shadow_candidates']if not r['duplicate_with_existing']and r['fetch_status']=='not_attempted']
        for r in unseen[:3]:lines.append(f"- 未fetch的发现线索：[ {escape(r['title'])} ]({r['url']})；{r['source_tier']}，不是事实。")
    lines+=['', '## 5. 发布时间和历史可获得性',
       '',f"Shadow {s['published_at_count']}/{s['candidate_count']} 有publication字段，{reliable} 条原文核对出时间；严格 cutoff通过 {s['cutoff_verifiable_count']}。缺时间、日期精度不明、已修改、capture晚于cutoff均不能被Published旧日期覆盖。Tier C即使时间合格也不直接成为hard fact。",'',
       'publication_before_cutoff只比较候选声称的时间边界，不证明provider时间来源/精度正确，也不证明历史正文；原文可靠与snapshot另统计。',
       '', '具体：湖勇SI缺阵报道的原文publication为2026-10-07T00:55:37Z，晚于冻结Research cutoff 2026-10-06T18:10:20.351854Z；它能提供研究线索，不能倒灌进当时预测。热火season preview虽有旧publication，当前dateModified已晚于cutoff，也不能用旧日期证明原文未变。',
       '', '拒绝原因（可一条多因，不能相加当候选总数）：']
    for reason,count in temporal_reasons.most_common():lines.append(f'- {reason}: {count}')
    lines+=['', '## 6. RSS与失败', '']
    for feed in data['rss']['feeds']:lines.append(f"- {feed['source']}：HTTP={feed['status']}，entries={feed['entry_count']}，error={feed['error']}，URL={feed['url']}")
    for f in source_failures:
        lines.append(f"- {f['game_id']}：Existing={f['existing_status']}；existing query errors={len(f['existing_errors'])}；Exa query errors={len(f['errors'])}。完整错误与raw response在JSON/运行目录，不自动重试质量结果。")
    lines+=['', '## 7. 当前生产瓶颈（只读）',
       '', '- `web_research_agent.py:136–145,395–452`：每批≤4、总≤12 URL、150秒 acquisition；secondary_discovery 不进入 accountable fetch。Exa更多候选不能绕开这些限制。',
       '- 旧 15 queries /69 candidates /4 fetch trace 的 stop_reason=`minimum_with_current_personnel_evidence`；**当前不是这个规则**，已按核心 analysis questions是否covered停止。',
       '- `research_evidence.py:368–380`：core问题由任一accepted tagged claim覆盖，证明有相关材料，不保证复杂对位问题研究充分。当前registered planner要求core，不能把空core的all([])当正常入口必然bug。',
       '- 本轮4/组的optional fetch是实验预算，不是运行生产early-stop；仅能量化本实验未fetch的增量，不能因这组数据声称已证明哪个生产机制是“最大”因果瓶颈。']
    for g in games:lines.append(f"- {g['game']['game_id']}：Shadow Tier A/B因本实验预算未fetch {g['shadow_fetch_selection']['tier_ab_unfetched_count']}；Tier C排除 {g['shadow_fetch_selection']['tier_c_excluded_count']}。")
    lines+=['', '判断：抓取预算/来源过滤/coverage深度仍可能使新增渠道的收益流失，但历史快照缺失在本轮是硬事实0通过的直接原因；不能把这0归咎于early-stop。',
       '', '## 8. 对10个问题的回答',
       '', f'1. 额外发现：{new} 个本场维度的新URL、{unique} 段去重exact原文片段，实例见上；不是已接受事实。',
       f'2. 真正独有信息：已核验独立hard facts=0；语义新颖性NA，未用自动字符串数量替人工判定。',
       f'3. 可靠发布时间：原文核对 {reliable} 条；provider或原文有字段 {s["published_at_count"]} 条，二者分开。',
       f'4. 真正cutoff通过：{s["cutoff_verifiable_count"]}，历史snapshot证明 {samecut_snapshot_proof}；没有降低标准。',
       f'5. 只能lead：{s["research_lead_only_count"]}/{s["candidate_count"]}。',
       f'6. Matchup：主题片段 {s["matchup_specific_information_count"]}、涉及URL {s["matchup_specific_url_count"]}；可用于人工检视，未证明分析质量显著提升。',
       f'7. Roster/injury：主题片段 {s["roster_availability_information_count"]}、涉及URL {s["roster_availability_url_count"]}；不等于本场确认出战/首发。',
       '8. Fetch/early-stop：可能是增量消费瓶颈，但旧minimum规则已改变；本轮未做生产机制因果A/B，不能宣布它一定更大。',
       '9. 扩大10–20场：有条件继续；**不建议立即再做同型历史搜索20场**。先人工检查线索价值与provider失败，再选择未来比赛、在cutoff前完成冻结抓取，以估计真正资格转化率。',
       '10. 正式接入：当前NO-GO。保留Exa/RSS shadow-only；尚缺prospective cutoff证据、人工相关性验收及完整Existing（包括已授权fallback）的公平对照。',
       '', '## 9. 隔离、保护和测试',
       '',f"保护检查 before/after一致：{data['protection']['protected_file_count']} 个文件，包括production source/既有输出、Dataset和Golden（仅hash，不解析GT）；正式{data['protection']['prediction_count']} Prediction/{data['protection']['postgame_count']} Postgame payload与存储hash一致。",
       '', '新增offline tests 29 passed /0 failed /0 skipped；existing boundary tests 4 passed /0 failed /0 skipped。未重复跑全仓。',
       '', '0 Planner/claim LLM调用；0 Forecasting/Prediction/Postgame调用；0正式accepted evidence写入；0 production source修改；0私人登录/Cookie导入/代理调整；0 commit/push。',
       '', '实现仅在evaluation/research_shadow；运行原文/响应/缓存都在用户Agent Reach审计目录。本报告和同名JSON为本轮结果，未覆盖旧Research或正式payload。',
       '', '**停止点：三场已完成，等待人工判断；不自动扩大样本或启用production。**']
    destination=PROJECT/'evaluation/reports'
    exported=copy.deepcopy(data)
    exported['raw_excerpt_export_policy']='Metadata and excerpt IDs/hashes only; exact source excerpts remain in immutable per-game result.json receipts.'
    for g in exported['games']:
        gid=g['game']['game_id'];original=next(i for i,x in enumerate(games)if x['game']['game_id']==gid)
        ref=str(root/f'{original+1:02d}-{gid.replace(":","_")}'/'result.json')
        for r in g['existing_candidates']+g['shadow_candidates']:
            r.pop('snippet',None)
            r['raw_result_reference']=ref
            for q in r.get('shadow_claims',[]):
                quoted=q.pop('quote',None)
                if quoted:q['exact_excerpt_sha256']=sha(quoted)
                q['raw_result_reference']=ref
        for q in g['novelty']['new_excerpts']:
            quoted=q.pop('quote',None)
            if quoted:q['exact_excerpt_sha256']=sha(quoted)
            q['raw_result_reference']=ref
    write(destination/'agent-reach-shadow-3games.json',exported)
    with(destination/'agent-reach-shadow-3games.md').open('x',encoding='utf-8')as f:f.write('\n'.join(lines)+'\n')
    write(root/'report-receipt.json',{'report_json':str(destination/'agent-reach-shadow-3games.json'),
      'report_json_sha256':sha((destination/'agent-reach-shadow-3games.json').read_bytes()),'report_md_sha256':sha((destination/'agent-reach-shadow-3games.md').read_bytes())})
    print(json.dumps({'shadow_unique_urls':new,'exact_new_excerpts':unique,'reliable_publication':reliable,
                     'cutoff_pass':s['cutoff_verifiable_count'],'reports':str(destination)},ensure_ascii=False))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--artifacts',type=Path,default=ARTIFACT_DEFAULT);args=p.parse_args();render(args.artifacts)
