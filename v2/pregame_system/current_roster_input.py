"""Current matchup roster snapshots, reusing the existing pregame roster cache."""
from __future__ import annotations
import argparse
import json
import re
from pathlib import Path
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor

from .official_web_roster import parse_league_rosters,embedded_props
from .team_roster import parse_rendered_table,parse_team_next_data,pack_roster
from .sources import get
from .storage import new_run,save,digest,now,instant

TARGET="espn:401902644"
TEAMS={1610612748:("heat","mia","Miami Heat"),1610612761:("raptors","tor","Toronto Raptors")}
VERSION="current-roster-input-v1"


def verify_roster_component(js):
    binding=re.search(r"rosterData:([A-Za-z_$][\w$]*)",js)
    if not binding:raise ValueError("roster_component_binding_unverified")
    data=binding.group(1)
    assigned=re.search(r"([A-Za-z_$][\w$]*)="+re.escape(data)+r"\.roster(?:[,;])",js)
    if not assigned or not re.search(r"players:"+re.escape(assigned.group(1))+r"[,}]",js):
        raise ValueError("roster_component_membership_unverified")
    if re.search(re.escape(data)+r"\.roster\.(?:filter|slice)",js):
        raise ValueError("roster_component_filters_require_explicit_support")
    return True


class ESPNRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.table=0;self.active=None;self.players=[];self.title=[];self.in_title=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=="table":self.table+=1
        if tag=="title":self.in_title=True
        if tag=="a" and self.table:
            match=re.search(r"/nba/player/_/id/(\d+)",a.get("href",""))
            if match:self.active={"source_player_id":match.group(1),"name":[],"url":a["href"]}
    def handle_data(self,data):
        if self.active is not None:self.active["name"].append(data)
        if self.in_title:self.title.append(data)
    def handle_endtag(self,tag):
        if tag=="table":self.table=max(0,self.table-1)
        if tag=="title":self.in_title=False
        if tag=="a" and self.active is not None:
            self.active["name"]=" ".join("".join(self.active["name"]).split())
            if self.active["name"]:self.players.append(self.active)
            self.active=None


def parse_espn_roster(html,tid,season,source,*,expected_team_name=None):
    parser=ESPNRows();parser.feed(html)
    expected_team_name=expected_team_name or TEAMS.get(tid,(None,None,''))[2]
    if not expected_team_name or expected_team_name.casefold() not in " ".join(parser.title).casefold() or "roster" not in " ".join(parser.title).casefold():
        raise ValueError("espn_roster_team_scope_mismatch")
    unique={p["source_player_id"]:p for p in parser.players}
    if not unique:raise ValueError("espn_roster_no_rendered_members")
    roster=pack_roster(tid,season,[{"player_id":None,"name":p["name"],"team_id":tid,
        "source_player_id":p["source_player_id"],"source_id_namespace":"ESPN","raw_identity":p}
        for p in unique.values()],source,[],[],"current ESPN rendered roster links")
    roster["roster_basis"]="espn_current_team_roster"
    roster["source"]["source_type"]="espn_current_team_roster"
    return roster


def _espn_get(url):
    import requests
    root=new_run("network")
    receipt={"url":url,"retry_count":0}
    try:
        response=requests.get(url,headers={"User-Agent":"CourtVision-Research/2.0"},timeout=(5,12),allow_redirects=False)
        path=root/"response.bin";path.write_bytes(response.content)
        receipt.update(http_status=response.status_code,captured_at=now(),raw_path=str(path),sha256=digest(path))
        if response.status_code!=200:raise ValueError("espn_roster_http_status")
        receipt["status"]="success";body=response.text
    except Exception as exc:receipt.update(status="unavailable",error_type=type(exc).__name__,message=str(exc)[:500]);body=None
    save(root/"receipt.json",receipt);return body,receipt


