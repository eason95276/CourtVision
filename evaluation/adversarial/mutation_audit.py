"""Mutate isolated code copies; never patch production or immutable files."""
import sys,os,tempfile,subprocess,json,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asset_scanner import EVAL
from build_eval_baseline import save

PYTHON=EVAL/'deepeval/.venv/Scripts/python.exe';folder=EVAL/'adversarial/runtime';folder.mkdir(parents=True,exist_ok=True)
mutations=[
 ('remove_target_exclusion','closed_book_data.py'," and r['game_id']!=target_id",''),
 ('allow_same_day','closed_book_data.py',"r['day']<target_day","r['day']<=target_day"),
 ('remove_all_past_filter','closed_book_data.py',"return [r for r in rows if r['day']<target_day and r['game_id']!=target_id]","return list(rows)"),
 ('disable_identity_detector','closed_book_anonymize.py','walk(context);return issues','walk(context);return []'),
 ('disable_field_hash','validate_closed_book_context.py',"if mapping is None and canonical_hash(values(context,field))!=proof['field_value_sha256']:","if False:"),
 ('disable_home_away_guard','validate_closed_book_context.py',"issues.append('home_away_role_mismatch')","None"),
 ('disable_nested_schema','context_shape.py','for key in set(obj)-keys:','for key in []:'),
 ('allow_same_day_source','validate_closed_book_context.py',"if event['event_time']>=day:","if event['event_time']>day:")]

results=[]
for name,module,old,new in mutations:
    with tempfile.TemporaryDirectory(dir=folder,prefix='mutant-') as tmp:
        temp=Path(tmp)
        for file in EVAL.joinpath('scripts').glob('*.py'):shutil.copyfile(file,temp/file.name)
        asset=temp/'asset_scanner.py';contents=asset.read_text(encoding='utf-8')
        contents=contents.replace('ROOT=Path(__file__).resolve().parents[2]',"ROOT=Path("+repr(str(EVAL.parent))+')',1);asset.write_text(contents,encoding='utf-8')
        source=(temp/module).read_text(encoding='utf-8');assert old in source,name;source=source.replace(old,new,1);(temp/module).write_text(source,encoding='utf-8')
        shutil.copyfile(EVAL/'adversarial/test_adversarial.py',temp/'test_redteam.py')
        env=os.environ.copy();env['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1';env['PYTHONUTF8']='1';env['PYTHONPATH']=str(temp)+os.pathsep+str(EVAL/'adversarial')+os.pathsep+str(EVAL/'scripts')
        command=[str(PYTHON),'-m','pytest',str(temp/'test_closed_book.py'),str(temp/'test_redteam.py'),'-q','-p','no:cacheprovider']
        outcome=subprocess.run(command,cwd=temp,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
        killed=outcome.returncode==1 and 'failed' in outcome.stdout
        row={'mutation':name,'exit_code':outcome.returncode,'killed_by_regression':killed,'setup_error':outcome.returncode not in (0,1),
            'output_tail':outcome.stdout[-1800:],'stderr_tail':outcome.stderr[-400:]}
        if name=='remove_target_exclusion':
            baseline=subprocess.run([str(PYTHON),'-m','pytest',str(temp/'test_closed_book.py'),'-q','-p','no:cacheprovider'],cwd=temp,env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120)
            row['legacy_only_exit_code']=baseline.returncode;row['legacy_only_summary']=baseline.stdout[-400:]
        results.append(row);print(name,'killed',killed,'code',outcome.returncode,flush=True)
save(EVAL/'reports/test-mutation-audit.json',results)
