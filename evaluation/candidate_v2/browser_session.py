"""Private local LS cookie bootstrap for read-only browser verification."""
import json,sys,urllib.request,urllib.parse,http.cookiejar
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import human_workspace as h

def main():
    sample=h.current();folder=h.HOME/sample['sample_id'];creds=h.read_json(folder/'credentials.json')
    jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url=f'http://127.0.0.1:{h.PORT}/user/login/'
    with opener.open(url,timeout=30) as resp:resp.read()
    csrf=next(c.value for c in jar if c.name=='csrftoken')
    data=urllib.parse.urlencode({'email':creds['username'],'password':creds['password'],'csrfmiddlewaretoken':csrf}).encode()
    with opener.open(urllib.request.Request(url,data=data,headers={'Referer':url}),timeout=30) as resp:resp.read()
    if not any(c.name=='sessionid' for c in jar):raise ValueError('local login failed')
    state={'cookies':[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path,'expires':c.expires or -1,'httpOnly':c.name=='sessionid','secure':c.secure,'sameSite':'Lax'} for c in jar],'origins':[]}
    # Private transient browser cookie file, not an evidence artifact or label.
    with (folder/'browser-state.json').open('w',encoding='utf-8') as file:json.dump(state,file)
    print('Private authenticated browser state prepared; no annotation submitted.')
if __name__=='__main__':main()