def refresh_matchup_rosters(game,*,probe_root=None,fetcher=get):
    root=new_run("roster_context");season=int(game.get("season_start",2026))
    ids={int(game[side]["id"])for side in ("home","away")}
    from courtvision.data import TEAM_ID_TO_ABBR
    if len(ids)!=2 or not ids<=set(TEAM_ID_TO_ABBR):raise ValueError('current_roster_matchup_team_identity_invalid')
    receipts={}
    if probe_root:
        probe_root=Path(probe_root)
        for tid in ids:
            row=json.loads((probe_root/(str(tid)+".json")).read_text(encoding="utf-8"))
            receipts[row["source"]["url"]]=row["source"]
        receipt=json.loads((probe_root/"league_receipt.json").read_text(encoding="utf-8"))
        receipts[receipt["url"]]=receipt
    def fetch(url):
        if url in receipts:
            receipt=receipts[url]
            if receipt["status"]!="success" or digest(receipt["raw_path"])!=receipt["sha256"]:raise ValueError("roster_raw_hash_mismatch")
            return Path(receipt["raw_path"]).read_text(encoding="utf-8"),receipt
        return fetcher(url,parse_json=False)
    league,league_source=fetch("https://www.nba.com/players")
    from courtvision.data import TEAM_ID_TO_ABBR
    assignments=parse_league_rosters(league,league_source,TEAM_ID_TO_ABBR,season)if league else {}
    def one(tid):
        assignment=assignments.get(tid)or{"team_id":tid,"season_start":season,"players":[]}
        team=next(game[side]for side in ('home','away')if int(game[side]['id'])==tid)
        known=TEAMS.get(tid)
        slug=known[0]if known else next((p.get('raw_identity',{}).get('TEAM_SLUG')for p in assignment['players']if p.get('raw_identity',{}).get('TEAM_SLUG')),None)
        if slug and not re.fullmatch('[a-z][a-z0-9-]*',slug):raise ValueError('official_roster_slug_invalid')
        from .market import ALIASES
        abbr=known[1]if known else ALIASES.get(TEAM_ID_TO_ABBR[tid],TEAM_ID_TO_ABBR[tid])
        name=known[2]if known else next((p.get('team')for p in assignment['players']if p.get('team')),None)or team.get('name')or game.get('home_team'if team is game['home']else'away_team')
        url=f'https://www.nba.com/{slug}/roster'if slug else None
        html,source=fetch(url)if url else (None,{'status':'unavailable','reason':'official_team_slug_unavailable'})
        roster=None;failures=[]
        try:
            if html and source["status"]=="success":
                roster=parse_rendered_table(html,tid,assignment,assignments,source)
                if roster is None:
                    props=embedded_props(html)
                    if props["page"].get("contentExpanded") or props["page"].get("contentStructured"):
                        raise ValueError("team_roster_cms_override_requires_rendered_members")
                    paths=re.findall(r"/_teams/_next/[^\"<>\s]*roster-[a-z0-9]+\.js",html)
                    if not paths:raise ValueError("official_roster_component_missing")
                    js,client_source=fetch("https://www.nba.com"+paths[0])
                    if client_source["status"]!="success":raise ValueError("official_roster_component_unavailable")
                    verify_roster_component(js)
                    roster=parse_team_next_data(html,tid,assignment,source,client_source)
                if league:
                    roster["supporting_sources"].append(league_source)
        except (ValueError,KeyError,TypeError)as exc:failures.append({"url":url,"error":str(exc)});roster=None
        if roster is None:
            friendly=re.sub('[^a-z0-9]+','-',str(name or '').casefold()).strip('-')
            fallback=f"https://www.espn.com/nba/team/roster/_/name/{abbr}"+(f'/{friendly}'if friendly else '')
            html,source=_espn_get(fallback)
            if not html:raise ValueError("current_roster_all_sources_unavailable:"+str(tid))
            roster=parse_espn_roster(html,tid,season,source,expected_team_name=name)
        observed=roster["source"]["captured_at"]
        if instant(observed)>=instant(game["tipoff"]):raise ValueError("current_roster_observed_after_tipoff")
        roster.update(observed_at=observed,roster_snapshot_time=observed)
        roster["source"].update(observed_at=observed,temporal_basis="observed_at")
        record={"team_id":tid,"season_start":season,"captured_at":now(),"observed_at":observed,
            "official_team_roster":roster if roster["roster_basis"]=="official_team_roster"else None,
            "current_team_roster":roster if roster["roster_basis"]!="official_team_roster"else None,
            "roster_basis":roster["roster_basis"],"league_team_assignment":assignments.get(tid),
            "minutes":None,"confirmed_transactions":None,"attempts":failures,"parser_version":VERSION}
        save(root/"teams"/(str(tid)+".json"),record)
        return tid,roster,record
    with ThreadPoolExecutor(max_workers=2)as pool:
        fetched=list(pool.map(one,sorted(ids)))
    teams={tid:roster for tid,roster,_record in fetched}
    team_records={tid:record for tid,_roster,record in fetched}
    quickley=next((p for p in (teams.get(1610612761)or{}).get('players',[])if p['name']=='Immanuel Quickley'),None)
    result={"status":"passed"if all(t["players"]for t in teams.values())else"failed",
        "mode":"current_roster_smoke","target":game["id"],"root":str(root),"finished_at":now(),
        "teams":{str(tid):{"count":len(r["players"]),"source_url":r["source"]["url"],"source_type":r["source"]["source_type"],
            "observed_at":r["observed_at"],"team_id":r["team_id"],"roster_basis":r["roster_basis"],
            "raw_response_sha256":r["source"].get("sha256"),"parser_version":VERSION,
            "players":r["players"],"snapshot":team_records[tid]}for tid,r in teams.items()},
        "quickley":quickley,"current_identity_verified":all(t['players']for t in teams.values()),
        "boundaries":{"confirmed_starter":False,"confirmed_available":False,"numeric_adjustment":False}}
    save(root/"result.json",result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--probe-root",type=Path)
    args=parser.parse_args()
    from v2.frontend.prediction_version_service import PredictionVersionService
    game=PredictionVersionService().tasks.registered_game(TARGET)
    result=refresh_matchup_rosters(game,probe_root=args.probe_root)
    print(json.dumps({k:v for k,v in result.items()if k!="teams"},ensure_ascii=True,indent=2))
    print(json.dumps({tid:{k:v for k,v in row.items()if k!="players"}for tid,row in result["teams"].items()},ensure_ascii=True))
    return 0 if result["status"]=="passed"else 1

if __name__=="__main__":raise SystemExit(main())
