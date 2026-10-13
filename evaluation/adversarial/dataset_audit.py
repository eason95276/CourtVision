"""Independent source/duplicate/fingerprint audit; never calls the context builder."""
import json,csv,sys,math
from collections import Counter,defaultdict
from datetime import date
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,ROOT,RUNTIME_DB,read_json,canonical_hash,gid,connect
from build_eval_baseline import save
from closed_book_anonymize import identity_leaks

def sources():
    example=json.loads((EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()[0]);p=read_json(EVAL/example['provenance_path'])
    index={};games={}
    for name in p['source_hashes']:
        path=Path(name)
        if path.suffix!='.csv':continue
        with path.open(encoding='utf-8-sig',newline='') as f:
            for n,r in enumerate(csv.DictReader(f),2):
                day=(r.get('gameDateTimeEst') or r['gameDate'])[:10];key=gid(r['gameId'])
                row={'game_id':key,'day':day,'source_row':n}
                if 'teamScore' in r:row.update(team_id=int(r['teamId']),opponent_id=int(r['opponentTeamId']),pf=float(r['teamScore']),pa=float(r['opponentScore']))
                else:
                    row.update(home_id=int(r['hometeamId']),away_id=int(r['awayteamId']),home_points=float(r['homeScore']),away_points=float(r['awayScore']));games[key]=row
                index[str(path)+':'+str(n)]=row
    with connect(ROOT/'v2/data/player_knowledge.sqlite') as c:
        for r in c.execute('SELECT * FROM player_game_facts'):
            d=dict(r);d['game_id']=gid(d['game_id']);d['day']=d['game_date'][:10];index[str(ROOT/'v2/data/player_knowledge.sqlite')+'#player_game_facts/'+str(d['fact_id'])]=d
    # Snapshot sources need their exact retained schedule metadata, not today's clock.
    with connect(RUNTIME_DB) as c:
        for r in c.execute('SELECT result_id,payload_json FROM game_result_snapshots'):
            d=json.loads(r['payload_json']);key=d['identity']['canonical_game_id'];s=c.execute('SELECT scheduled_tipoff FROM schedule_catalog_games WHERE game_id=?',(key,)).fetchone()
            if not s:continue
            from datetime import datetime
            from zoneinfo import ZoneInfo
            day=datetime.fromisoformat(s[0].replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York')).date().isoformat()
            row={'game_id':key,'day':day,**{k:d['game'][k] for k in ('home_points','away_points')}}
            row.update(home_id=d['identity'].get('home_team_id'),away_id=d['identity'].get('away_team_id'))
            games[key]=row;index[str(RUNTIME_DB)+'#game_result_snapshots/'+r['result_id']]=row
    return index,games

def run():
    def normalize(v):
        if isinstance(v,dict):return {k:normalize(x) for k,x in v.items() if k not in {'days_before_target','game_date','event_date','season','task_mode','interpretation','basis','scope','reason'}}
        if isinstance(v,list):return [normalize(x) for x in v]
        return v
    rows=[json.loads(s) for s in (EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()]
    am={r['base_sample_id']:r for r in (json.loads(s) for s in (EVAL/'datasets/anonymized/manifest.jsonl').read_text(encoding='utf-8').splitlines())}
    src,rawgames=sources();issues=[];explicit=[];normal_hashes=defaultdict(list);anon_hashes=defaultdict(list);normalized=defaultdict(list);identity=defaultdict(list);flipped=defaultdict(list);score_pairs=defaultdict(list)
    date_variants=defaultdict(set)
    for item in src.values():date_variants[item['game_id']].add(item['day'])
    disagreement={key:sorted(value) for key,value in date_variants.items() if len(value)>1}
    fingerprints={k:defaultdict(list) for k in ('recent_scores','record','h2h_sequence','player_lines')};byid={r['target_game_id']:r['sample_id'] for r in rows};visible_edges=[];shared_edges=set();duplicates=[];partial_counter=Counter();source_events=0
    gs={r['sample_id'] for r in read_json(EVAL/'golden_set/v1/selection_manifest.json')['samples']}
    for r in rows:
        c=read_json(EVAL/r['context_path']);p=read_json(EVAL/r['provenance_path']);g=read_json(EVAL/r['ground_truth_path']);a=am[r['sample_id']];anon=read_json(EVAL/a['context_path']);mapping=read_json(EVAL/a['mapping_path'])
        leakage=identity_leaks(anon,mapping)
        if leakage:explicit.append({'sample':r['sample_id'],'issues':leakage})
        normal_hashes[canonical_hash(c)].append(r['sample_id']);anon_hashes[canonical_hash(anon)].append(r['sample_id']);normalized[canonical_hash(normalize(anon))].append(r['sample_id'])
        identity[(r['game_date'],r['home_team_id'],r['away_team_id'])].append(r['sample_id']);flipped[(r['game_date'],*sorted([r['home_team_id'],r['away_team_id']]))].append(r['sample_id'])
        score_pairs[(g['home_points'],g['away_points'])].append(r['sample_id'])
        target=r['target_game_id'];day=c['matchup']['game_date'];resolved={}
        for field,proof in p['fields'].items():
            resolved[field]=[]
            for e in proof['source_events']:
                source_events+=1;actual=src.get(e['source_ref'])
                if not actual:issues.append({'sample':r['sample_id'],'code':'source_ref_unresolved','field':field,'source':e['source_ref']});continue
                if actual['game_id']!=e['game_id'] or actual['day']!=e['event_time']:issues.append({'sample':r['sample_id'],'code':'source_event_provenance_mismatch','field':field,'declared':e,'actual':{'id':actual['game_id'],'day':actual['day']}})
                if actual['game_id']==target or actual['day']>=day:issues.append({'sample':r['sample_id'],'code':'actual_source_not_past','field':field})
                resolved[field].append(actual)
                if e['game_id'] in byid and e['game_id']!=target:shared_edges.add((byid[e['game_id']],r['sample_id']))
        for side in ('team_a','team_b'):
            t=c[side];team=t['team_id'];record=t['record'];erows=[x for x in resolved['team_record'] if x.get('team_id')==team]
            if record['sample_games']!=len(erows):issues.append({'sample':r['sample_id'],'code':'independent_record_count','side':side})
            if record.get('wins_before_game') is not None and record['wins_before_game']!=sum(x.get('pf',0)>x.get('pa',0) for x in erows):issues.append({'sample':r['sample_id'],'code':'independent_record_wins','side':side})
            for n in (5,10):
                w=t['recent_form']['recent_'+str(n)];items=w['games']
                if len(items)>n or w['sample_games']!=len(items):issues.append({'sample':r['sample_id'],'code':'independent_window_size'})
                for item in items:
                    matching=[x for x in resolved['recent_form'] if x['game_id']==item['game_ref'] and x.get('team_id')==team]
                    if not matching or matching[0].get('pf')!=item['points_for'] or matching[0].get('pa')!=item['points_against']:issues.append({'sample':r['sample_id'],'code':'independent_visible_score','side':side})
                    if item['game_ref'] in byid and item['game_ref']!=target:visible_edges.append({'earlier_sample':byid[item['game_ref']],'later_sample':r['sample_id'],'group':'recent_form'})
            playerrows=[x for x in resolved['player_history'] if x.get('team_id')==team and 'player_id' in x]
            counts=Counter((x['game_id'],str(x['player_id'])) for x in playerrows)
            for k,n in counts.items():
                if n>1:duplicates.append({'sample':r['sample_id'],'team':team,'game':k[0],'player':k[1],'source_fact_rows':n})
            for player in t['player_history']['players']:
                pr=[x for x in playerrows if str(x['player_id'])==str(player['player_id']) and x['participation_status']=='played']
                if len(pr)!=player['recorded_appearances']:issues.append({'sample':r['sample_id'],'code':'independent_player_appearance','player':player['player_id']})
                nums=[x['points'] for x in pr if x['points'] is not None]
                expected=round(sum(nums)/len(nums),3) if nums else None
                if expected!=player['points_per_appearance']['value']:issues.append({'sample':r['sample_id'],'code':'independent_player_average','player':player['player_id']})
            rest=c['schedule_context'][side];pastdays=[x['day'] for x in resolved['rest'] if x.get('team_id')==team]
            if pastdays:
                delta=(date.fromisoformat(day)-date.fromisoformat(max(pastdays))).days
                if rest['days_rest']!=delta-1 or rest['back_to_back']!=(delta==1):issues.append({'sample':r['sample_id'],'code':'independent_rest_arithmetic'})
            partial_counter[t['previous_season']['status']]+=1
        for h in c['head_to_head']['meetings']:
            actual=rawgames.get(h['game_ref'])
            if not actual:continue
            pa=actual['home_points'] if actual['home_id']==c['team_a']['team_id'] else actual['away_points'];pb=actual['away_points'] if actual['home_id']==c['team_a']['team_id'] else actual['home_points']
            if (h['team_a_points'],h['team_b_points'])!=(pa,pb):issues.append({'sample':r['sample_id'],'code':'independent_h2h_direction'})
            if h['game_ref'] in byid and h['game_ref']!=target:visible_edges.append({'earlier_sample':byid[h['game_ref']],'later_sample':r['sample_id'],'group':'h2h'})
        fp={'recent_scores':[[[x['points_for'],x['points_against']] for x in c[s]['recent_form']['recent_5']['games']] for s in ('team_a','team_b')],
            'record':[[c[s]['record'].get('wins_before_game'),c[s]['record'].get('losses_before_game')] for s in ('team_a','team_b')],
            'h2h_sequence':[[h['team_a_points'],h['team_b_points']] for h in c['head_to_head']['meetings']],
            'player_lines':[[[x['minutes_per_appearance'],x['points_per_appearance']['value'],x['assists_per_appearance']['value']] for x in c[s]['player_history']['players'][:5]] for s in ('team_a','team_b')]}
        for k,v in fp.items():fingerprints[k][canonical_hash(v)].append(r['sample_id'])
    def dups(m):return [v for v in m.values() if len(v)>1]
    gold_edges=[e for e in visible_edges if e['earlier_sample'] in gs and e['later_sample'] in gs]
    report={'contexts_audited':len(rows),'source_events_checked':source_events,'independent_source_issues':issues,'explicit_anonymous_leaks':explicit,
        'duplicates':{'normal_exact':dups(normal_hashes),'anonymous_exact':dups(anon_hashes),'normalized':dups(normalized),'same_date_identity':dups(identity),'same_date_flipped_identity':dups(flipped),
            'repeated_score_values_groups':len(dups(score_pairs)),'note':'Repeated score values are not proof of duplicate games.'},
        'statistical_fingerprints':{k:{'distinct':len(v),'unique_vectors':sum(len(ids)==1 for ids in v.values()),'total':len(rows)} for k,v in fingerprints.items()},
        'cross_sample_dependencies':{'all_source_history_edges':len(shared_edges),'visible_prior_result_edges':len(visible_edges),'golden_visible_edges':gold_edges,
            'note':'Valid per-case past results can reveal another case outcome. Stateless model requests and chronological human workflow are required; no independent random train/test claim.'},
        'duplicated_player_event_facts':duplicates,'source_calendar_date_disagreements':disagreement,
        'previous_season_status_counts':dict(partial_counter),'model_calls':0}
    save(EVAL/'reports/dataset-adversarial-audit.json',report)
    print(json.dumps({k:v for k,v in report.items() if k not in ('independent_source_issues','duplicated_player_event_facts','cross_sample_dependencies','duplicates')},ensure_ascii=False))
    print('SOURCE_ISSUES',len(issues),'DUP_PLAYER_EVENTS',len(duplicates),'GOLDEN_VISIBLE_EDGES',len(gold_edges))
    return report

if __name__=='__main__':run()
