"""Bounded public GETs with raw-body evidence and immutable attempt records."""
import json
import time
import urllib.request
import urllib.error
import threading
import requests
from pathlib import Path
from .storage import BASE, save, digest, now, new_run

ALLOWED = {'www.atlhawksdigital.com','cdn.atlhawksdigital.com','cdn.celtics.com','www.basketball-reference.com','www.nba.com','content-api-prod.nba.com','cdn.nba.com','stats.nba.com','site.api.espn.com','sports.core.api.espn.com','api.actionnetwork.com'}
_THREAD=threading.local()


def get(url, max_bytes=8*1024*1024, *, parse_json=True):
    from urllib.parse import urlparse
    if urlparse(url).scheme!='https' or urlparse(url).hostname not in ALLOWED:
        raise ValueError('Unapproved public endpoint')
    folder=new_run('network')
    save(folder/'attempt.json',{'url':url,'requested_at':now(),'retries':0})
    receipt={'url':url,'retry_count':0,'captured_at':None}
    try:
        start=time.monotonic()
        if not hasattr(_THREAD,'session'):
            _THREAD.session=requests.Session()
        with _THREAD.session.get(url,headers={'User-Agent':'CourtVision-Research/2.0','Accept':'application/json'},
                                 timeout=(5,12),stream=True,allow_redirects=False) as response:
            chunks=[];size=0
            for chunk in response.iter_content(65536):
                size+=len(chunk)
                if size>max_bytes or time.monotonic()-start>25:raise TimeoutError('budget')
                chunks.append(chunk)
            raw=b''.join(chunks);receipt['http_status']=response.status_code
        (folder/'response.bin').write_bytes(raw)
        receipt.update(captured_at=now(),raw_path=str(folder/'response.bin'),sha256=digest(folder/'response.bin'))
        if receipt['http_status']!=200:raise ValueError('http_status_not_200')
        data=json.loads(raw) if parse_json else raw.decode('utf-8')
        receipt.update(status='success',captured_at=now(),raw_path=str(folder/'response.bin'),sha256=digest(folder/'response.bin'))
    except Exception as exc:
        if isinstance(exc,urllib.error.HTTPError):
            (folder/'response.bin').write_bytes(exc.read(max_bytes));receipt['http_status']=exc.code
        receipt.update(status='unavailable',error_type=type(exc).__name__)
        data=None
    save(folder/'receipt.json',receipt)
    return data,receipt


def normalize_schedule(data, receipt):
    if not data or receipt['status']!='success':return []
    records=[]
    for day in data.get('leagueSchedule',{}).get('gameDates',[]):
        for g in day.get('games',[]):
            if g.get('gameStatus')!=1 or not str(g.get('gameId','')).startswith('00226'):continue
            records.append({'game_id':g['gameId'],'home_team_id':int(g['homeTeam']['teamId']),
                'away_team_id':int(g['awayTeam']['teamId']),'home_team':g['homeTeam']['teamName'],
                'away_team':g['awayTeam']['teamName'],'scheduled_tipoff':g['gameDateTimeUTC'],
                'status':'scheduled','season_start':2026,'is_playoffs':0,'source':receipt})
    if len({g['game_id'] for g in records})!=len(records):raise ValueError('duplicate schedule ID')
    return records


def probe():
    folder=new_run('source_probe')
    schedule,receipt=get('https://cdn.nba.com/static/json/staticData/scheduleLeagueV2_1.json')
    games=normalize_schedule(schedule,receipt)
    save(folder/'schedule.json',games)
    results={'schedule':receipt,'scheduled_game_count':len(games)}
    board,br=get('https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates=20260113')
    results['espn_scoreboard']=br
    if board:
        for event in board.get('events',[])[:2]:
            eid=event['id']
            odds,orr=get(f'https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba/events/{eid}/competitions/{eid}/odds')
            results.setdefault('odds_probes',[]).append(orr)
            if odds:save(folder/f'odds_{eid}.json',odds)
    # No batch collection until real identity/provider/time fields are validated.
    results['market_batch_status']='not_started_pending_eligible_small_sample'
    results['historical_seasons_requested']=['2023-24','2024-25','2025-26']
    save(folder/'result.json',results)
    return folder


if __name__=='__main__':print(probe())
