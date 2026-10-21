from .article_contract import validate_article,public_verdicts


def context():
    return {'game':{'home':{'name':'活塞'},'away':{'name':'凯尔特人'}},
        'prediction':{'winner_correct':False,'absolute_total_error':11.5},
        'public_market_reference_review':{'quote':{'home_spread':1.5,'away_spread':-1.5},
            'spread':{'lean_side':'away','lean_line':-1.5,'settlement':'loss'},'total':{'lean':'over','settlement':'win'}},
        'key_players':[{'name':'保罗·乔治','points':38}]}


def test_program_owns_market_sign_and_selected_direction():
    result=public_verdicts(context())
    assert result['market_lines']==['活塞受让1.5分','凯尔特人让1.5分']
    assert result['selected_spread']=='凯尔特人让1.5分'and result['spread_result']=='让分未命中'
    assert validate_article({'article':'活塞受让1.5分，凯尔特人让1.5分。'},context())==[]
    assert any(i['code']=='spread_side_or_sign_mismatch'for i in validate_article({'article':'市场活塞让1.5分。'},context()))
    assert any(i['code']=='selected_spread_direction_mismatch'for i in validate_article({'article':'我选择活塞受让1.5分。'},context()))
    assert any(i['code']=='selected_total_direction_mismatch'for i in validate_article({'article':'我选小分。'},context()))


def test_full_game_player_points_are_not_partial_game_evidence():
    assert validate_article({'article':'乔治38分。'},context())==[]
    assert any(i['code']=='player_points_mismatch'for i in validate_article({'article':'乔治39分。'},context()))
    assert any(i['code']=='player_period_claim_without_period_data'for i in validate_article({'article':'乔治半场就打出高效得分。'},context()))


def test_period_shooting_uses_only_the_matching_players_verified_period():
    value=context();value['player_period_stats']=[{'name':'保罗·乔治','period':4,'points':2,'fgm':1,'fga':5,'assists':2}]
    assert validate_article({'article':'乔治末节5投1中。'},value)==[]
    assert any(i['code']=='player_shooting_mismatch'for i in validate_article({'article':'乔治末节5投2中。'},value))
    assert any(i['code']=='player_period_claim_without_period_data'for i in validate_article({'article':'乔治上半场就打出高效得分。'},value))
    assert validate_article({'article':'末节之王乔治全场38分。'},value)==[]


def test_single_recorded_assist_is_not_a_claim_about_period_totals():
    value=context();value['verified_game_events']=[{'period':3,'text':'队友 makes three point shot (保罗·乔治 assists)'}]
    assert validate_article({'article':'第三节最后一球，乔治送出助攻。'},value)==[]
    assert any(i['code']=='player_period_claim_without_period_data'for i in validate_article({'article':'乔治第三节拿到8分。'},value))


def test_fan_quotes_require_actual_source_and_cannot_claim_consensus():
    value=context()
    assert any(i['code']=='fan_reaction_without_source'for i in validate_article({'article':'虎扑球迷说这场有意思。'},value))
    value['fan_discussion']={'status':'available','threads':[{'comments':[{'text':'这场惊喜了，赛前没觉得能赢。'}]}]}
    assert validate_article({'article':'虎扑热评里的“这场惊喜了”，说出了结果的反差。'},value)==[]
    assert any(i['code']=='fan_quote_not_in_source'for i in validate_article({'article':'虎扑热评说“全靠裁判保送”。'},value))
    assert any(i['code']=='fan_consensus_overclaim'for i in validate_article({'article':'所有球迷都认为这场赢得轻松。'},value))


def test_length_and_unobserved_sequences_are_bounded():
    assert any(i['code']=='article_too_long'for i in validate_article({'article':'字'*901},context()))
    assert validate_article({'article':'第三节活塞得到37分。'},context())==[]
    assert any(i['code']=='sequence_claim_without_play_by_play'for i in validate_article({'article':'三分集中在需要回应的时段。'},context()))


def test_final_score_cannot_be_borrowed_from_style_reference():
    value=context();value['game'].update(home_score=121,away_score=113)
    assert validate_article({'article':'活塞121比113拿下比赛。'},value)==[]
    assert any(i['code']=='final_score_mismatch'for i in validate_article({'article':'活塞128比110拿下比赛。'},value))


def test_total_direction_hit_does_not_reverse_numeric_error_direction():
    value=context();value['game']['actual_total']=206;value['prediction']['expected_total']=228.5
    assert public_verdicts(value)['total_prediction_relation']=='总分预测高于实际22.5分'
    assert any(i['code']=='total_prediction_error_direction_mismatch'for i in
        validate_article({'article':'小分命中，但总分被大幅低估。'},value))
    assert validate_article({'article':'小分命中，但总分预测高于实际。'},value)==[]


def test_away_forecast_probability_and_actual_winner_are_separate():
    from .editorial import _public_summary,_winner_probability
    value=context();value['game'].update(home_score=121,away_score=113,actual_home_margin=8)
    value['game']['home']['team_id']=1;value['game']['away']['team_id']=2
    value['prediction'].update(winner_team_id=2,win_probability=.44,expected_home_margin=-2.5)
    assert abs(_winner_probability(value)-.56)<1e-10
    text=_public_summary(value)['prediction_summary']
    assert '胜率56%'in text and '赛前看好凯尔特人'in text and '赛果活塞赢8分'in text
