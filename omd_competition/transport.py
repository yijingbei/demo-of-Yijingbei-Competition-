"""Bounded JSONL participant transport. This is local process isolation, not an OCI sandbox."""
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time

PROTOCOL = "openmd-competition@0.1"
MAX_LINE = 1024 * 1024
# JSON interoperability and safe aggregation, not an API spending budget.
MAX_USAGE_VALUE = 2**53 - 1


class AgentFailure(RuntimeError): pass


def strict_loads(line):
    def unique_members(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key: ' + key)
            result[key] = value
        return result
    def invalid_constant(value): raise ValueError("non-finite number: " + value)
    def finite_float(value):
        parsed = float(value)
        if not math.isfinite(parsed): raise ValueError("non-finite number")
        return parsed
    try:
        return json.loads(line, object_pairs_hook=unique_members,
                          parse_constant=invalid_constant, parse_float=finite_float)
    except RecursionError as error:
        # An untrusted reply can fit MAX_LINE and still exceed the decoder's
        # nesting capacity. Normalize at the input boundary, not around evaluator
        # code, so it follows the existing participant protocol-failure path.
        raise ValueError('participant JSON nesting exceeds parser capacity') from error


class Participant:
    def __init__(self, command, log_path, resource_group="cpu"):
        from .sandbox import ContainerLaunch
        from .sandbox_preflight import POLICY, docker_environment, official_required, validate_launch_binding
        launch = command.new_instance() if isinstance(command, ContainerLaunch) else None
        if official_required() and (launch is None or not launch.metadata.get('external_sandbox_verified')):
            raise AgentFailure('official evaluation requires a verified isolated container submission')
        if launch is not None and launch.metadata.get('external_sandbox_verified'):
            # Calibration can reuse a declaration long after load_submission.
            # Evidence must still match this actual start, not merely parsing time.
            validate_launch_binding(launch.metadata.get('preflight'),resource_group)
        self.container_cleanup = launch.cleanup if launch else None
        self.container_cleanup_check = launch.cleanup_check if launch else None
        self.container_inspect_id = launch.inspect_id if launch else None
        self.isolation_audit = dict(launch.metadata) if launch else {
            'execution_kind': 'trusted_subprocess', 'external_sandbox_verified': False}
        self.isolation_audit.update(cleanup_status='pending', container_id=None)
        self.cleanup_error = None
        self.protocol_error = None
        self.exit_code = None
        self.termination_reason = None
        self.stderr_limit_bytes = POLICY['stderr_bytes']
        self.stderr_logged_bytes = 0
        self.stderr_limit_exceeded = False
        self._failure_lock = threading.Lock()
        command = launch.command if launch else command
        self.resource_group = resource_group
        self.usage = None
        self.log = open(log_path, "wb")
        self.lines = queue.Queue(maxsize=2)
        self.elapsed = 0.0
        self.timeouts = 0
        self.broken = False
        try:
            environment = docker_environment(launch.environment) if launch else dict(os.environ)
            for key in ('OPENMD_SUITE_ROOT','OPENMD_REQUIRE_SANDBOX','OPENMD_SANDBOX_PREFLIGHT','OPENMD_TEAM_ENV_FILE'):
                environment.pop(key,None)
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=environment,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1,
                start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as error:
            self.log.close()
            raise AgentFailure("participant launch failed: " + str(error)) from error
        threading.Thread(target=self._read, daemon=True).start()
        self.stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self.stderr_thread.start()

    def _kill(self, reason='cleanup'):
        try:
            if self.process.poll() is None and self.termination_reason is None:
                self.termination_reason = reason
            if os.name != 'nt' and self.container_cleanup is None:
                os.killpg(self.process.pid, signal.SIGKILL)
            else:
                self.process.kill()
        except (OSError, ProcessLookupError):
            pass

    def _stderr_failure(self, message):
        with self._failure_lock:
            if self.protocol_error is None:
                self.protocol_error = message
        self._kill('protocol_failure')

    def _read_stderr(self):
        try:
            stream = self.process.stderr.buffer
            while True:
                chunk = stream.read1(65536)
                if not chunk:
                    return
                remaining = max(0, self.stderr_limit_bytes-self.stderr_logged_bytes)
                if remaining:
                    written = chunk[:remaining]
                    self.log.write(written)
                    self.log.flush()
                    self.stderr_logged_bytes += len(written)
                if len(chunk) > remaining:
                    self.stderr_limit_exceeded = True
                    self._stderr_failure('participant stderr exceeds log limit')
                    return
        except (OSError, ValueError) as error:
            self._stderr_failure('participant stderr capture failed: '+type(error).__name__)

    def _capture_container_id(self):
        if not self.container_inspect_id or self.isolation_audit.get('container_id'):
            return
        from .sandbox_preflight import docker_environment
        try:
            result = subprocess.run(self.container_inspect_id, capture_output=True, text=True,
                                    timeout=3, env=docker_environment(),
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            identity = result.stdout.strip()
            if result.returncode == 0 and len(identity) == 64 and all(c in '0123456789abcdef' for c in identity):
                self.isolation_audit['container_id'] = identity
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    self.lines.put(AgentFailure("participant exited without response")); return
                if len(line.encode("utf-8")) > MAX_LINE or not line.endswith("\n"):
                    self.lines.put(AgentFailure("participant response exceeds line limit")); return
                self.lines.put(line)
        except Exception as error:
            self.lines.put(AgentFailure(str(error)))

    def request(self, payload, timeout=1.0):
        if self.broken: raise AgentFailure("participant transport already failed")
        began = time.monotonic()
        try:
            if self.protocol_error:
                raise AgentFailure(self.protocol_error)
            written = queue.Queue(maxsize=1)
            line = json.dumps(payload, allow_nan=False) + "\n"
            def write_request():
                try:
                    self.process.stdin.write(line)
                    self.process.stdin.flush()
                    written.put(None)
                except (ValueError, OSError) as error: written.put(error)
            threading.Thread(target=write_request, daemon=True).start()
            write_error = written.get(timeout=timeout)
            if write_error is not None: raise AgentFailure(str(write_error))
            remaining = timeout-(time.monotonic()-began)
            if remaining <= 0: raise queue.Empty
            result = self.lines.get(timeout=remaining)
            if self.protocol_error:
                raise AgentFailure(self.protocol_error)
            if isinstance(result, Exception): raise result
            parsed = strict_loads(result)
            if not isinstance(parsed, dict) or set(parsed) not in ({"protocol", "batch"}, {"protocol", "batch", "usage"}):
                raise AgentFailure("invalid response envelope")
            if parsed["protocol"] != PROTOCOL: raise AgentFailure("unsupported response protocol")
            if self.resource_group == "api" and "usage" not in parsed:
                raise AgentFailure("API group requires cumulative usage in every response")
            if "usage" in parsed:
                if self.resource_group != "api": raise AgentFailure("API usage requires api resource group")
                usage = parsed["usage"]
                required = {"calls", "input_tokens", "output_tokens", "cost_usd"}
                if not isinstance(usage, dict) or set(usage) != required: raise AgentFailure("invalid cumulative API usage")
                for key in required:
                    value = usage[key]
                    if key != "cost_usd":
                        if type(value) is not int:
                            raise AgentFailure("token/call counts must be integers")
                    elif type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
                        raise AgentFailure("invalid API usage value")
                    # Check integer bounds without converting arbitrary integers to float.
                    if not 0 <= value <= MAX_USAGE_VALUE:
                        raise AgentFailure("API usage value outside supported range")
                    if self.usage is not None and value < self.usage[key]: raise AgentFailure("API usage counters regressed")
                self.usage = usage
            return parsed["batch"]
        except AgentFailure:
            self.broken = True
            self._kill('protocol_failure')
            raise
        except queue.Empty as error:
            self.timeouts += 1
            # A late response cannot be reused as the next tick's decision.
            self.broken = True
            self._kill('decision_timeout')
            raise AgentFailure("participant decision timeout") from error
        except (ValueError, OSError) as error:
            self.broken = True
            self._kill('protocol_failure')
            raise AgentFailure(str(error)) from error
        finally:
            self.elapsed += time.monotonic() - began

    def close(self):
        clean = True
        try:
            self._capture_container_id()
            if self.process.poll() is None:
                def goodbye():
                    try:
                        self.process.stdin.write(json.dumps({'protocol': PROTOCOL, 'type': 'close'})+'\n')
                        self.process.stdin.flush()
                    except (OSError, ValueError):
                        pass
                threading.Thread(target=goodbye, daemon=True).start()
                try:
                    self.process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    self.termination_reason = self.termination_reason or 'close_timeout'
                    if os.name == 'nt':
                        subprocess.run(['taskkill.exe','/PID',str(self.process.pid),'/T','/F'],
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        self._kill('close_timeout')
                    self.process.wait(timeout=10)
            self.exit_code = self.process.poll()
            if (self.exit_code not in (None, 0)
                    and not getattr(self, 'termination_reason', None)
                    and not getattr(self, 'broken', False)):
                self.protocol_error = self.protocol_error or (
                    'participant exited with nonzero status: ' + str(self.exit_code))
            # A trusted Linux child can outlive its launcher; remove its owned
            # process group too. External descendants belong to the OCI cgroup.
            if os.name != 'nt' and not self.container_cleanup:
                self._kill()
            if self.container_cleanup:
                clean = self._cleanup_container() and clean
        except Exception as error:
            clean = False
            self.cleanup_error = 'participant cleanup failed: '+type(error).__name__
        finally:
            # Cleanup must be attempted even if waiting for the Docker client
            # raised. Never let a close exception discard the episode result.
            if self.container_cleanup and self.isolation_audit.get('cleanup_status') == 'pending':
                try:
                    clean = self._cleanup_container() and clean
                except Exception as error:
                    clean = False
                    self.cleanup_error = 'container cleanup failed: '+type(error).__name__
            self.stderr_thread.join(timeout=1)
            def close_pipes():
                for pipe in (self.process.stdin,self.process.stdout,self.process.stderr):
                    try:
                        pipe.close()
                    except (OSError,ValueError):
                        pass
            closer=threading.Thread(target=close_pipes,daemon=True)
            closer.start()
            closer.join(timeout=1)
            clean = clean and not closer.is_alive() and not self.stderr_thread.is_alive()
            if not clean and self.cleanup_error is None:
                self.cleanup_error = 'participant pipes or stderr writer did not close'
            # A blocked writer must never race with closing its log handle.
            if not self.stderr_thread.is_alive():
                try:
                    self.log.close()
                except OSError:
                    clean = False
                    self.cleanup_error = 'participant log close failed'
            self.isolation_audit.update(cleanup_status='passed' if clean else 'failed',
                cleanup_error=self.cleanup_error, stderr_logged_bytes=self.stderr_logged_bytes,
                stderr_limit_bytes=self.stderr_limit_bytes, stderr_limit_exceeded=self.stderr_limit_exceeded,
                protocol_error=self.protocol_error, exit_code=getattr(self, 'exit_code', None),
                termination_reason=getattr(self, 'termination_reason', None))
        return clean

    def _cleanup_container(self):
        from .sandbox_preflight import docker_environment
        options={'capture_output':True,'text':True,'timeout':15,'env':docker_environment(),
                 'creationflags':subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0}
        if not self.container_cleanup_check:
            self.cleanup_error = 'container absence cannot be verified'
            self.isolation_audit['cleanup_status'] = 'failed'
            return False
        before = subprocess.run(self.container_cleanup_check, **options)
        if before.returncode:
            self.cleanup_error = 'container cleanup inspection failed'
        elif before.stdout.strip():
            removed = subprocess.run(self.container_cleanup, **options)
            if removed.returncode:
                self.cleanup_error = 'docker rm failed with exit code '+str(removed.returncode)
            after = subprocess.run(self.container_cleanup_check, **options)
            if after.returncode or after.stdout.strip():
                self.cleanup_error = 'container remains or absence could not be verified'
        clean = self.cleanup_error is None
        self.isolation_audit['cleanup_status'] = 'passed' if clean else 'failed'
        return clean
