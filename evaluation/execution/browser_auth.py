"""Authenticate existing identity on isolated local instance, no label submission."""
import argparse,http.cookiejar,json,re,urllib.request,urllib.parse
from workspace import RUNTIME,PORTS
from control import Control
from asset_scanner import read_json
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--stage',choices=['pregame','postgame'],default='pregame');a=p.parse_args()
    if a.stage=='postgame':Control().postgame_gate('browser-auth')
    identity=Control().workspace(a.stage)
    if not identity:raise ValueError('workspace not initialized')
    c=read_json(RUNTIME/a.stage/'credentials.json')
    jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    url=f"http://127.0.0.1:{PORTS[a.stage]}/user/login/?next=/projects/{identity['project_id']}/data/"
    page=opener.open(url,timeout=20).read().decode()
    token=re.search(r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"',page)
    if not token:raise ValueError('login CSRF missing')
    form=urllib.parse.urlencode({'csrfmiddlewaretoken':token.group(1),'email':c['username'],'password':c['password'],'persist_session':'on'}).encode()
    response=opener.open(urllib.request.Request(url,data=form,headers={'Referer':url,'Content-Type':'application/x-www-form-urlencoded'}),timeout=20)
    if '/user/login' in response.url:raise ValueError('login failed')
    state={'cookies':[{'name':cookie.name,'value':cookie.value,'domain':cookie.domain,'path':cookie.path,'expires':cookie.expires or -1,
        'httpOnly':cookie.has_nonstandard_attr('HttpOnly'),'secure':cookie.secure,'sameSite':'Lax'} for cookie in jar],'origins':[]}
    dest=RUNTIME/a.stage/'browser-state.json';dest.write_text(json.dumps(state),encoding='utf-8')
    print('Existing identity authenticated on isolated instance; private state saved. No annotation.')
