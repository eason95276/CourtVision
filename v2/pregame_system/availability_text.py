"""Bind explicit availability wording to its local subject, never an entire paragraph."""
from __future__ import annotations

import re


STATUS = re.compile(
    r"缺阵|缺席|无法出战|不会出场|出战成疑|出战|可打|获准|首发|分钟限制|限时|"
    r"限制.{0,5}\d{1,2}\s*分钟|\bavailable\b|\bquestionable\b|\bdoubtful\b|"
    r"\bprobable\b|\binactive\b|\bdnp\b|\bout\b(?!\s+of\b)|"
    r"cleared to play|can play|minutes? restriction|limited to|capped at", re.I)
JOIN = re.compile(r"^[\s、，,&/和与及或]+$|^(?:\s*(?:and|or)\s*)+$", re.I)


def player_aliases(projection):
    names = set()
    for fact in projection.get('facts', []):
        value = fact.get('value') or {}
        if value.get('player_name'):
            names.add(value['player_name'])
        for key in ('roster_snapshot', 'players', 'confirmed_starters'):
            for item in value.get(key) or []:
                name = (item.get('name') or item.get('player_name') or item.get('player')) if isinstance(item, dict) else item
                if isinstance(name, str) and name.strip():
                    names.add(name.strip())
    names.update(row['canonical_name_en'] for row in projection.get('player_display_names') or []
                 if row.get('canonical_name_en'))
    owners = {}
    def add(alias, name):
        if alias and len(alias) >= 2:
            owners.setdefault(alias.casefold(), set()).add(name)
    for name in names:
        add(name, name)
        if ' ' in name:
            add(name.split()[-1], name)
    for row in projection.get('player_display_names') or []:
        name, zh = row.get('canonical_name_en'), row.get('display_name_zh')
        if name and zh:
            add(zh, name)
            add(zh.split('·')[-1], name)
    return {alias: next(iter(values)) for alias, values in owners.items() if len(values) == 1}


def people(text, aliases):
    found = []
    for alias, name in aliases.items():
        pattern = re.escape(alias)
        if re.search('[a-z]', alias, re.I):
            pattern = r'(?<![A-Za-z])' + pattern + r'(?![A-Za-z])'
        found.extend((m.start(), m.end(), name) for m in re.finditer(pattern, text, re.I))
    result = []
    for item in sorted(found, key=lambda x: (x[0], -x[1])):
        if not any(item[0] < old[1] and item[1] > old[0] for old in result):
            result.append(item)
    return result


def _names_only(text, mentions):
    for start, end, _ in reversed(mentions):
        text = text[:start] + ' ' + text[end:]
    return bool(mentions) and not re.sub(r'\band\b|\bor\b|[\s、，,&/和与及或]', '', text, flags=re.I)


def claim_scopes(text, projection, implicit_player=None):
    aliases = player_aliases(projection)
    implicit = aliases.get(str(implicit_player or '').casefold())
    for sentence in re.split(r'[。！？；;!?]', text):
        pending = ''
        previous = implicit
        for clause in re.split(r'[：:，,]|(?<=缺阵)、|\b(?:while|whereas|but)\b|但是|但|而(?!非)|并且|且', sentence, flags=re.I):
            if not clause.strip():
                continue
            mentions = people(clause, aliases)
            if _names_only(clause, mentions):
                pending += clause + '、'
                continue
            clause = pending + clause
            pending = ''
            mentions = people(clause, aliases)
            marker = STATUS.search(clause)
            if marker is None:
                if len(mentions) == 1:
                    previous = mentions[0][2]
                yield clause, [], False
                continue
            before = [m for m in mentions if m[1] <= marker.start()]
            bound = before[-1:]
            for item in reversed(before[:-1]):
                if JOIN.fullmatch(clause[item[1]:bound[0][0]]):
                    bound.insert(0, item)
                else:
                    break
            if not bound:
                after = [m for m in mentions if m[0] >= marker.end()]
                if after and not clause[marker.end():after[0][0]].strip(' ：:、'):
                    bound = after[:1]
                    for item in after[1:]:
                        if JOIN.fullmatch(clause[bound[-1][1]:item[0]]):
                            bound.append(item)
                        else:
                            break
            names = list(dict.fromkeys(m[2] for m in bound))
            if not names and not mentions and previous and re.match(
                    r'^(?:他|其|本人|已确认|已经确认|确认|正式确认|可以出战|获准|受限|限制|限时|本场限时|有分钟限制)', clause.strip()):
                names = [previous]
            if len(names) == 1:
                previous = names[0]
            yield clause, names, True
        if pending:
            yield pending.rstrip('、'), [], False


def supported_team_absence(text, projection, side):
    if side not in ('home', 'away') or not re.search(r'多名|多人|一长串|很长.{0,5}缺阵名单|several|multiple', text, re.I):
        return False
    roster = set()
    for fact in projection.get('facts', []):
        value = fact.get('value') or {}
        if fact.get('kind') == 'CURRENT' and value.get('side') == side:
            roster.update((p.get('name') or p.get('player_name') or '').casefold()
                          for p in value.get('roster_snapshot') or [] if isinstance(p, dict))
    out = set()
    for fact in projection.get('facts', []):
        value = fact.get('value') or {}
        name = str(value.get('player_name') or '').casefold()
        if (name and fact.get('kind') == 'LIVE' and fact.get('game_specific')
                and (name in roster or value.get('side') == side)
                and str(value.get('status') or '').casefold() in {'out', 'inactive', 'suspended', 'dnp'}):
            out.add(name)
    return len(out) >= (3 if re.search(r'一长串|很长', text) else 2)
