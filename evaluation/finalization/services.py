"""Start only owned evaluation services, hidden, preserving existing instances."""
import os,socket,subprocess
from .common import *
def listening(port):
    with socket.socket() as s:return s.connect_ex(('127.0.0.1',port))==0
def phoenix():
    if listening(8682):return {'status':'already_listening','port':8682}
    home=ROOT/'evaluation/phoenix/runtime';env=os.environ.copy();env.update(PHOENIX_HOST='127.0.0.1',PHOENIX_PORT='8682',PHOENIX_WORKING_DIR=str(home))
    with (home/'final-v1-server.log').open('ab') as log:
        p=subprocess.Popen([str(ROOT/'evaluation/phoenix/.venv/Scripts/phoenix.exe'),'serve'],cwd=ROOT/'evaluation',env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    receipt={'pid':p.pid,'port':8682,'created_at':now()};write(HERE/'phoenix-process.json',receipt);return receipt
if __name__=='__main__':print(json.dumps(phoenix()))
