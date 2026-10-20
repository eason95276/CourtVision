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


def test_length_and_unobserved_sequences_are_bounded():
    assert any(i['code']=='article_too_long'for i in validate_article({'article':'字'*901},context()))
    assert validate_article({'article':'第三节活塞得到37分。'},context())==[]
    assert any(i['code']=='sequence_claim_without_play_by_play'for i in validate_article({'article':'三分集中在需要回应的时段。'},context()))


def test_final_score_cannot_be_borrowed_from_style_reference():
    value=context();value['game'].update(home_score=121,away_score=113)
    assert validate_article({'article':'活塞121比113拿下比赛。'},value)==[]
    assert any(i['code']=='final_score_mismatch'for i in validate_article({'article':'活塞128比110拿下比赛。'},value))


def test_away_forecast_probability_and_actual_winner_are_separate():
    from .editorial import _public_summary,_winner_probability
    value=context();value['game'].update(home_score=121,away_score=113,actual_home_margin=8)
    value['game']['home']['team_id']=1;value['game']['away']['team_id']=2
    value['prediction'].update(winner_team_id=2,win_probability=.44,expected_home_margin=-2.5)
    assert abs(_winner_probability(value)-.56)<1e-10
    text=_public_summary(value)['prediction_summary']
    assert '胜率56%'in text and '赛前看好凯尔特人'in text and '赛果活塞赢8分'in text
