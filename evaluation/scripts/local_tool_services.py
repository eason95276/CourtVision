"""Launch isolated local tools; never imports CourtVision or calls a model."""
import json,os,secrets,subprocess,sys,socket
from pathlib import Path
from http.server import BaseHTTPRequestHandler,HTTPServer

ROOT=Path(__file__).resolve().parents[1]

class Fixture(BaseHTTPRequestHandler):
    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        data=json.dumps({'output':'smoke_ok','input':body.get('question'),'fixture':True}).encode()
        self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
    def do_GET(self):
        self.send_response(200);self.end_headers();self.wfile.write(b'isolated fixture only')
    def log_message(self,*args):pass

def launch():
    for port in (8681,8682,8683):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise RuntimeError(f'Port {port} already in use; do not launch duplicate tool services. Read reports/tool-processes.json.')
    env=os.environ.copy();env['DEEPEVAL_TELEMETRY_OPT_OUT']='YES';env['PROMPTFOO_DISABLE_TELEMETRY']='1'
    creds_path=ROOT/'label_studio/runtime/credentials.json';creds_path.parent.mkdir(parents=True,exist_ok=True)
    creds={'username':'local-evaluation@example.test','password':secrets.token_urlsafe(24),'token':secrets.token_hex(20)}
    if creds_path.exists():creds=json.loads(creds_path.read_text())
    else:creds_path.write_text(json.dumps(creds),encoding='utf-8')
    commands={
        'label_studio':[str(ROOT/'label_studio/.venv/Scripts/label-studio.exe'),'start','--no-browser','--internal-host','127.0.0.1','--port','8681','--data-dir',str(ROOT/'label_studio/runtime'),'--username',creds['username'],'--password',creds['password'],'--user-token',creds['token'],'--enable-legacy-api-token'],
        'phoenix':[str(ROOT/'phoenix/.venv/Scripts/phoenix.exe'),'serve'],
        'fixture':[sys.executable,str(Path(__file__).resolve()),'fixture']}
    processes={}
    for name,cmd in commands.items():
        folder=ROOT/(name if name!='fixture' else 'promptfoo')/'runtime';folder.mkdir(parents=True,exist_ok=True)
        specific=env.copy()
        if name=='phoenix':specific.update(PHOENIX_HOST='127.0.0.1',PHOENIX_PORT='8682',PHOENIX_WORKING_DIR=str(folder))
        with (folder/'server.log').open('ab') as log:
            p=subprocess.Popen(cmd,cwd=ROOT,env=specific,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        processes[name]={'pid':p.pid,'port':{'label_studio':8681,'phoenix':8682,'fixture':8683}[name]}
    (ROOT/'reports/tool-processes.json').write_text(json.dumps(processes,indent=2),encoding='utf-8')
    print(json.dumps(processes))

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='fixture':HTTPServer(('127.0.0.1',8683),Fixture).serve_forever()
    else:launch()
