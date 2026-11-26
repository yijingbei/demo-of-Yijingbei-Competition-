"""Content binding of all executable code and data, independent of install path."""
import hashlib
import json
from pathlib import Path


def runtime_files():
    root = Path(__file__).resolve().parents[1]
    from .runtime import ENGINE_ROOT, SCENE_ROOT
    engine = ENGINE_ROOT
    files = {}
    for prefix, directory in (("competition", root), ("engine", engine)):
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(directory)
            if not path.is_file() or any(p in {"__pycache__", ".git", ".venv", "artifacts", "dist", "tests", "docs"} for p in relative.parts):
                continue
            if prefix == "competition" and relative.parts[0] not in {"omd_competition", "scenarios", "requirements-lock.txt", "run.py"}:
                continue
            if prefix == "engine" and relative.parts[0] not in {"openmdbench", "catalog", "scenarios", "env", "config", "framework", "model", "assets"}:
                continue
            if path.suffix not in {".py", ".yaml", ".yml", ".json", ".txt"}: continue
            files[prefix + "/" + relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    files['engine/patches.json'] = hashlib.sha256((root/'engine/patches.json').read_bytes()).hexdigest()
    if SCENE_ROOT != root:
        for path in sorted((SCENE_ROOT/'scenarios').rglob('*')):
            if path.is_file():
                files['evaluation-suite/'+path.relative_to(SCENE_ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return files


def runtime_fingerprint():
    return hashlib.sha256(json.dumps(runtime_files(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
