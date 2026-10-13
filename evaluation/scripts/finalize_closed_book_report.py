import json,urllib.request
import xml.etree.ElementTree as ET
from asset_scanner import EVAL,read_json,sha
from build_eval_baseline import save,text

report=read_json(EVAL/'reports/closed-book-benchmark-preparation.json')
report['full_file_validation']=read_json(EVAL/'reports/closed-book-full-validation.json')
report['export_readiness']=read_json(EVAL/'reports/closed-book-export-readiness.json')
report['data_protection']=read_json(EVAL/'reports/closed-book-protection-result.json')
report['reconstruction_source_bytes_unchanged']={p:sha(p)==h for p,h in report['source_hashes'].items()}
assert all(report['reconstruction_source_bytes_unchanged'].values())
freeze=read_json(EVAL/'reports/closed-book-freeze-manifest.json');report['frozen_dataset']={'dataset_id':freeze['dataset_id'],'files':len(freeze['files']),'overwrite_policy':'Refuse changed frozen input; separately authorized revision required'}
cases=ET.parse(EVAL/'reports/closed-book-tests.xml').getroot().findall('.//testcase')
report['tests']={'total':len(cases),'passed':sum(c.find('failure') is None and c.find('error') is None and c.find('skipped') is None for c in cases),
    'failed':sum(c.find('failure') is not None or c.find('error') is not None for c in cases),'skipped':sum(c.find('skipped') is not None for c in cases),
    'scope':'closed-book tests plus prior isolated preparation/replay-audit regressions; no production suite or model evaluation'}
with urllib.request.urlopen('http://127.0.0.1:8625/',timeout=15) as r:report['production_http_status']=r.status
report['counterfactual']={'schema':'datasets/counterfactual-mutation.schema.json','generator':'scripts/counterfactual.py','final_cases_generated':0,'default_expected_relation':None,'source_type':'synthetic_counterfactual'}
report['regular_season_candidate_count']=report['game_type_distribution'].get('regular_season',0)
report['files_added_or_modified']=['scripts/closed_book_data.py','scripts/closed_book_anonymize.py','scripts/validate_closed_book_context.py','scripts/build_closed_book_dataset.py',
    'scripts/export_closed_book.py','scripts/future_eval_adapters.py','scripts/counterfactual.py','scripts/test_closed_book.py','scripts/verify_closed_book_dataset.py',
    'scripts/freeze_closed_book.py','scripts/finalize_closed_book_report.py','scripts/closed_book_probe.py','datasets/counterfactual-mutation.schema.json',
    'datasets/closed_book_candidates.jsonl/.csv','datasets/closed_book_contexts/*.json','datasets/closed_book_provenance/*.json','datasets/closed_book_ground_truth/*.json',
    'datasets/anonymized/contexts/*.json','datasets/anonymized/mappings/*.json','datasets/anonymized/manifest.jsonl','datasets/closed_book_exports/*',
    'reports/closed-book-*','README.md']
report['remaining_blockers']=['No blocker to dataset preparation. Formal quality evaluation still needs authorized no-tool model runner, sampling/rubric/Judge/calibration decisions.',
    'Sixteen games lack two-sided historical player/rotation coverage; previous 2024-25 archive is explicitly partial.',
    'Anonymous input still contains statistical fingerprints; contamination reduction is not a guarantee.',
    'Recent form means latest observed eligible games, possibly across seasons, not guaranteed current-season latest 5/10. The three preseason contexts are shadow material, not forward evaluation.',
    'This mother pool contains no playoff targets and mainly one regular season; broad cross-season/playoff generalization is not established.']
report['dataset_boundaries']={'historical_closed_book':'Fixed prior-event inputs; no web/tools, quality/regression only; not strict replay or predictive performance.',
    'anonymous':'Same basketball information; identities/exact dates removed, hidden mappings excluded from model.',
    'forward':'Genuinely future regular-season primary evaluation; preseason shadow pipeline only; future predictive metrics not mixed with historical samples.'}
