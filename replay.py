"""Convenience entry point for the optional offline HTML replay plugin."""
from pathlib import Path
import runpy
import sys

plugin = Path(__file__).resolve().parent / 'plugins/html_replay'
sys.path.insert(0, str(plugin))
if __name__ == '__main__':
    runpy.run_path(str(plugin / 'run.py'), run_name='__main__')
