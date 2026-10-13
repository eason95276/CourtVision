"""Actual Reader/API rendering on copied Heat @ Raptors data; Writer/status candidates are explicit fixtures."""
import json,shutil,subprocess,threading,functools
from http.server import ThreadingHTTPServer
from pathlib import Path
import pytest
from .test_global_capabilities import frozen_store
from . import server
from .prediction_version_service import PredictionVersionService
from .schedule_catalog import ScheduleCatalog
from v2.postgame_system import editorial
from v2.postgame_system.version_service import read_postgame

PW=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'
ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('width,height',[(1366,768),(390,844)])
@pytest.mark.parametrize('gid',['espn:401902644','espn:401914101'])
def test_non_portfolio_api_readers_candidates_and_restore(frozen_store,tmp_path,monkeypatch,width,height,gid):
    if not shutil.which('node') or not PW.is_dir():pytest.skip('Local Node/Playwright bundle absent')
    out=tmp_path/'articles'
    post=read_postgame(frozen_store,gid)
    assert post is not None
    def fake_writer(messages,key,model,timeout):
        return json.dumps({'title':'测试稿：赛前判断与终场比分复核','short_review':'测试稿：对照冻结的赛前判断和本场终场比分。',
            'article':'这是工程验收使用的 fake Writer 文章，不是正式篮球复盘。\n\n这份文章只用于验证另一组球队的预测、赛果、来源和阅读器可以保持同一版本绑定。'},ensure_ascii=False)
    result=editorial.generate_postgame_editorial(frozen_store,gid,api_client=fake_writer,output_root=out)
    assert result.status=='success'
    entry=editorial.build_public_postgame_entry;payload=editorial.build_public_postgame_payload
    monkeypatch.setattr(editorial,'build_public_postgame_entry',lambda store,post:entry(store,post,output_root=out))
    monkeypatch.setattr(editorial,'build_public_postgame_payload',lambda store,post:payload(store,post,output_root=out))
    service=PredictionVersionService(store=frozen_store);catalog=ScheduleCatalog(frozen_store.path)
    monkeypatch.setattr(server,'_PREDICTION_VERSION_SERVICE',service)
    monkeypatch.setattr(server,'_SCHEDULE_CATALOG',catalog)
    monkeypatch.setattr(server,'DEFAULT_PORTFOLIO_EDITORIAL_DIR',tmp_path/'no-sidecars')
    http=ThreadingHTTPServer(('127.0.0.1',0),functools.partial(server.Handler,directory=str(server.STATIC_ROOT)))
    thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
    captures=ROOT/'output/playwright/global-capabilities';captures.mkdir(parents=True,exist_ok=True)
    try:
        check=subprocess.run(['node',str(Path(__file__).with_name('global_browser_check.cjs')),str(PW),
            f'http://127.0.0.1:{http.server_port}',gid,str(width),str(height),str(captures)],
            capture_output=True,text=True,encoding='utf-8',timeout=90)
        assert check.returncode==0,check.stderr
        info=json.loads(check.stdout)
        assert info['pageErrors']==[] and info['consoleErrors']==[]
        assert info['postRequests']==0 and not info['overflow']
        assert info['keyPlayers']==[3,3]
        assert all(count==5 for count in info['lineupCounts'])
        assert info['outReplaced'] and info['questionableKept']
        assert info['confirmedPriority'] and info['sourceExpectedPriority'] and info['insufficientCandidateState']
        assert info['conditionalSingleMinutes']
        assert info['postgameVersion']==post['version_id']
        assert info['predictionVersion']==post['base_prediction_version_id']
        assert info['restoredVersion']==info['predictionVersion']
        assert '赛前短评' in info['reader'] and '来源与数据' in info['postgameReader']
        assert info['settlementSummary'].startswith('胜负看')
        assert not info['manualSidecar']
        if gid=='espn:401914101':
            assert '第25秒让分判断' in info['marketText'] and '第25秒大小分判断' in info['marketText']
        (captures/f'{gid.replace(":","_")}-{width}-result.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    finally:
        http.shutdown();thread.join(timeout=5);http.server_close()
