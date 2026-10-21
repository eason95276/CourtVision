"""Public scoreboard facts only. Never imports a model, Agent, or local database."""
import argparse
import json
import math
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from publish_game import save

CHINA=ZoneInfo('Asia/Shanghai')


def fetch_board(day):
    url='https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates='+day.strftime('%Y%m%d')
    request=urllib.request.Request(url,headers={'User-Agent':'CourtVision-Schedule/1.0'})
    with urllib.request.urlopen(request,timeout=25)as response:
        value=json.load(response)
    if not isinstance(value.get('events'),list):raise ValueError('scoreboard_events_missing')
    return {'url':url,'payload':value}


def build_schedule(boards,teams,selection,at):
    if at.tzinfo is None:raise ValueError('timezone_required')
    today=at.astimezone(CHINA).date();days={today+timedelta(days=n)for n in range(3)}
    mapping={str(t['espn_team_id']):t for t in teams['teams']}
    picks={g['game_id']for day,rows in selection['slates'].items()if day in {d.isoformat()for d in days}for g in rows}
    games={}
    for board in boards:
        for event in board['payload']['events']:
            tip=datetime.fromisoformat(event['date'].replace('Z','+00:00'))
            if tip.tzinfo is None:raise ValueError('schedule_timezone_missing')
            if tip.astimezone(CHINA).date()not in days:continue
            competition=event['competitions'][0];sides={r['homeAway']:r for r in competition['competitors']}
            if set(sides)!={'home','away'}:raise ValueError('schedule_sides_invalid')
            converted={}
            for side,row in sides.items():
                identity=mapping[str(row['team']['id'])]
                converted[side]={k:v for k,v in identity.items()if k!='espn_team_id'}
                if competition['status']['type'].get('state') in {'in','post'}:
                    try:
                        score=float(row['score'])
                        if math.isfinite(score) and score>=0:
                            converted[side]['score']=int(score) if score.is_integer() else score
                    except (KeyError,TypeError,ValueError):
                        pass
            state=competition['status']['type'];name=state.get('name','').lower()
            status=('cancelled'if'cancel'in name else'postponed'if any(s in name for s in('postpon','suspend','delay'))else
                'final'if state.get('completed')and state.get('state')=='post'else
                {'pre':'scheduled','in':'in_progress'}.get(state.get('state'),'unknown'))
            gid='espn:'+str(event['id'])
            game={'id':gid,'tipoff':tip.astimezone(timezone.utc).isoformat(),'status':status,**converted,
                'venue':(competition.get('venue')or{}).get('fullName'),'neutral_site':bool(competition.get('neutralSite')),
                'game_type':{1:'preseason',2:'regular_season',3:'playoffs'}.get((event.get('seasonType')or{}).get('type')or(event.get('season')or{}).get('type'),'unknown'),
                'season':(event.get('season')or{}).get('year'),'schedule_catalog_fact':True,'homepage_visible':True,
                'registered_game':False,'report_selected':gid in picks,'beijing_date':tip.astimezone(CHINA).date().isoformat(),
                'source':{'schedule_captured_at':at.isoformat()},'missing':[],
                'postgame':None}
            if gid in games and any(games[gid][s]['id']!=game[s]['id']for s in('home','away')):
                raise ValueError('duplicate_schedule_identity_conflict')
            games[gid]=game
    return {'schema_version':'courtvision-public-schedule-v1','timezone':'Asia/Shanghai','generated_at':at.isoformat(),
        'coverage_dates':sorted(d.isoformat()for d in days),'complete':True,'source_urls':[b['url']for b in boards],
        'games':sorted(games.values(),key=lambda g:(g['tipoff'],g['id'])),'generation_calls':0}


def sync(root=ROOT,at=None,fetcher=fetch_board):
    at=at or datetime.now(timezone.utc);today=at.astimezone(CHINA).date()
    # An extra product day lets the browser cross Beijing midnight without a blank tomorrow.
    days=[today+timedelta(days=n)for n in range(-1,3)]
    with ThreadPoolExecutor(max_workers=3)as pool:boards=list(pool.map(fetcher,days))
    teams=json.loads((root/'config/nba_teams.json').read_text(encoding='utf-8'))
    selection=json.loads((root/'config/daily_picks.json').read_text(encoding='utf-8'))
    result=build_schedule(boards,teams,selection,at)
    save(root/'public_data/schedule.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=ROOT)
    result=sync(parser.parse_args().root)
    print(json.dumps({'games':len(result['games']),'coverage_dates':result['coverage_dates'],'generation_calls':0}))
