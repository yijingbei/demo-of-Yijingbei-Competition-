"""Relocatable CLI, usable without an editable install of the engine."""
from pathlib import Path
import os
import runpy
import sys

# Bound the child MMG/Taichi pool before any engine import or worker spawn.
for variable in ('TI_CPU_MAX_NUM_THREADS','TI_NUM_THREADS','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS'):
    os.environ.setdefault(variable,'2')

if sys.version_info[:2] not in {(3, 11), (3, 12)}:
    raise SystemExit("OpenMDBench requires Python 3.11 or 3.12.")
root = Path(__file__).resolve().parent
engine = root / "engine/source_codes"
if not (engine / "openmdbench").is_dir(): raise SystemExit("Bundled engine source directory is missing.")
sys.path[:0] = [str(root), str(root / "tools"), str(engine)]
os.environ["PYTHONPATH"] = os.pathsep.join([str(root), str(engine), os.environ.get("PYTHONPATH", "")])
def main():
    if len(sys.argv)>1 and sys.argv[1]=='replay':
        sys.argv.pop(1)
        runpy.run_path(str(root/'replay.py'),run_name='__main__')
        return
    allowed = {"evaluate", "agent", "suite"}
    if len(sys.argv) < 2 or sys.argv[1] not in allowed:
        raise SystemExit("Usage: python run.py {evaluate|agent|suite|replay} ...")
    command = sys.argv.pop(1)
    runpy.run_module("omd_competition."+command, run_name="__main__")


if __name__ == "__main__": main()
