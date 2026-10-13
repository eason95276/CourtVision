import pytest,json
from .common import *
from .data import CONTEXTS
from . import run

def test_final_context_integrity_and_all20():
    manifest=read(CONTEXTS/'final-manifest.json');assert manifest['n']==20
    for s in manifest['samples']:
        ctx=read(ROOT/s['context_path']);assert digest(ctx)==s['context_hash'];check_visible(ctx)
        assert ctx['matchup']['target_game_id']==s['game_id'] and ctx['cutoff']<ctx['matchup']['scheduled_tipoff']
def test_official_report_not_postgame():
    manifest=read(CONTEXTS/'final-manifest.json')
    for s in manifest['samples']:
        ctx=read(ROOT/s['context_path']);inj=ctx['injury']
        if inj['status']!='UNAVAILABLE':
            assert inj['report_time']<=ctx['cutoff'] and inj['source_url'].startswith('https://ak-static.cms.nba.com/')
            assert all(r['team'] in {ctx['matchup']['home_team'],ctx['matchup']['away_team']} for r in inj['records'])
def test_forecast_request_stateless_and_price_free():
    cfg=run.verify()
    for s in cfg['samples']:
        msg,_=run.messages('forecast',run.ctx_for(s));visible=json.loads(msg[1]['content'])
        assert 'market' not in visible['closed_book_context'];check_visible(visible)
        assert [m['role'] for m in msg]==['system','user']
def test_pinned_skills_and_final_candidate():assert run.verify()['system_revision']==REV
