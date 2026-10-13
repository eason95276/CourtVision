"""Bounded modern official PDF format recovery; preserves base context files."""
from .data import *

def main():
    manifest=read(CONTEXTS/'manifest.json');rows=[];lookup={s['sample_id']:s for s in samples()}
    if (CONTEXTS/'final-manifest.json').exists():return read(CONTEXTS/'final-manifest.json')
    for row in manifest['samples']:
        s=lookup[row['sample_id']];ctx=read(ROOT/row['context_path'])
        existing=CONTEXTS/s['sample_id']/'enriched-context.json'
        if existing.exists():
            enriched=read(existing)
            rows.append({**row,'context_hash':digest(enriched),'context_path':str(existing.relative_to(ROOT)),
                'injury_status':enriched['injury']['status'],'injury_rows':len(enriched['injury']['records']),
                'lineup_basis':[t['basis'] for t in enriched['starting_lineup']['teams']],'base_context_hash':row['context_hash']})
            continue
        if ctx['injury']['status']=='UNAVAILABLE':
            local=datetime.fromisoformat(s['cutoff_at']).astimezone(ET);floor=local.replace(minute=(local.minute//15)*15,second=0,microsecond=0)
            folder=RUNTIME/'modern-injury'/s['sample_id'];folder.mkdir(parents=True,exist_ok=True)
            selected=None
            for minutes in (0,15,30):
                d=floor-timedelta(minutes=minutes);url='https://ak-static.cms.nba.com/referee/injury/'+d.strftime('Injury-Report_%Y-%m-%d_%I_%M%p.pdf')
                p=folder/url.rsplit('/',1)[-1]
                if p.exists():t,records=official.parse(p,url);receipt={'captured_at':now()}
                else:
                    p,receipt=official.obtain(url,folder)
                    if not p:continue
                    t,records=official.parse(p,url)
                match=TRICODE[s['away_team']]+'@'+TRICODE[s['home_team']];clock=datetime.fromisoformat(s['scheduled_tipoff']).astimezone(ET).strftime('%I:%M')+'(ET)'
                records=[r for r in records if r['matchup']==match and r['game_time']==clock and r['game_date']==local.strftime('%m/%d/%Y') and r['team'] in {s['home_team'],s['away_team']}]
                if t.astimezone(timezone.utc)>datetime.fromisoformat(s['cutoff_at']) or not records:continue
                selected={'status':'COMPLETE' if len({r['team'] for r in records})==2 else 'PARTIAL','records':records,'report_time':t.astimezone(timezone.utc).isoformat(),
                   'source_url':url,'document_hash':digest(p.read_bytes()),'source_path':str(p),'captured_at':receipt.get('captured_at'),'grade':'A','latest_edition_exhaustively_proven':False};break
            if selected:
                ctx['injury']=selected
                # Reconstruct partial identity from frozen prior player observations.
                ledger={side:{'player_history':ctx[key]['player_history']} for side,key in (('home','team_a'),('away','team_b'))}
                ctx['roster']=research.roster_from_history(s,ledger,selected)
                ev=[]
                for e in ctx['research']['evidence']:
                    ev.append({**e,'parsed_path':e['acquisition_reference'],'source_tier':'TIER_A' if e['grade']=='A' else 'TIER_B','temporal_grade':'TEMPORAL_B','captured_at':e.get('captured_at')})
                ctx['starting_lineup']=research.extract_lineups(s,ev,ctx['roster'],selected)
                ctx['known_missing_information']=[r for r in ctx['known_missing_information'] if r['field'] not in {'injury','lineup'}]
                ctx['known_missing_information'].append({'field':'lineup','unknown_teams':[t['team'] for t in ctx['starting_lineup']['teams'] if t['basis']=='unknown']})
                check_visible(ctx);path=CONTEXTS/s['sample_id']/'enriched-context.json';write(path,ctx)
                row={**row,'context_hash':digest(ctx),'context_path':str(path.relative_to(ROOT)),'injury_status':selected['status'],'injury_rows':len(selected['records']),
                  'lineup_basis':[t['basis'] for t in ctx['starting_lineup']['teams']],'base_context_hash':row['context_hash']}
        rows.append(row);print(json.dumps({'enrichment':s['sample_id'],'injury':row['injury_status']}),flush=True)
    final={**manifest,'samples':rows,'assembly':'base contexts + bounded modern PDF recovery; base files retained','created_at':now()};final.pop('manifest_hash');final['manifest_hash']=digest(final);write(CONTEXTS/'final-manifest.json',final);return final
if __name__=='__main__':main()
