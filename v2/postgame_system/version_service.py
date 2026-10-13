"""Published-version-aware result sync and append-only Postgame V1."""
from __future__ import annotations
import copy
import json
import uuid
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from v2.pregame_system.storage import content_hash, digest, now, save
from v2.pregame_system.prediction_versions import _instant, _json, payload_sha256
from v2.pregame_system.espn_boxscore_normalizer import normalize_espn_summary_payload, normalize_identity_name
from v2.pregame_system.market import team_id
from .record import build_postgame_record
from .ats_evaluation import aggregate_ats_evaluations


def normalize_completed_summary(game, summary, source):
    event_id=game['id'].removeprefix('espn:')
    header=summary.get('header') or {}
    comp=(header.get('competitions') or [{}])[0]
    if str(header.get('id'))!=event_id or str(comp.get('id'))!=event_id:
        raise ValueError('result_event_identity_mismatch')
    if not (comp.get('status') or {}).get('type',{}).get('completed') or comp['status']['type'].get('state')!='post':
        raise ValueError('result_not_final')
    if _instant(comp.get('date'),'result_tipoff')!=_instant(game['tipoff'],'scheduled_tipoff'):
        raise ValueError('result_tipoff_identity_mismatch')
    if source.get('status')!='success' or digest(source['raw_path'])!=source['sha256']:
        raise ValueError('result_raw_source_unverified')
    if content_hash(json.loads(Path(source['raw_path']).read_text(encoding='utf-8')))!=content_hash(summary):
        raise ValueError('result_normalization_input_hash_mismatch')
    sides={c['homeAway']:c for c in comp.get('competitors',[])}
    if set(sides)!={'home','away'}:raise ValueError('result_sides_missing')
    mapping={}
    for side,c in sides.items():
        if team_id(c)!=game[side]['id']:raise ValueError('result_team_identity_mismatch')
        mapping[str(c['team']['id'])]=game[side]['id']
    home,away=int(sides['home']['score']),int(sides['away']['score'])
    if min(home,away)<=0 or home==away:raise ValueError('result_score_invalid')
    if bool(sides['home'].get('winner'))!=(home>away) or bool(sides['away'].get('winner'))!=(away>home):
        raise ValueError('result_winner_conflict')
    date=_instant(game['tipoff'],'tipoff').astimezone(ZoneInfo('America/New_York')).date().isoformat()
    season=int(game.get('season_start') or datetime.fromisoformat(date).year)
    identity={'canonical_game_id':game['id'],'espn_game_id':game['id'],'nba_game_id':game.get('nba_game_id'),
        'home_team_id':game['home']['id'],'away_team_id':game['away']['id'],'scheduled_tipoff':game['tipoff'],'eastern_date':date}
    identity['identity_hash']=content_hash(identity)
    actual={'game_id':game.get('nba_game_id') or game['id'],'canonical_game_id':game['id'],'game_date':date,
        'scheduled_tipoff':game['tipoff'],'season_start':season,'season':f'{season}-{str(season+1)[-2:]}',
        'season_type':'Preseason' if game['game_type']=='preseason' else 'Playoffs' if game['game_type']=='playoffs' else 'Regular Season',
        'status':'final','home_team_id':game['home']['id'],'away_team_id':game['away']['id'],
        'home_team':sides['home']['team']['displayName'],'away_team':sides['away']['team']['displayName'],
        'home_team_tricode':sides['home']['team']['abbreviation'],'away_team_tricode':sides['away']['team']['abbreviation'],
        'home_points':home,'away_points':away,'actual_home_margin':home-away,'actual_total':home+away,
        'winner_team_id':game['home']['id'] if home>away else game['away']['id']}
    rows=normalize_espn_summary_payload(actual,summary,mapping,expected_event_id=event_id)
    players=[]
    for p in rows:
        players.append({**p,'person_id':None,'full_name':p['player_name'],'minutes':p['minutes_seconds']/60 if p['minutes_seconds'] is not None else None,
            'reboundsTotal':p['rebounds'],'reboundsOffensive':p['offensive_rebounds'],'reboundsDefensive':p['defensive_rebounds'],
            'fieldGoalsMade':p['fgm'],'fieldGoalsAttempted':p['fga'],'threePointersMade':p['three_pm'],
            'threePointersAttempted':p['three_pa'],'freeThrowsMade':p['ftm'],'freeThrowsAttempted':p['fta'],
            'id_namespace':'espn'})
    teams=[]
    for t in summary['boxscore']['teams']:
        tid=mapping[str(t['team']['id'])];side='home' if tid==game['home']['id'] else 'away'
        stats={s['name']:s.get('displayValue') for s in t['statistics']}
        row={'side':side,'team_id':tid,'team_name':t['team']['displayName'],'team_tricode':t['team']['abbreviation'],
            'points':home if side=='home' else away}
        for field,keys in {'fieldGoalsMade-fieldGoalsAttempted':('fieldGoalsMade','fieldGoalsAttempted'),
            'threePointFieldGoalsMade-threePointFieldGoalsAttempted':('threePointersMade','threePointersAttempted'),
            'freeThrowsMade-freeThrowsAttempted':('freeThrowsMade','freeThrowsAttempted')}.items():
            raw=stats.get(field)
            if raw is not None:
                values=[int(v)for v in raw.split('-')]
                if len(values)!=2 or values[0]>values[1]:raise ValueError('result_shooting_invalid')
                row.update(zip(keys,values))
        for field,key in {'totalRebounds':'reboundsTotal','offensiveRebounds':'reboundsOffensive','defensiveRebounds':'reboundsDefensive',
            'assists':'assists','turnovers':'turnovers','steals':'steals','blocks':'blocks'}.items():
            row[key]=int(stats[field]) if stats.get(field) is not None else None
        if all(row.get(k) is not None for k in ('fieldGoalsMade','threePointersMade','freeThrowsMade')) and 2*row['fieldGoalsMade']+row['threePointersMade']+row['freeThrowsMade']!=row['points']:
            raise ValueError('result_scoring_identity_conflict')
        scored=sum(p['points'] for p in players if p['team_id']==tid and p['points'] is not None)
        if scored!=row['points']:raise ValueError('result_player_points_sum_conflict')
        teams.append(row)
    pbp=[{**p,'period':(p.get('period')or{}).get('number'),'scoreHome':p.get('homeScore'),'scoreAway':p.get('awayScore')}for p in summary.get('plays',[])]
    core={'identity':identity,'game':actual,'team_boxscores':teams,'player_boxscores':players,'play_by_play':pbp,'shots':[],
        'period_scores':{side:[int(x['displayValue'])for x in c.get('linescores',[])]for side,c in sides.items()},
        'components':{'result':'available','team_boxscores':'available','player_boxscores':'available',
            'play_by_play':'available' if pbp else 'missing','shots':'missing'},'errors':{},'receipts':{'summary':source}}
    # A re-fetch of an unchanged ESPN box score gets a new receipt path and
    # capture time. Keep that provenance in the frozen payload, but exclude it
    # from result identity so retries do not create duplicate result versions.
    stable_result={key:value for key,value in core.items() if key!="receipts"}
    return {**core,'bundle_hash':content_hash(stable_result),'refreshed_at':now()}


