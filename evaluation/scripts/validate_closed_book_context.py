"""Deterministic leakage validation, not a basketball quality rubric."""
import argparse,json,re
from pathlib import Path
from asset_scanner import canonical_hash,read_json
from closed_book_anonymize import identity_leaks
from context_shape import shape_issues

FIELD_KEYS={'team_record':'record','recent_form':'recent_form','previous_season_metrics':'previous_season',
    'player_history':'player_history','rotation_candidates':'recent_rotation_candidates'}

def values(context,field):
    if field in FIELD_KEYS:return {side:context[side][FIELD_KEYS[field]] for side in ('team_a','team_b')}
    return context['head_to_head'] if field=='h2h' else context['schedule_context']

def validate(context,provenance,mapping=None,context_path=None,truth_path=None,expected_context_hash=None):
    issues=[];target=provenance['target_game_id'];day=provenance['target_event_time']
    allowed={'matchup','team_a','team_b','head_to_head','schedule_context','market','injury','research','known_missing_information','task_mode'}
    if set(context)-allowed:issues.append('unexpected_model_context_root')
    issues.extend(shape_issues(context))
    if expected_context_hash is not None and canonical_hash(context)!=expected_context_hash:issues.append('trusted_context_hash_mismatch')
    required_proof=set(FIELD_KEYS)|{'h2h','rest'}
    if set(provenance.get('fields',{}))!=required_proof:issues.append('provenance_group_mismatch')
    if mapping is None:
        if context['matchup'].get('target_game_id')!=target:issues.append('target_identity_mismatch')
        if context['matchup'].get('game_date')!=day:issues.append('target_date_mismatch')
    if context['matchup'].get('home_team')!=context['team_a']['team_name'] or context['matchup'].get('away_team')!=context['team_b']['team_name']:issues.append('home_away_team_mismatch')
    if context['matchup'].get('team_a_role')!='home' or context['matchup'].get('team_b_role')!='away':issues.append('home_away_role_mismatch')
    banned={'final_score','winner','actual_margin','actual_total','actual_winner','target_boxscore','boxscore','target_minutes','target_dnp','target_starters',
        'ground_truth','expected_output','evaluation_answer','hidden_labels','postgame','seasonWins','seasonLosses','metadata','ground_truth_path','mapping_path','provenance_path'}
    def walk(v,path='',historical=False):
        if isinstance(v,dict):
            historical=historical or 'game_ref' in v or 'event_date' in v
            if v.get('game_ref')==target:issues.append('target_game_in_history:'+path)
            if 'event_date' in v and v['event_date']>=day:issues.append('future_or_target_event:'+path)
            if 'days_before_target' in v and (not isinstance(v['days_before_target'],(int,float)) or v['days_before_target']<=0):issues.append('nonpast_anonymous_event:'+path)
            for k,x in v.items():
                if k in banned:issues.append('forbidden_field:'+path+'/'+k)
                if k in {'home_score','away_score','home_points','away_points'} and not historical:issues.append('unbound_score:'+path+'/'+k)
                walk(x,path+'/'+k,historical)
        elif isinstance(v,list):
            for n,x in enumerate(v):walk(x,path+'/'+str(n),historical)
        elif isinstance(v,str):
            if re.search(r'closed_book_ground_truth|ground_truth_path|anonymized[/\\]mappings|https?://|file://',v):issues.append('hidden_or_external_reference:'+path)
            if re.match(r'^\d{4}-\d{2}-\d{2}',v) and v[:10]>day:issues.append('future_timestamp:'+path)
    walk(context)
    for field,proof in provenance['fields'].items():
        events=proof['source_events']
        for event in events:
            if event['game_id']==target:issues.append('target_source_event:'+field)
            if event['event_time']>=day:issues.append('future_source_event:'+field)
        maximum=max((e['event_time'] for e in events),default=None)
        if maximum!=proof['max_source_event_time']:issues.append('provenance_max_mismatch:'+field)
        if proof.get('future_rows_used')!=0 or proof.get('target_rows_used')!=0:issues.append('provenance_nonzero_leakage:'+field)
        if mapping is None and canonical_hash(values(context,field))!=proof['field_value_sha256']:issues.append('field_hash_mismatch:'+field)
    if mapping:
        issues.extend(identity_leaks(context,mapping))
        if canonical_hash(context)!=mapping['anonymous_context_sha256']:issues.append('anonymous_hash_mismatch')
    if context_path and truth_path:
        c=Path(context_path).resolve();t=Path(truth_path).resolve()
        if c==t or c.parent==t.parent:issues.append('ground_truth_not_physically_isolated')
        if any(p.is_symlink() for original in (Path(context_path),Path(truth_path)) for p in (original,*original.parents)):issues.append('symlink_boundary_unverified')
    return sorted(set(issues))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('context');p.add_argument('provenance');p.add_argument('--mapping');p.add_argument('--ground-truth');a=p.parse_args()
    issues=validate(read_json(a.context),read_json(a.provenance),read_json(a.mapping) if a.mapping else None,a.context,a.ground_truth)
    print(json.dumps({'passed':not issues,'issues':issues},ensure_ascii=False));raise SystemExit(bool(issues))
