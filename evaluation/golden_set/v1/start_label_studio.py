"""Compatibility launcher: shared instance retired, isolated Pregame only."""
import runpy,sys
from pathlib import Path
if __name__=='__main__':
    entry=Path(__file__).resolve().parents[2]/'execution/workspace.py'
    sys.path.insert(0,str(entry.parent))
    sys.argv=[str(entry),'prepare','--stage','pregame']
    runpy.run_path(str(entry),run_name='__main__')
