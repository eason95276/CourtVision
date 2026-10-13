"""In-memory attacks only; immutable dataset fixtures are read, never rewritten."""
import copy,json,sys,importlib.util,inspect
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL,read_json,canonical_hash
from validate_closed_book_context import values
from closed_book_anonymize import anonymize
from build_eval_baseline import save

def originals():
    p=EVAL/'adversarial/originals/validate_closed_book_context.py';spec=importlib.util.spec_from_file_location('old_validate',p);mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod);return mod.validate

def base():
    row=json.loads((EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()[0])
    return read_json(EVAL/row['context_path']),read_json(EVAL/row['provenance_path'])

def rebind(c,p):
    for key,proof in p['fields'].items():proof['field_value_sha256']=canonical_hash(values(c,key))
    return c,p

def cases():
    result=[];c,p=base()
    def add(name,modify,bound=False):
        cc,pp=copy.deepcopy(c),copy.deepcopy(p);modify(cc,pp);rebind(cc,pp)
        result.append({'name':name,'context':cc,'provenance':pp,'trusted_hash_required':bound,'expected_hash':canonical_hash(c)})
    for key in ('final_score','winner','actual_margin','actual_total','target_boxscore','target_minutes','target_dnp','target_starters','ground_truth','hidden_labels'):
        add('root_'+key,lambda a,b,k=key:a.update({k:123}))
    add('target_recent_game',lambda a,b:a['team_a']['recent_form']['recent_5']['games'][0].update(game_ref=b['target_game_id']))
    for date in ('2099-01-01',p['target_event_time']):add('future_or_same_day_'+date,lambda a,b,d=date:a['team_a']['recent_form']['recent_5']['games'][0].update(event_date=d))
    add('next_game_provenance',lambda a,b:b['fields']['player_history']['source_events'][0].update(event_time='2099-01-01'))
    add('false_max_time',lambda a,b:b['fields']['h2h'].update(max_source_event_time='2099-01-01'))
    extras={'future_roster':'Current 2099 roster','target_box_score':{'points':123},'future_team_record':{'wins':99},'availability':{'status':'out'},
        'confirmed_lineup':['unknown'],'debug':{'answer':123},'postgame_news':'already won','source_path':'../closed_book_ground_truth/answer.json',
        'environment_variable':'GROUND_TRUTH_PATH','manifest_reference':'closed-book-freeze-manifest.json','rescheduled_tipoff':'2099-01-01',
        'status':'cancelled','source_calendar':'UTC','trade_after_target':'Player changed teams','current_team_identity':'future membership'}
    for key,value in extras.items():add('nested_'+key,lambda a,b,k=key,v=value:a['team_a'].update({k:v}))
    add('nested_target_id_metadata',lambda a,b:a['team_b'].update({'metadata':{'target_game_id':b['target_game_id']}}))
    add('hidden_relative_reference',lambda a,b:a['team_a']['record'].update({'pointer':'../../closed_book_ground_truth/answer.json'}))
    add('nested_history_unknown_winner',lambda a,b:a['team_a']['recent_form']['recent_5']['games'][0].update(winner='home'))
    add('cup_kind_spoof',lambda a,b:a['matchup'].update(game_type='unknown_cup_final'),True)
    add('swap_team_ids',lambda a,b:a['team_a'].update(team_id=a['team_b']['team_id']),True)
    add('target_player_minutes_numeric',lambda a,b:a['team_a']['player_history']['players'][0].update(minutes_per_appearance=9999),True)
    add('unshifted_window_numeric',lambda a,b:a['team_a']['recent_form']['recent_5'].update(points_for_per_game=9999),True)
    add('future_season_aggregate_numeric',lambda a,b:a['team_a']['previous_season'].update(points_for_per_game=9999),True)
    add('rest_off_by_one_numeric',lambda a,b:a['schedule_context']['team_a'].update(days_rest=9999),True)
    add('record_count_mismatch',lambda a,b:a['team_a']['record'].update(sample_games=9999))
    add('window_overflow',lambda a,b:a['team_b']['recent_form']['recent_5']['games'].extend(copy.deepcopy(a['team_b']['recent_form']['recent_5']['games'])))
    add('doubleheader_duplicate_ref',lambda a,b:a['team_a']['recent_form']['recent_10']['games'].append(copy.deepcopy(a['team_a']['recent_form']['recent_10']['games'][0])))
    add('invalid_calendar_date',lambda a,b:a['team_b']['recent_form']['recent_5']['games'][0].update(event_date='2025-99-99'))
    add('utc_timestamp_inside_date_field',lambda a,b:a['team_a']['recent_form']['recent_5']['games'][0].update(event_date='2025-01-01T23:59:00Z'))
    add('missing_provenance_group',lambda a,b:b['fields'].pop('player_history'))
    return result

def run(old=False):
    if old:validator=originals()
    else:
        from validate_closed_book_context import validate
        validator=validate
    outputs=[]
    for case in cases():
        kwargs={}
        if case['trusted_hash_required'] and 'expected_context_hash' in inspect.signature(validator).parameters:kwargs['expected_context_hash']=case['expected_hash']
        try:issues=validator(case['context'],case['provenance'],**kwargs);rejected=bool(issues);crash=None
        except Exception as error:issues=[];rejected=False;crash=type(error).__name__+':'+str(error)[:150]
        outputs.append({'name':case['name'],'rejected':rejected,'issues':issues,'crash':crash,'requires_trusted_hash':case['trusted_hash_required']})
    save(EVAL/'reports'/('temporal-attacks-before.json' if old else 'temporal-attacks-after.json'),outputs)
    print(json.dumps({'scenarios':len(outputs),'rejected':sum(r['rejected'] for r in outputs),'crashed':sum(bool(r['crash']) for r in outputs),'accepted_gaps':[r['name'] for r in outputs if not r['rejected']]},ensure_ascii=False))
    return outputs

if __name__=='__main__':run('--before' in sys.argv)
