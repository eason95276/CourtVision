"""Pure pregame request boundary. No file tools, history or evaluator metadata."""
import json
from asset_scanner import canonical_hash
from validate_closed_book_context import validate,values,FIELD_KEYS

INSTRUCTION='Analyze this fixed basketball context only. Do not use outside knowledge, search, web or tools.'

def serialize_context(context,expected_hash=None):
    # This structural proof is not represented as historical source evidence.
    # Frozen-file loader supplies real authentication; pure fixtures can test shape.
    copied=json.loads(json.dumps(context,ensure_ascii=False,allow_nan=False))
    proof={'target_game_id':copied.get('matchup',{}).get('target_game_id'),'target_event_time':copied.get('matchup',{}).get('game_date'),'fields':{}}
    if not proof['target_event_time']:raise ValueError('anonymous input needs frozen loader validation, not unauthenticated serialization')
    for field in set(FIELD_KEYS)|{'h2h','rest'}:
        proof['fields'][field]={'source_events':[],'max_source_event_time':None,'future_rows_used':0,'target_rows_used':0,'field_value_sha256':canonical_hash(values(copied,field))}
    issues=validate(copied,proof,expected_context_hash=expected_hash)
    if issues:raise ValueError('Unsafe model-visible context: '+str(issues[:5]))
    return INSTRUCTION+'\n\n'+json.dumps(copied,ensure_ascii=False)

def frozen_request(sample_id,anonymous=False):
    from evaluation_integrity import load_model_context
    context=load_model_context(sample_id,anonymous)
    # Anonymous files have been byte-authenticated; no mapping/GT is loaded here.
    user=INSTRUCTION+'\n\n'+json.dumps(context,ensure_ascii=False) if anonymous else serialize_context(context,canonical_hash(context))
    return {'messages':[{'role':'user','content':user}],'tools':[],'cross_sample_history':False}
