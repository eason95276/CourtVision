"""SDK envelope conversion only. Never invokes evaluate(), models or production."""
import json,os

def deepeval_case(export_row,actual_output,allow_unfrozen_fixture=False):
    if not isinstance(actual_output,str) or not actual_output.strip():raise ValueError('A real generated output must be supplied explicitly; no placeholder test case')
    os.environ['DEEPEVAL_TELEMETRY_OPT_OUT']='YES'
    from deepeval.test_case import LLMTestCase
    context=json.dumps(export_row['context'],ensure_ascii=False)
    sample=export_row.get('metadata',{}).get('sample_id')
    if sample:
        from evaluation_integrity import load_model_context
        from asset_scanner import canonical_hash
        authentic=load_model_context(sample,anonymous=sample.startswith('anon-'))
        if canonical_hash(authentic)!=canonical_hash(export_row['context']):raise ValueError('Export context differs from frozen sample')
        context=json.dumps(authentic,ensure_ascii=False)
    elif not allow_unfrozen_fixture:raise ValueError('Frozen sample ID required for formal adapter; fixtures must opt in explicitly')
    from safe_model_input import serialize_context,INSTRUCTION
    if 'target_game_id' in export_row['context'].get('matchup',{}):safe_input=serialize_context(export_row['context'])
    else:
        # Anonymous transport needs a frozen sample ID; do not blindly trust an
        # arbitrary anonymous wrapper with evaluator metadata.
        from safe_model_input import frozen_request
        safe_input=frozen_request(export_row['metadata']['sample_id'],anonymous=True)['messages'][0]['content']
    metadata={'dataset_mode':'controlled_closed_book','visibility':'evaluator-only'}
    if export_row.get('metadata',{}).get('sample_id'):metadata['sample_id']=export_row['metadata']['sample_id']
    return LLMTestCase(input=safe_input,actual_output=actual_output,
        expected_output=None,context=[context],metadata=metadata)
