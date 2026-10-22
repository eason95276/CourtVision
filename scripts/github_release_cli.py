"""Use existing user-level Git credentials without writing/printing tokens."""
import json,os,subprocess,sys,urllib.request,urllib.error

def credential():
    key=os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if key:return key
    env=os.environ.copy();env.update(GIT_TERMINAL_PROMPT='0',GCM_INTERACTIVE='never')
    r=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',text=True,capture_output=True,env=env,timeout=20)
    fields=dict(line.split('=',1) for line in r.stdout.splitlines() if '=' in line)
    if r.returncode or not fields.get('password'):raise RuntimeError('existing_github_credential_unavailable')
    return fields['password']

def api(path):
    req=urllib.request.Request('https://api.github.com'+path,headers={'Authorization':'Bearer '+credential(),'Accept':'application/vnd.github+json','User-Agent':'CourtVision-Release'})
    with urllib.request.urlopen(req,timeout=20) as r:return json.load(r),r.headers.get('X-OAuth-Scopes')

def main():
    try:
        if len(sys.argv)==1 or sys.argv[1]=='inspect':
            user,scopes=api('/user');repo,_=api('/repos/eason95276/CourtVision')
            if user['login']!='eason95276':raise RuntimeError('credential_owner_does_not_match_authorized_repository')
            print(json.dumps({'login':user['login'],'scopes':scopes,'repository':repo['full_name'],'private':repo['private'],'branch':repo['default_branch'],'permissions':repo.get('permissions'),'homepage':repo.get('homepage')}))
        else:
            env=os.environ.copy();env['GH_TOKEN']=credential()
            r=subprocess.run(['gh',*sys.argv[1:]],env=env)
            raise SystemExit(r.returncode)
    except urllib.error.HTTPError as e:print(json.dumps({'error':'github_http_'+str(e.code)}));raise SystemExit(1)
    except (RuntimeError,OSError,subprocess.TimeoutExpired) as e:print(json.dumps({'error':type(e).__name__,'message':str(e)[:90]}));raise SystemExit(1)
if __name__=='__main__':main()
