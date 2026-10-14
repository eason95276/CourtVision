"""Persistent Chinese display names; never a player identity/availability source."""
from __future__ import annotations
import json
import re
from collections import Counter
from v2.pregame_system.espn_boxscore_normalizer import normalize_identity_name
from v2.pregame_system.storage import now


KNOWN_BY_NAME={
 'Immanuel Quickley':'伊曼纽尔·奎克利','RJ Barrett':'RJ·巴雷特','Scottie Barnes':'斯科蒂·巴恩斯',
 'Jakob Poeltl':'雅各布·珀尔特尔','Kawhi Leonard':'科怀·伦纳德','Giannis Antetokounmpo':'扬尼斯·阿德托昆博',
 'Andrew Wiggins':'安德鲁·维金斯','Bam Adebayo':'巴姆·阿德巴约','Klay Thompson':'克莱·汤普森',
 'Davion Mitchell':'戴维恩·米切尔','Pelle Larsson':'佩莱·拉松','Nikola Jović':'尼古拉·约维奇',
 'Tim Hardaway Jr.':'蒂姆·哈达威二世','Bobby Portis Jr.':'博比·波蒂斯','Jamal Shead':'贾马尔·谢德',
 'Dru Smith':'德鲁·史密斯','Keshad Johnson':'凯沙德·约翰逊',"Ja'Kobe Walter":'贾科比·沃尔特',
 'LeBron James':'勒布朗·詹姆斯','Tyrese Maxey':'泰瑞斯·马克西','Joel Embiid':'乔尔·恩比德',
}


def transliterate(players):
    """One bounded batch, no web search, numbers or basketball assertions."""
    from v2.frontend.server import load_local_env
    from v2.agent.generate_historical_pregame_analysis import request_deepseek
    import os
    load_local_env()
    if not os.environ.get('DEEPSEEK_API_KEY'):raise ValueError('transliteration_key_missing')
    messages=[{'role':'system','content':'仅为中文NBA产品生成球员姓名音译。已有常用译名优先，否则自然音译。不得改变输入ID或英文姓名。只输出JSON对象names数组，每项含player_id、canonical_name_en、display_name_zh。不要输出球队、伤病或其他事实。'},
        {'role':'user','content':json.dumps({'players':players},ensure_ascii=False)}]
    from v2.pregame_system.storage import new_run,save
    root=new_run('player_name_translations');save(root/'request.json',{'messages':messages})
    from v2.pregame_system.cost_ledger import component
    with component('Display Names'):
        response=request_deepseek(messages,os.environ['DEEPSEEK_API_KEY'],'deepseek-v4-flash',3000,timeout_seconds=40)
    save(root/'response.json',{'raw':response.content,'usage':response.usage})
    raw=response.content.strip()
    if raw.startswith('```'):raw=re.sub(r'^```(?:json)?\s*|\s*```$','',raw)
    value=json.loads(raw)
    return value['names']


class PlayerDisplayNameStore:
    def __init__(self, versions):
        self.versions=versions
        with versions.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS player_display_names (
                player_id TEXT PRIMARY KEY,canonical_name_en TEXT NOT NULL,name_key TEXT NOT NULL,
                display_name_zh TEXT NOT NULL,translation_source TEXT NOT NULL,created_at TEXT NOT NULL)''')

    def ensure(self, players, *, translator=transliterate):
        from .display_data import PLAYER_NAMES_ZH_BY_ID
        missing=[];result=[]
        with self.versions.connect() as db:
            for p in players:
                name=p.get('name') or p.get('player_name') or p.get('full_name')
                if not name:continue
                key=normalize_identity_name(name)
                raw=p.get('player_id');namespace=p.get('id_namespace') or 'nba'
                ident=f'{namespace.lower()}:{raw}' if raw else 'name:'+key
                existing=db.execute('SELECT * FROM player_display_names WHERE player_id=?',(ident,)).fetchone()
                if existing:
                    if existing['name_key']!=key:raise ValueError('display_player_id_name_conflict')
                    result.append(dict(existing));continue
                sibling=db.execute('SELECT * FROM player_display_names WHERE name_key=?',(key,)).fetchone()
                known=KNOWN_BY_NAME.get(name) or (PLAYER_NAMES_ZH_BY_ID.get(str(raw)) if namespace.lower()=='nba' else None)
                prior=p.get('display_name_zh') or p.get('display_name')
                zh=known or (sibling['display_name_zh'] if sibling else prior if prior and re.search('[\u4e00-\u9fff]',prior) else None)
                source='known_mapping' if known else sibling['translation_source'] if sibling else 'existing_data'
                row={'player_id':ident,'canonical_name_en':name,'name_key':key,'display_name_zh':zh,'translation_source':source,'created_at':now()}
                if zh:
                    db.execute('INSERT OR IGNORE INTO player_display_names VALUES (?,?,?,?,?,?)',tuple(row.values()));result.append(row)
                else:missing.append(row)
        error=None
        if missing:
            groups={}
            for row in missing:groups.setdefault(row['name_key'],[]).append(row)
            requested={rows[0]['player_id']:rows[0]for rows in groups.values()}
            try:
                translated=translator([{k:r[k]for k in ('player_id','canonical_name_en')}for r in requested.values()])
                with self.versions.connect() as db:
                    for r in translated:
                        original=requested.get(r.get('player_id'))
                        zh=r.get('display_name_zh','')
                        if not original or r.get('canonical_name_en')!=original['canonical_name_en'] or not re.search('[\u4e00-\u9fff]',zh) or len(zh)>60:
                            raise ValueError('transliteration_identity_or_display_invalid')
                        for sibling in groups[original['name_key']]:
                            sibling.update(display_name_zh=zh,translation_source='llm_transliteration')
                            db.execute('INSERT OR IGNORE INTO player_display_names VALUES (?,?,?,?,?,?)',tuple(sibling.values()));result.append(sibling)
            except Exception as exc:error=type(exc).__name__
        return {'names':result,'missing':[r['canonical_name_en']for r in missing if not r.get('display_name_zh')],
            'translation_error':error,'numeric_model_usage':False}


def catalog(versions, players=None):
    with versions.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='player_display_names'").fetchone():return []
        rows=[dict(r)for r in db.execute('SELECT * FROM player_display_names')]
    if players is not None:
        keys={normalize_identity_name(p.get('name') or p.get('full_name') or p.get('player_name'))for p in players}
        rows=[r for r in rows if r['name_key']in keys]
    unique={r['name_key']:r for r in rows}
    return list(unique.values())


def display_aliases(names):
    mapping={r['canonical_name_en']:r['display_name_zh']for r in names}
    tokens=Counter(t for name in mapping for t in name.split() if len(t)>=5)
    for name,zh in list(mapping.items()):
        for part in name.split():
            if len(part)>=5 and tokens[part]==1:mapping[part]=zh
    return mapping


def translate_text(text, names):
    aliases=display_aliases(names)
    if not aliases:return text
    pattern=re.compile(r'(?<![A-Za-z])(?:'+'|'.join(re.escape(k)for k in sorted(aliases,key=len,reverse=True))+r')(?![A-Za-z])')
    return pattern.sub(lambda m:aliases[m.group()],text)
