"""Dataset contract validation; no semantic LLM or production gate changes."""
import re
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
from evaluation.research_shadow.core import stamp,bounds

def qualify_article(meta,cutoff):
    cut=stamp(cutoff);_,pub=bounds(meta.get('published_at'));_,mod=bounds(meta.get('updated_at'));reasons=[]
    if not meta.get('source_timestamp_verified'):reasons.append('publication_origin_unverified')
    if not pub:reasons.append('publication_unavailable')
    elif pub>cut:reasons.append('publication_precision_insufficient'if stamp(meta.get('published_at'))is None else'published_after_cutoff')
    if mod and mod>cut:reasons.append('after_cutoff_modification_unresolved')
    if meta.get('source_tier')not in {'TIER_A','TIER_B'}:reasons.append('unqualified_source')
    if not meta.get('is_pregame'):reasons.append('pregame_identity_or_genre_unverified')
    eligible=not reasons
    return {'temporal_grade':'TEMPORAL_B'if eligible else'TEMPORAL_C','research_fact_eligible':eligible,
            'hard_fact_eligible':False,'confidence':'original_pregame_publication_not_materially_updated'if eligible else'discovery_only',
            'cutoff_pass':eligible,'reasons':reasons,'historical_capture_required':False,'not_strict_as_was_replay':True}

def qualify_market(row,cutoff):
    q=stamp(row.get('quote_timestamp'));cut=stamp(cutoff);reasons=[]
    if not q:reasons.append('quote_timestamp_unavailable')
    elif q>cut:reasons.append('quote_after_cutoff')
    if not row.get('source')or not row.get('source_url'):reasons.append('source_provenance_incomplete')
    if not row.get('identity_verified'):reasons.append('market_identity_unverified')
    return {'eligible':not reasons,'reasons':reasons,'latest_known_before_cutoff':not reasons,
            'line_age_at_cutoff_seconds':(cut-q).total_seconds()if q and q<=cut else None}

def qualify_lineup(row,cutoff):
    pub=stamp(row.get('published_at'));cut=stamp(cutoff);reasons=[]
    if row.get('basis')not in {'confirmed_before_cutoff','source_expected'}:reasons.append('invalid_lineup_basis')
    if not pub or pub>cut:reasons.append('lineup_publication_not_before_cutoff')
    if not row.get('source')or not row.get('source_url'):reasons.append('lineup_source_missing')
    players=row.get('players')or[]
    if len(players)!=5 or len(set(players))!=5:reasons.append('five_unique_players_required')
    if row.get('temporal_grade')not in {'TEMPORAL_A','TEMPORAL_B'}:reasons.append('lineup_time_proof_unavailable')
    if not row.get('identity_verified'):reasons.append('lineup_team_identity_unverified')
    return {'eligible':not reasons,'reasons':reasons,'cutoff_pass':not reasons}

FORBIDDEN={'final_score','actual_winner','target_final_score','target_boxscore','target_box_score','actual_minutes','actual_starters','postgame','ats_settlement','total_settlement','ground_truth','hidden_outcome_bucket'}
REQUIRED={'metadata','matchup','cutoff','team_records','recent_form','previous_season_metrics','head_to_head','schedule_context','player_history','roster','injury','starting_lineup','market','research','known_missing_information','provenance'}
ENVELOPE={'value','source','event_time','published_at','captured_at','temporal_grade','confidence'}
def validate_context(ctx):
    errors=[];checks={};cut=stamp((ctx.get('cutoff')or{}).get('value'));identity=(ctx.get('matchup')or{}).get('value')or{}
    tip=stamp(identity.get('scheduled_tipoff'));target=(ctx.get('metadata')or{}).get('game_id')
    if not REQUIRED<=ctx.keys():errors.append('missing_required_groups')
    if not cut or not tip or tip-cut!=timedelta(minutes=5):errors.append('cutoff_not_t_minus_five')
    targetday=tip.astimezone(ZoneInfo('America/New_York')).date().isoformat()if tip else None
    def walk(v,path=''):
        if isinstance(v,dict):
            for k,x in v.items():
                if k.casefold()in FORBIDDEN:errors.append('forbidden_target_field:'+path+'/'+k)
                walk(x,path+'/'+k)
        elif isinstance(v,list):
            for i,x in enumerate(v):walk(x,path+'/'+str(i))
    walk(ctx)
    records=ctx.get('provenance',{}).get('event_records',[])
    for e in records:
        if e.get('game_id')==target:errors.append('target_game_in_event_derived_inputs')
        if not e.get('event_date')or not targetday or e['event_date']>=targetday:errors.append('future_or_same_day_unproven_event')
        if e.get('event_time')and stamp(e['event_time'])and cut and stamp(e['event_time'])>=cut:errors.append('event_after_cutoff')
        if not e.get('source_ref'):errors.append('event_source_provenance_missing')
    for group in REQUIRED-{'metadata','known_missing_information','provenance'}:
        value=ctx.get(group)
        if isinstance(value,dict)and 'value'in value and not ENVELOPE<=value.keys():errors.append('incomplete_envelope:'+group)
    injury=(ctx.get('injury')or{}).get('value')or{}
    if injury.get('report_time')and(not stamp(injury['report_time'])or not cut or stamp(injury['report_time'])>cut):errors.append('injury_report_after_cutoff')
    market=(ctx.get('market')or{}).get('value')or{}
    if market.get('quote')and not qualify_market(market['quote'],cut.isoformat())['eligible']:errors.append('market_quote_not_qualified')
    lineups=(ctx.get('starting_lineup')or{}).get('value')or{}
    for row in lineups.get('teams',[]):
        if row.get('basis')in {'confirmed_before_cutoff','source_expected'}and not qualify_lineup(row,cut.isoformat())['eligible']:errors.append('lineup_not_qualified')
    research=(ctx.get('research')or{}).get('value')or{}
    for row in research.get('evidence',[]):
        if row.get('temporal_grade')not in {'TEMPORAL_A','TEMPORAL_B'}:errors.append('research_lead_entered_facts')
        _,pub=bounds(row.get('published_at'))
        if not pub or not cut or pub>cut:errors.append('research_timestamp_after_cutoff')
        if not row.get('source_url')or not row.get('document_hash'):errors.append('research_source_provenance_missing')
    checks.update(no_target_result=not any('forbidden_target_field'in e for e in errors),target_excluded_from_rolling=not any('target_game'in e for e in errors),
                  events_strictly_pre_target=not any('event_'in e for e in errors),no_model_calls=ctx.get('metadata',{}).get('model_calls',0)==0)
    return {'passed':not errors,'errors':list(dict.fromkeys(errors)),'checks':checks,'event_records_checked':len(records),
            'semantic_sentence_certification':False,'policy':'historical_t5_archive_reconstruction_v1_not_production_strict_capture_replay'}
