from pathlib import Path
import csv,json
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from .safe_csv import projected_rows
from evaluation.research_shadow.core import sha

ROOT=Path(__file__).resolve().parents[2]
SOURCE_GAMES=ROOT.parent/'courtvision/data/raw/Games.csv'
SOURCE_TEAMS=ROOT.parent/'courtvision/data/raw/TeamStatistics.csv'
PK=ROOT/'v2/data/player_knowledge.sqlite'
AUDIT=Path(str(Path.home() / '.agent-reach/audits/historical-t5-pilot-20261012'))
ET=ZoneInfo('America/New_York')
META_FIELDS={'gameId','gameDateTimeEst','gameDate','hometeamId','awayteamId','hometeamCity','hometeamName','awayteamCity','awayteamName','arenaName','arenaCity','gameType','gameLabel','gameSubLabel'}

def et_tipoff(raw):
    d=datetime.fromisoformat(raw)
    if d.tzinfo is not None:raise ValueError('unexpected_zoned_est_field_requires_explicit_source_review')
    return d.replace(tzinfo=ET).astimezone(timezone.utc).isoformat()

def golden_metadata():
    allowed={'sample_id','target_game_id','season','game_date','home_team','away_team','home_team_id','away_team_id','game_type','context_path','context_sha256'}
    p=ROOT/'evaluation/golden_set/v1/selection.csv'
    # Project in parser rather than constructing records containing private labels.
    return list(projected_rows(p,allowed))

def select_samples():
    items=golden_metadata();ids={r['target_game_id'].split(':')[-1]for r in items}
    meta={r['gameId'].zfill(10):r for r in projected_rows(SOURCE_GAMES,META_FIELDS,allowed_game_ids=ids)}
    for r in items:
        rawid=r['target_game_id'].split(':')[-1];m=meta.get(rawid)
        if not m:raise ValueError('golden_target_schedule_metadata_missing:'+rawid)
        if {int(m['hometeamId']),int(m['awayteamId'])}!={int(r['home_team_id']),int(r['away_team_id'])}:raise ValueError('target_identity_conflict')
        if int(m['hometeamId'])!=int(r['home_team_id']):raise ValueError('target_home_away_conflict')
        r['scheduled_tipoff']=et_tipoff(m['gameDateTimeEst'])
        r['cutoff_at']=(datetime.fromisoformat(r['scheduled_tipoff'])-timedelta(minutes=5)).isoformat()
        r['schedule_raw']=m;r['schedule_timezone']='America/New_York';r['scheduled_time_precision']='minute'
        r['source_selection_sha256']=sha((ROOT/'evaluation/golden_set/v1/selection.csv').read_bytes())
    items.sort(key=lambda r:(r['scheduled_tipoff'],r['target_game_id']))
    return items[:3]

def corroborate_pdf_clock(sample,pdf_rows):
    tricode={'Houston Rockets':'HOU','Toronto Raptors':'TOR','Los Angeles Lakers':'LAL','Minnesota Timberwolves':'MIN','Miami Heat':'MIA','Denver Nuggets':'DEN'}
    matchup=tricode[sample['away_team']]+'@'+tricode[sample['home_team']]
    local=datetime.fromisoformat(sample['scheduled_tipoff']).astimezone(ET)
    expected=local.strftime('%I:%M')+'(ET)';day=local.strftime('%m/%d/%Y')
    matching=[r for r in pdf_rows if r['matchup']==matchup and r['game_date']==day]
    clocks={r['game_time'].replace(' ','')for r in matching}
    if clocks!={expected}:raise ValueError('official_pdf_schedule_clock_conflict:'+str(clocks)+' expected '+expected)
    sample['schedule_official_clock']={'matchup':matchup,'game_date':day,'game_time_et':expected,
        'agreement':True,'adopted_timezone':'America/New_York','dst_applied':True,'actual_first_possession_not_used':True}
    return matching
