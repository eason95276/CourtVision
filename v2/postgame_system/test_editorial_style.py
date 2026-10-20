import json
from .test_editorial import fixture_store,GAME_ID
from .editorial import generate_postgame_editorial,load_postgame_skill


def test_one_title_only_revision_preserves_review_and_article(tmp_path):
    store=fixture_store(tmp_path);calls=[]
    prose='勇士赢下比赛，赛前方向得到支持，但分差判断偏小。'
    review='方向看对，分差低估。'
    def client(messages,*args):
        calls.append(messages)
        return json.dumps({'title':'标题太短','short_review':review,'article':prose}if len(calls)==1 else
            {'title':'双星缺阵添变数\n勇士主场定乾坤'},ensure_ascii=False)
    result=generate_postgame_editorial(store,GAME_ID,api_client=client,api_key='fixture',output_root=tmp_path/'articles')
    assert result.status=='success'and len(calls)==2
    artifact=json.loads(result.artifact_path.read_text(encoding='utf-8'))
    assert artifact['editorial']['article']==prose and artifact['editorial']['short_review']==review
    assert artifact['title_policy']=={'required':True,'revision_calls':1,'status':'passed'}


def test_invalid_repair_does_not_publish_or_loop(tmp_path):
    store=fixture_store(tmp_path);calls=[]
    def client(messages,*args):
        calls.append(1)
        return json.dumps({'title':'短题','short_review':'短评','article':'正文'}if len(calls)==1 else{'title':'仍然很短'},ensure_ascii=False)
    result=generate_postgame_editorial(store,GAME_ID,api_client=client,api_key='fixture',output_root=tmp_path/'articles')
    assert result.status=='failed'and len(calls)==2
    assert not list((tmp_path/'articles').glob('*.json'))
