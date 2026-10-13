"""Null-output tool adapters. Metadata and manifests are evaluator-private."""
import json
from asset_scanner import EVAL,read_json
from build_eval_baseline import jsonl,save

def model_input(context):
    return {'input':'Analyze this fixed basketball context only. Do not use outside knowledge, search, web or tools.',
        'actual_output':None,'expected_output':None,'context':context,
        'metadata':{'dataset_mode':'controlled_closed_book','model_calls':0,'visibility':'metadata is not part of model prompt'}}

def export():
    candidates=[json.loads(s) for s in (EVAL/'datasets/closed_book_candidates.jsonl').read_text(encoding='utf-8').splitlines()]
    normal=[];anonymous=[];ls=[];lsanon=[]
    for c in candidates:
        context=read_json(EVAL/c['context_path']);anon=read_json(EVAL/c['anonymous_context_path'])
        for value,sample,output,imports in [(context,c['sample_id'],normal,ls),(anon,c['anonymous_sample_id'],anonymous,lsanon)]:
            item=model_input(value);item['metadata']['sample_id']=sample;output.append(item)
            imports.append({'data':{'sample_id':sample,'matchup':value['matchup'],'context':json.dumps(value,ensure_ascii=False),'future_response':None},
                'meta':{'purpose':'preparation_only_no_annotations'}})
    folder=EVAL/'datasets/closed_book_exports'
    jsonl(folder/'deepeval-normal.jsonl',normal);jsonl(folder/'deepeval-anonymous.jsonl',anonymous)
    save(folder/'label-studio-normal.json',ls);save(folder/'label-studio-anonymous.json',lsanon)
    report={'normal_samples':len(normal),'anonymous_samples':len(anonymous),'actual_outputs_filled':0,'expected_outputs_filled':0,'labels_created':0,
        'note':'Adapters only, not instantiated DeepEval test cases. Populate real output later. Future runner must send only input+context and disable all tools.'}
    save(EVAL/'reports/closed-book-export-readiness.json',report);print(json.dumps(report))

if __name__=='__main__':export()
