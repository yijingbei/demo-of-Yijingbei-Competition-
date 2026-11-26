"""Standalone hybrid baseline launcher; existing rule CLI remains unchanged."""
from pathlib import Path
import os, sys
ROOT=Path(__file__).resolve().parent
for key in ('TI_CPU_MAX_NUM_THREADS','TI_NUM_THREADS','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ.setdefault(key,'2')
sys.path[:0]=[str(ROOT),str(ROOT/'engine/source_codes'),str(ROOT/'tools')]
os.environ['PYTHONPATH']=os.pathsep.join([str(ROOT),str(ROOT/'engine/source_codes'),str(ROOT/'tools'),os.environ.get('PYTHONPATH','')])
if __name__=='__main__':
    from omd_competition.hybrid_runner import main
    raise SystemExit(main())
