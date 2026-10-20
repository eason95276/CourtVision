from .editorial_hard_facts import _availability_issues_for_text


def projection(status='out'):
    return {'game':{},'facts':[{'id':'live-harris','kind':'LIVE','game_specific':True,
        'value':{'player_name':'Tobias Harris','status':status,'claim_type':'availability'}}],
        'player_display_names':[{'canonical_name_en':'Tobias Harris','display_name_zh':'托拜厄斯·哈里斯'}]}


def test_chinese_short_name_binds_only_to_existing_live_identity():
    assert _availability_issues_for_text('哈里斯确认缺阵。',projection(),'article')==[]
    assert any(r['code']=='confirmed_availability_source_conflict'for r in
        _availability_issues_for_text('哈里斯确认缺阵。',projection('available'),'article'))
    value=projection();value['facts']=[]
    assert any(r['code']=='current_availability_unverified'for r in
        _availability_issues_for_text('哈里斯确认缺阵。',value,'article'))


def test_other_sentence_or_condition_cannot_supply_or_hide_status_claim():
    value=projection();value['facts'].append({'id':'another','kind':'CURRENT','value':{'roster_snapshot':[{'name':'Victor Wembanyama'}]}})
    value['player_display_names'].append({'canonical_name_en':'Victor Wembanyama','display_name_zh':'维克托·文班亚马'})
    assert _availability_issues_for_text('文班亚马负责护框。哈里斯确认缺阵。',value,'article')==[]
    assert _availability_issues_for_text('如果节奏很快，文班亚马需要回防。哈里斯确认缺阵。',projection('available'),'article')


def test_absence_of_out_report_is_not_a_positive_availability_assertion():
    value=projection('unknown')
    assert _availability_issues_for_text('哈里斯暂无缺阵报告。',value,'article')==[]
    assert _availability_issues_for_text('哈里斯暂无缺阵报告，因此确认出战。',projection('out'),'article')


def test_named_status_does_not_apply_to_other_matchup_players_in_same_sentence():
    value=projection()
    value['facts'].append({'id':'roster','kind':'CURRENT','value':{'roster_snapshot':[{'name':'DeAaron Fox'},{'name':'Cason Wallace'}]}})
    assert _availability_issues_for_text('DeAaron Fox 对上 Cason Wallace：哈里斯因伤缺阵期间，马刺更依赖后场创造。',value,'article')==[]
    assert _availability_issues_for_text('DeAaron Fox确认缺阵：哈里斯因伤缺阵期间，马刺更依赖后场创造。',value,'article')


def test_unknown_availability_cannot_be_published_as_full_strength():
    value=projection();value['limits']={'unconfirmed_player_availability':True}
    assert _availability_issues_for_text('雷霆带着完整核心来到客场。',value,'article')
    assert _availability_issues_for_text('不能据此说雷霆核心齐整。',value,'article')==[]
