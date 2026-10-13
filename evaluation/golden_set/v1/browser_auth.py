"""Authenticate the existing user locally; never print credentials/cookies."""
import http.cookiejar,json,re,urllib.request,urllib.parse
from pathlib import Path
HERE=Path(__file__).resolve().parent;EVAL=HERE.parents[1]
c=json.loads((EVAL/'label_studio/runtime/credentials.json').read_text())
jar=http.cookiejar.CookieJar();opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
url='http://127.0.0.1:8681/user/login/?next=/projects/2/data/'
body=opener.open(url,timeout=20).read().decode();token=re.search(r'name="csrfmiddlewaretoken"[^>]*value="([^"]+)"',body)
if not token:raise ValueError('Observed login form CSRF token missing')
form=urllib.parse.urlencode({'csrfmiddlewaretoken':token.group(1),'email':c['username'],'password':c['password'],'persist_session':'on'}).encode()
response=opener.open(urllib.request.Request(url,data=form,headers={'Referer':url,'Content-Type':'application/x-www-form-urlencoded'}),timeout=20)
if '/user/login' in response.url:raise ValueError('Authenticated login failed; no bypass')
state={'cookies':[{'name':cookie.name,'value':cookie.value,'domain':cookie.domain,'path':cookie.path,'expires':cookie.expires or -1,'httpOnly':cookie.has_nonstandard_attr('HttpOnly'),'secure':cookie.secure,'sameSite':'Lax'} for cookie in jar],'origins':[]}
p=HERE/'runtime/browser-state.json';p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(state),encoding='utf-8')
print('Existing user authenticated; browser state saved in ignored private runtime')
