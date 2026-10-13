import pytest
from v2.pregame_system.prediction_versions import PredictionVersionStore
from .player_display_names import PlayerDisplayNameStore,catalog,translate_text


def test_known_ids_and_unknown_transliteration_persist_without_changing_identity(tmp_path):
    store=PlayerDisplayNameStore(PredictionVersionStore(tmp_path/'names.sqlite'))
    players=[{'name':'Immanuel Quickley','player_id':'1630193'}, {'name':'New Player','player_id':'123','id_namespace':'espn'}]
    calls=[]
    def translator(rows):
        calls.append(rows);return [{**r,'display_name_zh':'纽·普莱尔'}for r in rows]
    first=store.ensure(players,translator=translator);second=store.ensure(players,translator=lambda _:pytest.fail('cached names must not regenerate'))
    assert len(calls)==1 and len(first['names'])==len(second['names'])==2
    assert first['names'][0]['translation_source']=='known_mapping'
    assert first['names'][1]['translation_source']=='llm_transliteration'
    assert first['names'][1]['player_id']=='espn:123' and players[1]['name']=='New Player'
    assert translate_text('Immanuel Quickley 与 New Player',first['names'])=='伊曼纽尔·奎克利 与 纽·普莱尔'


def test_failed_name_translation_is_nonblocking_and_not_saved_as_chinese(tmp_path):
    store=PlayerDisplayNameStore(PredictionVersionStore(tmp_path/'names.sqlite'))
    result=store.ensure([{'name':'Unknown Player'}],translator=lambda _: (_ for _ in ()).throw(RuntimeError('offline')))
    assert result['missing']==['Unknown Player'] and result['translation_error']=='RuntimeError'
    assert catalog(store.versions)==[]


def test_conflicting_provider_id_does_not_relabel_player(tmp_path):
    store=PlayerDisplayNameStore(PredictionVersionStore(tmp_path/'names.sqlite'))
    store.ensure([{'name':'Immanuel Quickley','player_id':'1630193'}])
    with pytest.raises(ValueError,match='id_name_conflict'):store.ensure([{'name':'Other Person','player_id':'1630193'}])
