"""Current-game identity is a prerequisite even for uncertain availability wording."""
from __future__ import annotations

import json
import re

from .availability_text import player_aliases


def lexical_names(projection, text=''):
    from v2.frontend.player_display_names import KNOWN_BY_NAME
    rows = [{'canonical_name_en': name, 'display_name_zh': zh} for name, zh in KNOWN_BY_NAME.items()]
    rows += projection.get('player_identity_lexicon') or []
    rows += projection.get('player_display_names') or []
    facts = projection.get('facts') or []
    blob = json.dumps(facts, ensure_ascii=False) + ' ' + text
    current = {p.get('name') or p.get('player_name') for f in facts if f.get('kind') == 'CURRENT'
               for p in (f.get('value') or {}).get('roster_snapshot') or []}
    relevant = set(current)
    for row in rows:
        name, zh = row.get('canonical_name_en'), row.get('display_name_zh')
        if name and (name.casefold() in blob.casefold() or (zh and zh in text)):
            relevant.add(name)
    # A translation is a naming aid, never a source of current affiliation.
    family = {}
    for row in rows:
        name, zh = row.get('canonical_name_en'), row.get('display_name_zh')
        if name and zh and '·' in zh:
            en = re.sub(r'\s+(?:Jr\.?|Sr\.?|II|III|IV)$', '', name, flags=re.I).split()[-1]
            family.setdefault(en.casefold(), set()).add(zh.split('·')[-1])
    result = {r['canonical_name_en']: r for r in rows if r.get('canonical_name_en') in relevant}
    for name in relevant:
        if not name: continue
        surname = re.sub(r'\s+(?:Jr\.?|Sr\.?|II|III|IV)$', '', name, flags=re.I).split()[-1].casefold()
        if name not in result and len(family.get(surname, ())) == 1:
            result[name] = {'canonical_name_en': name, 'display_name_zh': next(iter(family[surname]))}
    return list(result.values()), current


def identity_issues(text, projection, unit_id, implicit_player=None):
    rows, current = lexical_names(projection, text)
    if not current:
        return []  # Registered production preflight separately requires a verified roster.
    owners = {}
    for row in rows:
        name, zh = row.get('canonical_name_en'), row.get('display_name_zh')
        surname = re.sub(r'\s+(?:Jr\.?|Sr\.?|II|III|IV)$', '', name, flags=re.I).split()[-1] if name else None
        for alias in filter(None, (name, surname, zh, zh.split('·')[-1] if zh else None)):
            owners.setdefault(alias.casefold(), set()).add(name)
    # Include unique current English names even if no Chinese display name exists.
    for alias, name in player_aliases(projection).items():
        owners.setdefault(alias, set()).add(name)
    result = []
    historical = False
    for clause in re.split(r'[。！？；;，,]', text):
        if re.search(r'上赛季|去年|曾经|曾效力|离队后|转会后|last season|formerly|used to play', clause, re.I):
            historical = True
        if re.search(r'本场|今晚|现在|如今|today|tonight|this game', clause, re.I):
            historical = False
        if historical or re.search(r'(?:如果|假如).{0,25}(?:仍在|还在|还拥有)|if.{0,30}still played', clause, re.I):
            continue
        if not re.search(r'出战|缺阵|首发|上场|出场|分钟限制|轮换|本场.{0,20}核心|\b(?:available|out|starter|availability|lineup)\b', clause, re.I):
            continue
        matches = []
        for alias, candidates in owners.items():
            pattern = re.escape(alias)
            if re.search('[a-z]', alias, re.I): pattern = r'(?<![A-Za-z])' + pattern + r'(?![A-Za-z])'
            matches += [(m.start(), m.end(), alias, candidates) for m in re.finditer(pattern, clause, re.I)]
        picked = []
        for item in sorted(matches, key=lambda x: (x[0], -x[1])):
            if not any(item[0] < old[1] and item[1] > old[0] for old in picked): picked.append(item)
        teams={str((projection.get('game')or{}).get(side,{}).get('name')or'').casefold()for side in ('home','away')}
        for match in re.finditer(r"[A-Z][A-Za-z'’-]+(?:\s+[A-Z][A-Za-z'’-]+){1,2}|[\u4e00-\u9fff]{2,6}·[\u4e00-\u9fff]{2,8}",clause):
            value=match.group()
            if value.casefold()in teams or value in {'Expected Lineup','Starting Lineup','Injury Report'}:continue
            if not any(match.start()<old[1]and match.end()>old[0]for old in picked):
                picked.append((match.start(),match.end(),value,{value}))
        if not picked and implicit_player:
            picked = [(0, 0, implicit_player, {implicit_player})]
        for _, _, alias, candidates in picked:
            if len(candidates) != 1:
                result.append({'unit_id': unit_id, 'code': 'ambiguous_current_player_identity',
                    'quote': clause, 'alias': alias, 'candidates': sorted(candidates)})
            elif next(iter(candidates)) not in current:
                result.append({'unit_id': unit_id, 'code': 'current_game_player_identity_unverified',
                    'quote': clause, 'player': next(iter(candidates)),
                    'reason': 'not_in_frozen_current_matchup_rosters'})
    return result


def basketball_identity_projection(basketball):
    facts = []
    for item in basketball.get('evidence') or []:
        value = item.get('value') or {}
        facts.append({'kind': 'BACKGROUND', 'value': value})
        if item.get('section') == 'roster_context':
            for side, team in value.get('teams', {}).items():
                roster = team.get('official_team_roster') or team.get('current_team_roster') or team.get('roster') or {}
                if roster.get('status') in {'available', 'verified'}:
                    facts.append({'kind': 'CURRENT', 'value': {'side': side, 'roster_snapshot': roster.get('players') or []}})
    game = basketball.get('game') or {}
    return {'game': game, 'facts': facts, 'player_display_names': game.get('player_display_names') or [],
            'player_identity_lexicon': game.get('player_identity_lexicon') or []}


def validate_forecast_identity(candidate, basketball):
    projection = basketball_identity_projection(basketball)
    data = candidate.model_dump() if hasattr(candidate, 'model_dump') else candidate
    def visit(value, path='forecast'):
        if isinstance(value, dict):
            for key, child in value.items(): yield from visit(child, path+'.'+key)
        elif isinstance(value, list):
            for index, child in enumerate(value): yield from visit(child, path+'.'+str(index))
        elif isinstance(value, str):
            yield from identity_issues(value, projection, path)
    findings = list(visit(data))
    if findings:
        raise ValueError('forecast_current_player_identity:' + json.dumps(findings, ensure_ascii=False))
