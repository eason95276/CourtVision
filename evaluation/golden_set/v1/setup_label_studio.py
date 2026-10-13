"""Deprecated combined-project setup cannot silently expose Postgame truth."""
if __name__=='__main__':
    raise SystemExit('Shared setup retired. Use evaluation/execution/workspace.py prepare --stage pregame; Postgame is phase-gated.')
