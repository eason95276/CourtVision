"""Optional future integration, intentionally no Judge model/prompt/threshold."""
from pydantic import BaseModel,ConfigDict,Field

class JudgeInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    sample_id:str
    revision:str
    context_hash:str
    editorial_hash:str
    human_label_revision:str|None=None
    judge_model:str|None=None
    judge_prompt:str|None=None
    pass_threshold:float|None=None

def import_human_labels(receipt,labels):
    if not labels or not receipt.get('export_hash'):raise ValueError('real frozen human export required')
    return {'sample_id':receipt['sample_id'],'labels':labels,'calibration_performed':False}

def run_judge(*args,**kwargs):raise RuntimeError('Judge model, prompt, calibration and authorization are not decided; no Judge execution')

CALIBRATION_TEMPLATE={'judge_model':None,'criteria_prompt':None,'threshold':None,'human_gold_labels':None,'agreement':None,'kappa':None,'status':'NOT_STARTED'}
