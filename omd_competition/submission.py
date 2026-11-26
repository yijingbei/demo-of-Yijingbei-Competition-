"""Validate declared participant files and bind the reviewed entry point."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from .transport import PROTOCOL


def load_submission(path, scenario, *, require_sandbox=None, preflight_path=None):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    from .runtime import entry
    track = entry(scenario)['track']
    if data.get("protocol") != PROTOCOL or data.get("track") != track:
        raise ValueError("submission protocol/track mismatch")
    if not isinstance(data.get("team_id"), str) or not data["team_id"].strip():
        raise ValueError("submission team_id required")
    command = data.get("entrypoint")
    if 'sandbox' not in data and (not isinstance(command, list) or not command or not all(isinstance(v, str) and v for v in command)):
        raise ValueError("entrypoint must be a nonempty argument array")
    artifacts = data.get("artifacts")
    from .sandbox_preflight import official_required, validate_evidence, approved_team_environment
    required = official_required() or require_sandbox is True
    if required and 'sandbox' not in data:
        raise ValueError('official evaluation requires an isolated container submission')
    group = data.get("resource_group", "cpu")
    if group not in {"cpu", "api"}: raise ValueError("resource_group must be cpu or api")
    if group == "api":
        models = data.get("api_models")
        if (not isinstance(models, list) or not models
                or any(not isinstance(model, dict)
                       or any(not isinstance(model.get(key), str) or not model[key].strip()
                              for key in ("provider", "model"))
                       for model in models)):
            raise ValueError("api_models must be a nonempty list of nonempty provider/model identifiers")
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError("declare participant source/model files in artifacts with SHA-256")
    for name, digest in artifacts.items():
        if not isinstance(name,str) or not isinstance(digest,str) or not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError('invalid submission artifact name or SHA-256')
        relative = Path(name)
        file = (path.parent / relative).resolve()
        if relative.is_absolute() or not file.is_relative_to(path.parent) or any(p.startswith(".") for p in relative.parts):
            raise ValueError("artifact path outside submission directory or hidden file")
        if any((path.parent/Path(*relative.parts[:i])).is_symlink() for i in range(1,len(relative.parts)+1)):
            raise ValueError('submission artifact links are not accepted')
        if not file.is_file() or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError("submission artifact hash mismatch: " + name)
    digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if 'sandbox' in data:
        from .sandbox import container_launch
        evidence = validate_evidence(preflight_path or os.environ.get('OPENMD_SANDBOX_PREFLIGHT'),group) if required else None
        environment = approved_team_environment(os.environ.get('OPENMD_TEAM_ENV_FILE'),data['team_id'])
        staging_root = os.environ.get('OPENMD_SUBMISSION_STAGING')
        if required and not staging_root:
            raise ValueError('official evaluation requires a dedicated submission staging root')
        if staging_root:
            staging_root=Path(staging_root).resolve()
            staging_root.mkdir(parents=True,exist_ok=True,mode=0o700)
            if required and (staging_root.stat().st_uid not in {0,os.getuid()} or staging_root.stat().st_mode & 0o077):
                raise ValueError('official submission staging root must be owner-only on Linux')
        stage=Path(tempfile.mkdtemp(prefix='openmd-submission-',dir=staging_root))
        # Copy only declared content into an independent mount. Preserve this
        # evidence on failure; never fall back to the surrounding source folder.
        for name,artifact_digest in artifacts.items():
            content=(path.parent/name).read_bytes()
            if hashlib.sha256(content).hexdigest()!=artifact_digest:
                raise ValueError('submission artifact changed during staging: '+name)
            target=stage/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(content)
            target.chmod(0o555 if (path.parent/name).stat().st_mode & 0o111 else 0o444)
        for folder in sorted((p for p in stage.rglob('*') if p.is_dir()),reverse=True):
            folder.chmod(0o555)
        stage.chmod(0o555)
        command=container_launch(data,stage,group,preflight=evidence,environment=environment)
        command.metadata.update(team_id=data['team_id'],submission_sha256=digest,staged_artifacts=dict(artifacts))
    return command, digest, data["team_id"], group