def project_player_usage_review(player_usage, player_boxscores):
    """Compare pregame minute ranges without turning missing minutes into DNP."""
    players = player_boxscores or []
    comparisons = []
    valid_participation = {
        "did_not_play_confirmed", "played", "data_missing", "unknown",
    }
    for estimate in player_usage or []:
        name = str(estimate.get("player_name") or "")
        match = next((player for player in players
            if normalize_identity_name(player.get("full_name") or player.get("player_name") or "")
            == normalize_identity_name(name)), None)
        raw_status = str((match or {}).get("participation_status") or "").strip().casefold()
        minutes = (match or {}).get("minutes")
        if match is None:
            participation = "not_in_source"
            status = "not_in_source"
            minutes = None
        else:
            # Older normalized bundles may predate the participation field.
            # Positive source minutes prove appearance; absent minutes never prove DNP.
            if raw_status in valid_participation:
                participation = raw_status
            elif minutes is not None:
                participation = "played"
            else:
                participation = "unknown"
            if participation == "did_not_play_confirmed":
                minutes = None
                status = "did_not_play_confirmed"
            elif participation == "data_missing":
                status = "data_missing"
            elif participation == "unknown":
                status = "unknown"
            elif minutes is None:
                # ESPN can confirm a player appeared via a boxscore stat while
                # its displayed minute field is absent. Preserve both facts.
                status = "minutes_missing" if participation == "played" else "unknown"
            else:
                status = ("supported" if estimate["minutes_min"] <= minutes <= estimate["minutes_max"]
                          else "not_supported")
        comparisons.append({
            "player_name": estimate["player_name"],
            "side": estimate["side"],
            "estimated_minutes": [estimate["minutes_min"], estimate["minutes_max"]],
            "actual_minutes": minutes,
            "participation_status": participation,
            "minute_precision": "whole_minute_source",
            "status": status,
        })
    return comparisons


