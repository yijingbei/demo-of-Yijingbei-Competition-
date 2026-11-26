"""Diagnose this interpreter's pinned dependencies without loading the engine."""
import argparse
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile


MODULES = ('yaml', 'numpy', 'pydantic', 'gymnasium', 'matplotlib', 'pyproj', 'taichi')
IMPORT_CODE = 'import importlib,sys; importlib.import_module(sys.argv[1])'
LOG_LIMIT = 8192


def read_lock(path):
    """Accept only the exact default dependency pins shipped with this package."""
    pins = {}
    for number, line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(), 1):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9_.-]*)==([0-9][A-Za-z0-9.!+_-]*)', line)
        if not match:
            raise ValueError(f'unsupported dependency pin on line {number}')
        name, version = match.groups()
        normalized = re.sub(r'[-_.]+', '-', name).lower()
        if normalized in pins:
            raise ValueError(f'duplicate dependency pin: {name}')
        pins[normalized] = version
    if not pins:
        raise ValueError('dependency lock is empty')
    return pins


def dependency_versions(pins):
    checks = []
    for name, expected in sorted(pins.items()):
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError:
            actual = None
        checks.append({'distribution': name, 'expected': expected, 'actual': actual,
                       'ok': actual == expected})
    return checks


def _tail(stream):
    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(max(0, size - LOG_LIMIT))
    return {'tail': stream.read().decode('utf-8', errors='replace'),
            'bytes': size, 'truncated': size > LOG_LIMIT}


def check_import(module, timeout):
    if module not in MODULES:
        raise ValueError('module is not a declared dependency probe')
    # A fresh isolated interpreter avoids inheriting successful imports or a
    # package from the caller's working directory. Native crashes stay in it.
    command = [sys.executable, '-I', '-B', '-c', IMPORT_CODE, module]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                     stderr=stderr, timeout=timeout, check=False)
            exit_code, timed_out, error = process.returncode, False, None
        except subprocess.TimeoutExpired:
            exit_code, timed_out, error = None, True, 'dependency import timed out'
        except OSError as exc:
            exit_code, timed_out, error = None, False, str(exc)
        return {'module': module, 'ok': exit_code == 0, 'exit_code': exit_code,
                'timed_out': timed_out, 'error': error,
                'stdout': _tail(stdout), 'stderr': _tail(stderr)}


def timeout_seconds(value):
    result = float(value)
    if not math.isfinite(result) or not 0 < result <= 120:
        raise argparse.ArgumentTypeError('timeout must be greater than 0 and at most 120 seconds')
    return result


def diagnose(lock_path, *, timeout=30):
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise ValueError('invalid dependency import timeout')
    report = {'schema_version': 'competition-environment-check@1',
              'scope': 'installation_diagnostics_only',
              'python': {'executable': sys.executable, 'version': platform.python_version(),
                         'supported': sys.version_info[:2] in {(3, 11), (3, 12)}},
              'system': {'name': platform.system(), 'machine': platform.machine(),
                         'libc': list(platform.libc_ver())},
              'versions': [], 'imports': [], 'errors': []}
    try:
        report['versions'] = dependency_versions(read_lock(lock_path))
    except (OSError, ValueError) as exc:
        report['errors'].append(str(exc))
    # Version metadata alone cannot detect unresolved GLIBC/native DLL symbols.
    report['imports'] = [check_import(module, timeout) for module in MODULES]
    report['ok'] = (report['python']['supported'] and not report['errors']
                    and all(item['ok'] for item in report['versions'])
                    and all(item['ok'] for item in report['imports']))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--timeout', type=timeout_seconds, default=30,
                        help='maximum seconds for each of the seven dependency imports')
    args = parser.parse_args(argv)
    report = diagnose(Path(__file__).resolve().with_name('requirements-lock.txt'),
                      timeout=args.timeout)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
