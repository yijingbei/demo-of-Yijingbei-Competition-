"""Compatibility entry point for the autonomous-v2 public reference program."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from examples.autonomous_surface_reference import main

if __name__=='__main__':
    main()