class PostgameVersionService:
    def __init__(self, versions):
        self.versions=versions
        with versions.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS game_result_snapshots (
                    result_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,captured_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS postgame_versions (
                    version_id TEXT PRIMARY KEY,game_id TEXT NOT NULL,base_prediction_version_id TEXT NOT NULL,
                    result_id TEXT NOT NULL,payload_json TEXT NOT NULL,payload_sha256 TEXT NOT NULL,published_at TEXT NOT NULL,
                    FOREIGN KEY(base_prediction_version_id) REFERENCES prediction_versions(version_id),
                    FOREIGN KEY(result_id) REFERENCES game_result_snapshots(result_id));
                CREATE TRIGGER IF NOT EXISTS postgame_no_update BEFORE UPDATE ON postgame_versions BEGIN SELECT RAISE(ABORT,'immutable_postgame_version'); END;
                CREATE TRIGGER IF NOT EXISTS postgame_no_delete BEFORE DELETE ON postgame_versions BEGIN SELECT RAISE(ABORT,'immutable_postgame_version'); END;
                CREATE TRIGGER IF NOT EXISTS result_no_update BEFORE UPDATE ON game_result_snapshots BEGIN SELECT RAISE(ABORT,'immutable_result_snapshot'); END;
                CREATE TRIGGER IF NOT EXISTS result_no_delete BEFORE DELETE ON game_result_snapshots BEGIN SELECT RAISE(ABORT,'immutable_result_snapshot'); END;
                CREATE INDEX IF NOT EXISTS idx_postgame_idempotency
                    ON postgame_versions(game_id,base_prediction_version_id,result_id,published_at);
            ''')

    def sync_result(self, game, summary, source):
        bundle=normalize_completed_summary(game,summary,source)
        ident=bundle['bundle_hash']
        with self.versions.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                db.execute('INSERT OR IGNORE INTO game_result_snapshots VALUES (?,?,?,?,?)',
                    (ident,game['id'],_json(bundle),payload_sha256(bundle),source['captured_at']))
                row=db.execute('SELECT registered_game_json FROM game_prediction_state WHERE game_id=?',(game['id'],)).fetchone()
                if not row or not row['registered_game_json']:
                    raise ValueError('registered_game_state_missing')
                current=json.loads(row['registered_game_json'])
                if current.get('status')!='final' or current.get('result_snapshot_id')!=ident:
                    current.update(status='final',result_snapshot_id=ident)
                    db.execute('UPDATE game_prediction_state SET registered_game_json=?,state_revision=state_revision+1 WHERE game_id=?',
                        (_json(current),game['id']))
                db.commit()
            except Exception:db.rollback();raise
        return ident,bundle

    def publish(self, game, result_id, *, required_base_version=None):
        selected=self.versions.select_current(game)
        base=selected.get('version')
        if not base or (required_base_version and base['version_id']!=required_base_version):
            raise ValueError('postgame_last_published_prediction_mismatch')
        if _instant(base['published_at'],'published_at')>=_instant(game['tipoff'],'tipoff'):
            raise ValueError('postgame_base_not_pregame')
        with self.versions.connect() as db:
            row=db.execute('SELECT * FROM game_result_snapshots WHERE result_id=? AND game_id=?',(result_id,game['id'])).fetchone()
        if not row:raise ValueError('postgame_result_identity_mismatch')
        bundle=json.loads(row['payload_json'])
        if payload_sha256(bundle)!=row['payload_sha256']:raise ValueError('postgame_result_hash_mismatch')
        record=build_postgame_record(bundle,official_prediction=base)
        record['game_flow']['period_scores']=bundle['period_scores']
        record['record_hash']=content_hash({k:v for k,v in record.items()if k not in ('record_hash','generated_at','output')})
        plan=next((m['pregame_plan']for m in base.get('editorial_material',[])if m.get('pregame_plan')), {})
        comparisons=project_player_usage_review(plan.get('player_usage',[]),bundle.get('player_boxscores',[]))
        lineups=[]
        for row in plan.get('lineups',[]):
            actual=[p['full_name']for p in bundle['player_boxscores']if p['team_id']==game[row['side']]['id'] and p['starter']==1]
            predicted=row['players'];keys={normalize_identity_name(n)for n in actual}
            lineups.append({'side':row['side'],'predicted':predicted,'actual':actual,
                'retained_count':sum(normalize_identity_name(n)in keys for n in predicted)})
        decomposition=[]
        own={t['side']:t for t in bundle['team_boxscores']}
        for side,t in own.items():
            decomposition.append({'side':side,'field_goal_points':2*t['fieldGoalsMade']+t['threePointersMade'],
                'free_throw_points':t['freeThrowsMade'],'bench_points':sum(p['points']or 0 for p in bundle['player_boxscores']if p['team_id']==t['team_id'] and p['starter']==0)})
        public=record['prediction_review']['public_prediction']
        errors=public['errors']
        evaluation={'winner_correct':errors['win_correct'],'predicted_home_margin':public['prediction']['expected_margin'],
            'actual_home_margin':bundle['game']['actual_home_margin'],'margin_error':errors['signed_margin_error'],'absolute_margin_error':errors['absolute_margin_error'],
            'predicted_total':public['prediction']['expected_total'],'actual_total':bundle['game']['actual_total'],
            'total_error':errors['signed_total_error'],'absolute_total_error':errors['absolute_total_error'],
            'error_definition':'prediction minus actual; margin uses home minus away'}
        ats_review=(record.get('prediction_review')or{}).get('spread_prediction_review') or {}
        evaluation['ats']={
            'schema_version':'postgame-ats-evaluation-v1',
            'prediction_version_id':base['version_id'],
            'prediction_version_payload_sha256':base['payload_sha256'],
            'market':copy.deepcopy(ats_review.get('market')),
            'independent':copy.deepcopy(ats_review.get('independent') or {}),
            'final':copy.deepcopy(ats_review.get('final') or {}),
        }
        payload={'schema_version':'postgame-version-v1','game_id':game['id'],'version_id':str(uuid.uuid4()),
            'base_prediction_version_id':base['version_id'],'base_prediction_payload_sha256':base['payload_sha256'],'base_stage':base['stage'],
            'result_id':result_id,'record':record,'evaluation':evaluation,'pregame_plan':plan,'minutes_review':comparisons,
            'lineup_review':lineups,
            'scoring_decomposition':decomposition,'unknown_before_game':copy.deepcopy(base['analyst_context'].get('missing_data',[])),
            'lessons':[{'type':'review','text':'本场胜负方向未得到赛果支持。' if not errors['win_correct'] else '本场胜负方向得到赛果支持。'},
                {'type':'process','text':'赛前核心球员出场与轮换不确定，需要在下一场重新取证，不能把本场结论直接沿用。'}],
            'automatic_lesson_feedback':False,'published_at':now()}
        observed=[r for r in comparisons if r['actual_minutes'] is not None]
        scored={r['side']:r for r in decomposition}
        bench_relevant=abs(scored['home']['bench_points']-scored['away']['bench_points'])>=abs(bundle['game']['actual_home_margin'])/2
        pregame_bench=any(word in base['judgment'].get('analysis_article','')for word in ('替补','新人','第二阵容'))
        payload['judgment_review']=[{'judgment':'胜负方向','status':'supported' if errors['win_correct'] else 'not_supported',
            'reason':'以实际最终比分验证。'},
            {'judgment':'核心球员有限负荷','status':'supported' if observed and all(r['actual_minutes']<=r['estimated_minutes'][1] for r in observed) else 'not_verifiable',
                'reason':'实际已取得的球员分钟支持有限使用；具体区间下限仍有多处估计偏高。'},
            {'judgment':'替补与新人会影响比赛','status':'supported' if bench_relevant and pregame_bench else 'not_verifiable',
                'reason':'替补得分差至少达到最终分差的一半，支持赛前关注轮换的观察。' if bench_relevant and pregame_bench else '未同时具备明确的赛前判断与足够的赛后得分构成支撑。'},
            {'judgment':'未知出战与教练安排','status':'unknown_before_game','reason':'赛后才能确认的首发和分钟不能回填成赛前已知。'}]
        payload['article']=render_review(payload)
        from v2.frontend.player_display_names import catalog,translate_text
        payload['display_names']=catalog(self.versions,bundle['player_boxscores'])
        payload['display_article']=translate_text(payload['article'],payload['display_names'])
        with self.versions.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                existing=db.execute('SELECT * FROM postgame_versions WHERE game_id=? AND base_prediction_version_id=? AND result_id=? ORDER BY published_at DESC,rowid DESC LIMIT 1',
                    (game['id'],base['version_id'],result_id)).fetchone()
                if existing:
                    saved=json.loads(existing['payload_json'])
                    if payload_sha256(saved)!=existing['payload_sha256']:
                        raise ValueError('postgame_payload_hash_mismatch')
                    db.commit()
                    return saved
                db.execute('INSERT INTO postgame_versions VALUES (?,?,?,?,?,?,?)',
                    (payload['version_id'],game['id'],base['version_id'],result_id,_json(payload),payload_sha256(payload),payload['published_at']))
                db.commit()
            except BaseException:
                db.rollback()
                raise
        return payload

    def ats_evaluation_summary(self):
        """Aggregate each game's latest immutable Postgame version, without fetching data."""
        with self.versions.connect() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='postgame_versions'").fetchone():
                return aggregate_ats_evaluations([])
            rows=db.execute('SELECT game_id,payload_json,payload_sha256 FROM postgame_versions ORDER BY published_at,rowid').fetchall()
        latest_by_game={}
        for row in rows:
            value=json.loads(row['payload_json'])
            if payload_sha256(value)!=row['payload_sha256']:
                raise ValueError('postgame_payload_hash_mismatch')
            latest_by_game[row['game_id']]=value.get('evaluation',{}).get('ats',{})
        return aggregate_ats_evaluations(list(latest_by_game.values()))


def read_postgame(versions, game_id):
    with versions.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='postgame_versions'").fetchone():return None
        row=db.execute('SELECT * FROM postgame_versions WHERE game_id=? ORDER BY published_at DESC,rowid DESC LIMIT 1',(game_id,)).fetchone()
    if row is None:return None
    payload=json.loads(row['payload_json'])
    if payload_sha256(payload)!=row['payload_sha256']:raise ValueError('postgame_payload_hash_mismatch')
    return payload


def read_postgame_status(versions, game_id, *, game_status, has_published_pregame):
    """Small read-only status payload for schedule/detail rendering."""
    post=read_postgame(versions,game_id)
    if post:
        return {'status':'available','version_id':post['version_id'],
            'base_prediction_version_id':post['base_prediction_version_id'],
            'base_stage':post.get('base_stage'),'published_at':post.get('published_at')}
    if str(game_status or '').casefold() in {'final','post','completed','finished'}:
        return {'status':'pending' if has_published_pregame else 'unavailable'}
    return None


def result_for_game(versions, game_id):
    with versions.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='game_result_snapshots'").fetchone():return None
        row=db.execute('SELECT * FROM game_result_snapshots WHERE game_id=? ORDER BY captured_at DESC,rowid DESC LIMIT 1',(game_id,)).fetchone()
    if not row:return None
    value=json.loads(row['payload_json'])
    if payload_sha256(value)!=row['payload_sha256']:raise ValueError('result_payload_hash_mismatch')
    return value


def render_review(p):
    r=p['record'];g=r['game'];e=p['evaluation'];teams={t['side']:t for t in r['team_boxscores']};d={t['side']:t for t in p['scoring_decomposition']}
    lines=[f"{g['away_team_tricode']} {g['away_points']}—{g['home_points']} {g['home_team_tricode']}",
        f"本场实际主队分差 {g['actual_home_margin']:+g}，总分 {g['actual_total']}。复盘基准是最后成功发布的 {p['base_stage']}，不是失败的 FINAL 产物。",
        f"赛前预计主队分差 {e['predicted_home_margin']:+g}、总分 {e['predicted_total']:g}；胜负方向{'正确' if e['winner_correct'] else '错误'}，分差绝对误差 {e['absolute_margin_error']:g} 分，总分绝对误差 {e['absolute_total_error']:g} 分。",
        '比赛的计分差距：']
    for side in ('away','home'):
        t=teams[side];s=d[side]
        lines.append(f"{t['team_tricode']} 投篮 {t['fieldGoalsMade']}/{t['fieldGoalsAttempted']}，三分 {t['threePointersMade']}/{t['threePointersAttempted']}，罚球 {t['freeThrowsMade']}/{t['freeThrowsAttempted']}；投篮得分 {s['field_goal_points']}、罚球得分 {s['free_throw_points']}、替补得分 {s['bench_points']}。")
    winner='home' if g['actual_home_margin']>0 else 'away';loser='away' if winner=='home' else 'home'
    bench_gap=d[winner]['bench_points']-d[loser]['bench_points']
    starter_gap=(teams[winner]['points']-d[winner]['bench_points'])-(teams[loser]['points']-d[loser]['bench_points'])
    lines.append(f"从得分构成看，获胜方的替补得分差 {bench_gap:+g} 分，首发得分差 {starter_gap:+g} 分，两项合计对应最终 {abs(g['actual_home_margin'])} 分差。这场需要重点复盘的是实际轮换和替补产出，而不是把赛前纸面阵容直接等同于本场表现。")
    lines.append('关键球员：')
    for player in r['key_players']:
        lines.append(f"{player['name']}：{player['points']} 分、{player['rebounds']} 篮板、{player['assists']} 助攻。")
    lines.append('赛前判断复核：')
    for row in p.get('judgment_review',[]):
        labels={'supported':'得到赛果支持','not_supported':'没有得到支持','not_verifiable':'无法充分验证','unknown_before_game':'赛前未知'}
        lines.append(f"{row['judgment']}：{labels[row['status']]}。{row['reason']}")
    for row in p.get('lineup_review',[]):
        lines.append(f"{'猛龙' if row['side']=='home' and g['home_team_tricode']=='TOR' else g['home_team_tricode'] if row['side']=='home' else g['away_team_tricode']} 预计首发与实际首发重合 {row['retained_count']}/{len(row['predicted'])}；实际首发：{'、'.join(row['actual'])}。")
    for row in p['minutes_review']:
        if row.get('participation_status') == 'did_not_play_confirmed':
            lines.append(f"{row['player_name']} 预计 {row['estimated_minutes'][0]}–{row['estimated_minutes'][1]} 分钟，本场确认未出场（DNP），分钟区间不适用。")
            continue
        if row['actual_minutes'] is not None:
            actual=f"实际 {row['actual_minutes']:g} 分钟"
        elif row.get('participation_status') == 'played':
            actual='已确认出场，但分钟数据缺失'
        elif row.get('participation_status') == 'data_missing':
            actual='出场数据不完整，分钟数据缺失'
        elif row.get('participation_status') == 'not_in_source':
            actual='未在本次球员数据源中找到，出场状态未知'
        else:
            actual='出场状态未知，分钟未取得'
        result = ('在预计区间内' if row['status']=='supported' else
                  '不在预计区间内' if row['status']=='not_supported' else
                  '分钟数据缺失' if row['status']=='minutes_missing' else
                  '数据源不完整' if row['status']=='data_missing' else
                  '未出现在数据源' if row['status']=='not_in_source' else '尚无法验证')
        lines.append(f"{row['player_name']} 预计 {row['estimated_minutes'][0]}–{row['estimated_minutes'][1]} 分钟，{actual}，{result}。")
    lines.append('赛前出场、确认首发和教练使用计划仍有未知项。完整 box score 可以验证负荷、得分构成与方向误差，不能单凭这些数字确认某一战术或对位造成了胜负。')
    return '\n\n'.join(lines)