save(EVAL/'reports/closed-book-benchmark-preparation.json',report)
coverage='\n'.join(f"| {k} | {v['games']} | {v['percent']}% |" for k,v in report['coverage'].items())
text(EVAL/'reports/closed-book-benchmark-preparation.md',
    '# Controlled Closed-book Benchmark Preparation\n\n'+
    f"处理 {report['processed_games']} 场，生成 {report['successful_contexts']} 份普通context、{report['anonymized_contexts']} 份匿名context。常规赛1,228，季前赛3，覆盖30队。\n\n"+
    '## 结构完整度与覆盖\n\n'+json.dumps(report['completeness_distribution'],ensure_ascii=False)+'\n\n'+
    '7/7只表示七组资料均有可读内容，不是质量评分，也不意味着每个窗口满N或上一赛季档案齐全。1,228场的上一赛季指标明确为partial、附实际观察N；只有3场双方上一赛季档案完整。\n\n'+
    '| 字段 | 场数 | 覆盖率 |\n|---|---:|---:|\n'+coverage+'\n\n'+
    '## 50 / 100 / 300\n\n均有足够候选容量用于以后抽样。应保留完整度、partial及早赛季缺失分层。本轮没有抽取正式Benchmark、标注、Judge、Rubric或质量阈值。\n\n'+
    '## 时间规则与泄漏\n\n同来源日历日期严格小于目标日期，同日全部排除；不伪造UTC。模型文件只含目标基础身份与此前比赛的聚合；没有目标赛果、分钟、DNP、真实首发、市场、伤病或赛后文章。首发频率/分钟只用于历史候选，不代表本场名单。provenance逐字段保留真正使用的事件、source、max事件日期和字段hash。\n\n'+
    '数据源2024–25常规赛档案不完整；杯赛细标签Championship与大类Regular Season冲突，在本builder隔离识别为cup_championship，不编辑原数据或模型。上一赛季结束由目标日期前的总决赛系列赛完成资料核验，采集覆盖率另列。\n\n'+
    '## 验证与匿名化\n\n'+json.dumps(report['tests'],ensure_ascii=False)+'\n\n'+json.dumps(report['full_file_validation'],ensure_ascii=False)+'\n\n'+
    '负面fixtures覆盖目标赛果/分钟/首发、target进入窗口、同日/未来事件、错误hash/max日期、隐藏文件引用、错身份/主客、匿名真名/ID/日期；均按预期被拒绝。匿名映射只供评测端，真实身份同步替换；统计指纹仍可能存在。\n\n'+
    '## Counterfactual与工具导出\n\n仅schema及单变量生成器骨架，synthetic_counterfactual显式标记，expected relation默认null，未批量生成或设置正确数字变化。两套DeepEval export各1,231条、actual/expected output全为null；两套Label Studio import同样无回答/标注。未来SDK转换器必须显式提供真实模型回答，不会运行evaluate。\n\n'+
    '## 冻结与保护\n\n'+json.dumps(report['frozen_dataset'],ensure_ascii=False)+'\n\n'+json.dumps(report['data_protection'],ensure_ascii=False)+'\n\n'+
    f"生产8625 HTTP {report['production_http_status']}。0 LLM调用、0正式Prediction/Postgame/文章，未修改生产或Skill，未commit/push。\n\n"+
    '## 边界和剩余事项\n\n固定历史context用于quality/reasoning/regression；匿名版减少记忆线索但不证明零污染；真正预测表现由未来常规赛Forward Validation检验，季前赛只做shadow。市场/伤病均为unavailable。正式工具封闭运行器及人工质量标准未执行。\n\n'+
    '完整每队/赛季/早期缺失分布、文件清单及限制见同名JSON。最终输入字节冻结清单在closed-book-freeze-manifest.json。')
print(json.dumps({'tests':report['tests'],'site':report['production_http_status'],'frozen':report['frozen_dataset'],'protection':report['data_protection']},ensure_ascii=False))
