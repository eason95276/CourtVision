"""Finite checks for reader-facing Postgame numbers and evidence boundaries."""
import re


def line_text(team,line):
    if line is None:return None
    return team+('受让'if line>0 else'让'if line<0 else'平手')+(f'{abs(line):g}分'if line else'')


def public_verdicts(context):
    teams=context['game'];market=context.get('public_market_reference_review')or{};quote=market.get('quote')or{}
    spread=market.get('spread')or{};total=market.get('total')or{}
    home=quote.get('home_spread');away=quote.get('away_spread')
    if away is None and home is not None:away=-home
    side=spread.get('lean_side');prediction=context.get('prediction')or{}
    result={'market_lines':[v for v in [line_text(teams['home']['name'],home),line_text(teams['away']['name'],away)]if v],
        'selected_spread':line_text(teams[side]['name'],spread.get('lean_line'))if side in {'home','away'}else'无明确让分方向',
        'selected_total':{'over':'大分','under':'小分','push':'无明确大小分方向'}.get(total.get('lean'),'无可靠市场参考'),
        'winner_result':'胜负看对'if prediction.get('winner_correct')else'胜负看错',
        'spread_result':{'win':'让分命中','loss':'让分未命中','push':'让分走盘'}.get(spread.get('settlement')),
        'total_result':{'win':'大小分命中','loss':'大小分未命中','push':'大小分走盘'}.get(total.get('settlement')),
        'total_absolute_error':prediction.get('absolute_total_error'),
        'margin_absolute_error':prediction.get('absolute_margin_error')}
    return result


def validate_article(editorial,context):
    issues=[];game=context['game'];market=context.get('public_market_reference_review')or{};quote=market.get('quote')or{}
    body=editorial.get('article','');whole='\n'.join(str(editorial.get(k,''))for k in ('title','short_review','article'))
    if len(re.sub(r'\s+','',body))>900:issues.append({'code':'article_too_long','limit':900})
    if re.search(r'\bavailability\b|payload|\bQA\b|hash',whole,re.I):issues.append({'code':'internal_audit_language'})
    for side in ('home','away'):
        team=game[side]['name'];line=quote.get(side+'_spread')
        if line is None and side=='away'and quote.get('home_spread')is not None:line=-quote['home_spread']
        for match in re.finditer(re.escape(team)+r'(?:队)?\s*(受让|让)\s*([+-]?\d+(?:\.\d+)?)\s*分',whole):
            stated=abs(float(match[2]))*(1 if match[1]=='受让'else-1)
            if line is None or abs(stated-line)>0.001:
                issues.append({'code':'spread_side_or_sign_mismatch','quote':match[0],
                    'expected':line_text(team,line)})
            prefix=re.split(r'[。；，,\n]',whole[:match.start()])[-1][-18:]
            selected=(market.get('spread')or{}).get('lean_side')
            if re.search(r'支持|选择|我选|我站',prefix)and selected in {'home','away'}and selected!=side:
                issues.append({'code':'selected_spread_direction_mismatch','quote':match[0]})
        score_pattern=re.escape(team)+r'.{0,6}?(\d{2,3})\s*(?:比|[-—–：:])\s*(\d{2,3})'
        for match in re.finditer(score_pattern,body):
            nearby=body[max(0,match.start()-12):min(len(body),match.end()+8)]
            if re.search(r'半场|中场|首节|次节|末节|第[一二三四1234]节|赛前|预计|预测|去年|上季|上赛季',nearby):continue
            expected=(game.get(side+'_score'),game.get(('away'if side=='home'else'home')+'_score'))
            if None not in expected and (int(match[1]),int(match[2]))!=expected:
                issues.append({'code':'final_score_mismatch','quote':match[0],'expected':expected})
    expected_total=(market.get('total')or{}).get('lean')
    for match in re.finditer(r'(?:我(?:赛前|当时)?(?:选|偏|倾向|支持)|赛前(?:选择|倾向)|总分(?:我)?(?:选择|倾向|偏))\s*(大分|小分)',whole):
        stated='over'if match[1]=='大分'else'under'
        if expected_total in {'over','under'}and stated!=expected_total:
            issues.append({'code':'selected_total_direction_mismatch','quote':match[0]})
    aliases={}
    for player in context.get('key_players')or[]:
        name=player.get('name','')
        for alias in {name,name.split('·')[-1]}:
            if len(alias)>=2:aliases.setdefault(alias,[]).append(player)
    clauses=re.split(r'[。！？；，,\n]',whole)
    for clause in clauses:
        names=[(alias,rows[0])for alias,rows in aliases.items()if len(rows)==1 and alias in clause]
        if names and not context.get('player_period_stats')and re.search(r'半场|首节|次节|末节|第[一二三四1234]节',clause)and re.search(r'得分|拿到|砍下|高效|命中|三分|篮板|助攻|连续|爆发',clause):
            issues.append({'code':'player_period_claim_without_period_data','quote':clause})
        for alias,row in names:
            match=re.search(re.escape(alias)+r'(?:全场|拿到|得到|贡献|砍下|交出)?\s*(\d+(?:\.\d+)?)\s*分',clause)
            if match and row.get('points')is not None and abs(float(match[1])-row['points'])>0.001:
                issues.append({'code':'player_points_mismatch','quote':match[0],'expected':row['points']})
        if not re.search(r'赛前|预期|预想|设想|如果|假如|若',clause)and re.search(
            r'集中在.{0,8}时段|(?:下半场|第三节|第四节).{0,16}(?:调整防守|加强对抗|改变防守)|连续.{0,8}(?:三分|得分潮)|错位.{0,8}正在兑现',clause):
            issues.append({'code':'sequence_claim_without_play_by_play','quote':clause})
    return issues
