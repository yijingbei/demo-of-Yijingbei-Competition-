"""Organizer-owned sandbox evidence. Missing or stale evidence never permits fallback."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import time

POLICY = {'cpus': 2, 'memory_bytes': 2147483648, 'swap_bytes': 0, 'pids': 128,
          'uid': 65534, 'gid': 65534, 'tmp_bytes': 67108864, 'stderr_bytes': 8388608,
          'root_readonly': True, 'submission_readonly': True, 'cap_drop': 'ALL',
          'no_new_privileges': True, 'cpu_network': 'none'}
CPU_CHECKS = frozenset({'runtime_limits', 'cpu_throttling', 'memory_oom', 'pids_enforced',
    'nonroot', 'capabilities_dropped', 'no_new_privileges', 'root_readonly',
    'submission_readonly', 'tmp_bounded', 'private_files_invisible', 'cpu_network_none',
    'cross_container_isolation', 'timeout_cleanup', 'protocol_jsonl'})
API_CHECKS = frozenset({'api_egress_allowlisted', 'api_direct_egress_blocked',
                      'api_cross_team_isolation', 'api_billing_verified'})
SCHEMA = 'openmd-sandbox-preflight@1'
MAX_AGE_SECONDS = 3600
MAX_EVIDENCE_BYTES = 1024 * 1024


def official_required():
    return os.environ.get('OPENMD_REQUIRE_SANDBOX') == '1'


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def launcher_fingerprint():
    root = Path(__file__).resolve().parent
    return digest_bytes(json.dumps({name: digest_bytes((root/name).read_bytes()) for name in
        ('sandbox.py', 'sandbox_preflight.py', 'case_evidence.py', 'submission.py', 'transport.py',
         '../official_evaluate.py', '../deployment/linux-vm/verify.py',
         '../deployment/linux-vm/probe_agent.py')},
        sort_keys=True, separators=(',', ':')).encode())


def policy_fingerprint():
    return digest_bytes(json.dumps(POLICY, sort_keys=True, separators=(',', ':')).encode())


def docker_command():
    binary = shutil.which('docker')
    if not binary:
        raise ValueError('Docker unavailable; isolated submissions cannot run')
    endpoint = os.environ.get('OPENMD_DOCKER_HOST', 'unix:///var/run/docker.sock')
    if not endpoint.startswith('unix:///') or any(c in endpoint for c in ('\n', '\r', '\x00')):
        raise ValueError('official Docker endpoint must be an organizer-controlled local Unix socket')
    return [binary, '--host', endpoint]


def docker_environment(extra=None):
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(('DOCKER_', 'OPENMD_')):
            environment.pop(key, None)
    environment.update(extra or {})
    return environment


def runtime_identity():
    if os.name != 'posix' or not Path('/proc/sys/kernel/random/boot_id').is_file():
        raise ValueError('official sandbox requires an independent Linux worker')
    command = docker_command()
    try:
        run = subprocess.run([*command, 'info', '--format', '{{json .}}'],
            capture_output=True, text=True, timeout=10, env=docker_environment())
        if run.returncode:
            raise ValueError('Docker server unavailable; external evaluation is disabled')
        data = json.loads(run.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
        raise ValueError('Docker server identity could not be verified') from error
    fields = ('ID', 'ServerVersion', 'CgroupDriver', 'CgroupVersion')
    if not isinstance(data,dict) or not all(isinstance(data.get(k), str) and data[k] for k in fields):
        raise ValueError('Docker server identity or cgroup information is missing')
    return {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'docker_command': command, **{k: data[k] for k in fields}}


def validate_image(image):
    try:
        result = subprocess.run([*docker_command(),'image','inspect','--format','{{json .}}',image],
            capture_output=True,text=True,timeout=10,env=docker_environment())
        if result.returncode:
            raise ValueError('reviewed image must already exist locally; automatic pulls are disabled')
        info=json.loads(result.stdout)
        if not isinstance(info,dict) or image not in (info.get('RepoDigests') or []):
            raise ValueError('local image does not match the declared immutable digest')
        if info.get('Config',{}).get('Volumes'):
            raise ValueError('image declares writable volumes outside the approved temporary directory')
    except (OSError,subprocess.TimeoutExpired,json.JSONDecodeError) as error:
        raise ValueError('container image policy could not be verified') from error


def _read_organizer_file(path, *, owner_only):
    """Bounded ordinary-file read; check every path component and sampled identity."""
    # Fingerprint inspection via isolated runpy remains standard-library-only.
    from .case_evidence import _guarded_file

    if os.name != 'posix':
        raise ValueError('organizer evidence/configuration requires Linux')
    # Do not resolve links before the ordinary-file guard gets to inspect them.
    path = Path(path).absolute()
    if '..' in path.parts:
        raise ValueError('organizer evidence path must not contain parent traversal')
    anchor = Path(path.anchor)
    with _guarded_file(anchor, path.relative_to(anchor).as_posix()) as stream:
        info = os.fstat(stream.fileno())
        if info.st_size > MAX_EVIDENCE_BYTES:
            raise ValueError('organizer evidence/configuration must be a bounded regular file')
        forbidden = 0o077 if owner_only else 0o022
        if info.st_uid not in {0, os.getuid()} or stat.S_IMODE(info.st_mode) & forbidden:
            raise ValueError('organizer evidence/configuration has unsafe ownership or permissions')
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_EVIDENCE_BYTES:
            raise ValueError('organizer evidence/configuration exceeds its bounded read')
        if len(raw) != info.st_size:
            raise ValueError('organizer evidence/configuration changed while reading')
    return path, raw


def protected_json(path):
    from .case_evidence import decode_json

    path, raw = _read_organizer_file(path, owner_only=True)
    data = decode_json(raw, label='organizer evidence/configuration')
    if not isinstance(data, dict):
        raise ValueError('organizer evidence/configuration must be a JSON object')
    return path, raw, data


def validate_evidence(path, group):
    if not path:
        raise ValueError('official evaluation requires sandbox preflight evidence')
    evidence_path, raw, report = protected_json(path)
    if group not in {'cpu', 'api'}:
        raise ValueError('invalid sandbox resource group')
    if report.get('schema_version') != SCHEMA or report.get('status') != 'passed':
        raise ValueError('sandbox preflight has not passed')
    timestamp = report.get('completed_at_unix')
    if (type(timestamp) not in {int, float} or not 0 <= time.time()-timestamp <= MAX_AGE_SECONDS):
        raise ValueError('sandbox preflight evidence is stale or future-dated')
    if report.get('policy_sha256') != policy_fingerprint() or report.get('launcher_sha256') != launcher_fingerprint():
        raise ValueError('sandbox preflight policy or launcher changed')
    if report.get('runtime') != runtime_identity():
        raise ValueError('sandbox preflight belongs to another worker/runtime')
    required = CPU_CHECKS | (API_CHECKS if group == 'api' else frozenset())
    checks = report.get('checks', {})
    if not isinstance(checks, dict) or any(checks.get(k) is not True for k in required):
        raise ValueError('sandbox preflight is incomplete for this resource group')
    artifacts = report.get('evidence_files')
    if not isinstance(artifacts, dict) or not artifacts:
        raise ValueError('sandbox preflight lacks raw evidence files')
    originals = []
    for name, digest in artifacts.items():
        if not isinstance(name, str) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid sandbox raw evidence binding')
        relative = PurePosixPath(name)
        if (not name or '\\' in name or ':' in name or relative.is_absolute()
                or '..' in relative.parts or relative.as_posix() != name or name == '.'):
            raise ValueError('sandbox raw evidence path must be canonical and relative')
        artifact = evidence_path.parent.joinpath(*relative.parts)
        _, artifact_raw = _read_organizer_file(artifact, owner_only=False)
        if digest_bytes(artifact_raw) != digest:
            raise ValueError('sandbox raw evidence missing or changed')
        originals.append((artifact, digest))
    # A later file or runtime lookup must not silently invalidate an earlier read.
    for artifact, digest in originals:
        _, artifact_raw = _read_organizer_file(artifact, owner_only=False)
        if digest_bytes(artifact_raw) != digest:
            raise ValueError('sandbox raw evidence changed during validation')
    final_path, final_raw, _ = protected_json(evidence_path)
    if final_path != evidence_path or final_raw != raw:
        raise ValueError('sandbox preflight report changed during validation')
    return {'schema_version': SCHEMA, 'sha256': digest_bytes(raw), 'resource_group': group,
            'completed_at_unix': timestamp, 'runtime': report['runtime'],
            'policy_sha256': report['policy_sha256'], 'launcher_sha256': report['launcher_sha256']}


def validate_launch_binding(binding,group):
    """Revalidate a reused declaration immediately before each container starts."""
    if (not isinstance(binding,dict) or binding.get('schema_version')!=SCHEMA
            or binding.get('resource_group')!=group
            or not isinstance(binding.get('sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',binding['sha256'])):
        raise ValueError('container launch lacks a verified preflight binding')
    timestamp=binding.get('completed_at_unix')
    if type(timestamp) not in {int,float} or not 0<=time.time()-timestamp<=MAX_AGE_SECONDS:
        raise ValueError('container launch preflight binding is stale or future-dated')
    if binding.get('policy_sha256')!=policy_fingerprint() or binding.get('launcher_sha256')!=launcher_fingerprint():
        raise ValueError('container launch policy or launcher changed since preflight')
    if binding.get('runtime')!=runtime_identity():
        raise ValueError('container launch worker/runtime changed since preflight')


def approved_team_environment(path, team_id):
    if not path:
        return {}
    _, _, data = protected_json(path)
    if data.get('team_id') != team_id or not isinstance(data.get('environment'), dict):
        raise ValueError('API environment configuration does not belong to this team')
    environment = data['environment']
    if not all(isinstance(k, str) and re.fullmatch('[A-Z][A-Z0-9_]*', k)
               and not k.startswith(('OPENMD_', 'DOCKER_', 'LD_', 'DYLD_'))
               and k not in {'PATH','HOME','PYTHONPATH','PYTHONHOME','BASH_ENV','ENV','SHELL'} and isinstance(v, str)
               and '\x00' not in v for k, v in environment.items()):
        raise ValueError('invalid approved team environment')
    return environment
