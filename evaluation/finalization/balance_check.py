"""Read-only credential diagnostics; never emits keys or attempts generation."""
import urllib.request,urllib.error
from .common import *
def main():
    credentials();key=os.environ['DEEPSEEK_API_KEY'];disk=None
    for line in (ROOT/'.env').read_text(encoding='utf-8-sig').splitlines():
        if line.strip().startswith('DEEPSEEK_API_KEY='):disk=line.split('=',1)[1].strip().strip('"').strip("'")
    result={'credential_present':True,'environment_equals_project_credential':key==disk if disk else None,'checked_at':now(),'generation_calls':0}
    try:
        req=urllib.request.Request('https://api.deepseek.com/user/balance',headers={'Authorization':'Bearer '+key})
        with urllib.request.urlopen(req,timeout=20) as r:data=json.load(r)
        result['is_available']=data.get('is_available');result['balances']=[{k:x.get(k) for k in ('currency','total_balance')} for x in data.get('balance_infos',[])]
    except urllib.error.HTTPError as e:result['http_status']=e.code
    except Exception as e:result['error']=type(e).__name__
    write(HERE/'provider-availability.json',result);print(json.dumps(result))
if __name__=='__main__':main()
