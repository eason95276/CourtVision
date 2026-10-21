"""Small, reconciled player facts derived from the frozen play-by-play."""
import re


def build_player_focus(bundle, selected_names, display_name, translate):
    plays=bundle.get('play_by_play')or[]
    periods={r.get('period')for r in plays if 'End of the'in r.get('text','')}
    if not set(range(1,5)).issubset(periods):
        return {'player_period_stats':[],'verified_game_events':[],'status':'incomplete_play_by_play'}
    results=[];diagnostics=[]
    for player in bundle.get('player_boxscores')or[]:
        if player.get('participation_status')in {'did_not_play','inactive','unknown','not_played'}:
            continue
        name=player.get('full_name')or player.get('player_name')
        public=display_name(name)
        if public not in selected_names:continue
        stats={period:{'points':0,'fgm':0,'fga':0,'assists':0}for period in periods}
        for event in plays:
            period=event.get('period');text=event.get('text','')
            if period not in stats:continue
            row=stats[period]
            shot=re.match(re.escape(name)+r' (makes|misses) ',text)
            blocked=re.search(r' blocks '+re.escape(name)+r"\s*'s\b",text)
            if blocked and event.get('shootingPlay')is True:
                row['fga']+=1
            if shot:
                if 'free throw'not in text.lower() and event.get('shootingPlay')is True:
                    row['fga']+=1;row['fgm']+=int(shot[1]=='makes')
                if event.get('scoringPlay')is True:
                    row['points']+=event.get('scoreValue')or 0
            if event.get('scoringPlay')is True and re.search(r'\('+re.escape(name)+r' assists\)',text):
                row['assists']+=1
        expected={k:player.get(k)for k in ('points','fgm','fga','assists')}
        if any(expected[k]is None or sum(r[k]for r in stats.values())!=expected[k]for k in expected):
            diagnostics.append({'player':public,'status':'period_totals_do_not_match_boxscore',
                'observed':{k:sum(r[k]for r in stats.values())for k in expected},'expected':expected})
            continue
        for period,row in sorted(stats.items()):
            results.append({'name':public,'period':period,**row,
                'basis':'frozen play-by-play; reconciled against full-game boxscore'})
    events=[]
    for event in plays:
        if event.get('scoringPlay')is not True or event.get('scoreValue')not in (2,3):continue
        clock=(event.get('clock')or{}).get('displayValue','')
        try:
            parts=clock.split(':');seconds=float(parts[-1])+(60*int(parts[0])if len(parts)==2 else 0)
        except (ValueError,TypeError):continue
        period=event.get('period')
        if not ((period==3 and seconds<=60)or(period==4 and seconds<=300)):continue
        events.append({'event_id':event.get('id'),'period':period,'clock':clock,
            'text':translate(event.get('text','')),'home_score':event.get('scoreHome'),
            'away_score':event.get('scoreAway'),'basis':'frozen play-by-play event; not video observation'})
    return {'player_period_stats':results,'verified_game_events':events[-12:],
        'status':'available'if results else'unavailable','diagnostics':diagnostics}
