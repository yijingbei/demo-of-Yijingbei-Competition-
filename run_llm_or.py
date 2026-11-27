"""Standalone launcher for the independent LLM + operations research strategy."""
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parent
for key in ('TI_CPU_MAX_NUM_THREADS', 'TI_NUM_THREADS', 'OMP_NUM_THREADS',
            'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ.setdefault(key, '2')
sys.path[:0] = [str(ROOT), str(ROOT / 'engine/source_codes')]
os.environ['PYTHONPATH'] = os.pathsep.join([
    str(ROOT), str(ROOT / 'engine/source_codes'), os.environ.get('PYTHONPATH', '')])

if __name__ == '__main__':
    from omd_competition.llm_or.runner import main
    raise SystemExit(main())
