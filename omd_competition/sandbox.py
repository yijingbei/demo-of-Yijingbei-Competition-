"""Docker JSONL isolation; trusted local debugging remains a separate path."""
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import uuid


@dataclass
class ContainerLaunch:
    command: list[str]
    cleanup: list[str]
    cleanup_check: list[str] | None = None
    inspect_id: list[str] | None = None
    metadata: dict = field(default_factory=dict)
    environment: dict | None = field(default=None,repr=False)

    def new_instance(self):
        """A declaration reused by calibration still creates distinct containers."""
        if '--name' not in self.command:
            return self
        name = 'openmd-agent-' + uuid.uuid4().hex
        command = list(self.command)
        old = command[command.index('--name') + 1]
        command[command.index('--name') + 1] = name
        def renamed(arguments):
            return [a.replace(old, name) for a in arguments] if arguments else None
        return ContainerLaunch(command, renamed(self.cleanup), renamed(self.cleanup_check),
                               renamed(self.inspect_id), {**self.metadata, 'container_name': name},
                               dict(self.environment) if self.environment is not None else None)


def container_launch(data, submission_directory, group, *, preflight=None, environment=None):
    from .sandbox_preflight import POLICY, docker_command, official_required, validate_image
    sandbox=data['sandbox']
    if not isinstance(sandbox,dict) or set(sandbox)-{'backend','image','entrypoint','environment_names'} or sandbox.get('backend')!='docker':
        raise ValueError('unsupported sandbox configuration')
    image=sandbox.get('image','')
    if not isinstance(image,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:-]*@sha256:[0-9a-f]{64}',image):
        raise ValueError('container image must use an immutable sha256 digest')
    entrypoint=sandbox.get('entrypoint')
    if not isinstance(entrypoint,list) or not entrypoint or not all(isinstance(a,str) and a and '\x00' not in a for a in entrypoint):
        raise ValueError('container entrypoint must be an argument array')
    if group not in {'cpu','api'}:raise ValueError('unknown container resource group')
    docker=docker_command()
    if official_required() and preflight is None:
        raise ValueError('official evaluation requires verified sandbox preflight evidence')
    if preflight is not None:
        validate_image(image)
    directory=Path(submission_directory).resolve()
    if not directory.is_dir() or any(c in str(directory) for c in (',','\n','\r')):
        raise ValueError('submission directory is absent or contains unsafe mount separators')
    name='openmd-agent-'+uuid.uuid4().hex
    network='none' if group=='cpu' else os.environ.get('OPENMD_API_DOCKER_NETWORK')
    if not network:raise ValueError('API containers require an organizer-managed proxy network')
    if group=='api' and (network in {'host','bridge','none'} or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',network)):
        raise ValueError('API containers require a dedicated organizer-managed proxy network')
    variables=sandbox.get('environment_names',[])
    if not isinstance(variables,list) or not all(isinstance(v,str) and re.fullmatch(r'[A-Z][A-Z0-9_]*',v) for v in variables) or len(set(variables))!=len(variables):
        raise ValueError('environment_names must be distinct environment variable names')
    if group!='api' and variables:
        raise ValueError('only API containers may receive explicitly named environment variables')
    approved=environment or {}
    if set(variables)-set(approved):raise ValueError('API environment variable is not approved for this team')
    command=[*docker,'run','--rm','--interactive','--name',name,
        '--pull','never',
        '--label','openmd.competition=external-agent','--network',network,
        '--cpus','2','--memory','2g','--memory-swap','2g','--pids-limit','128','--cap-drop','ALL',
        '--security-opt','no-new-privileges','--read-only','--user','65534:65534',
        '--log-driver','none',
        '--tmpfs','/tmp:rw,nosuid,nodev,size=64m',
        '--mount',f'type=bind,source={directory},target=/submission,readonly',
        '--workdir','/submission','--entrypoint',entrypoint[0]]
    for variable in variables:
        command += ['--env',variable]
    command += [image,*entrypoint[1:]]
    audit={'execution_kind':'external_container','external_sandbox_verified':preflight is not None,
           'container_name':name,'image_digest':image,'resource_policy':dict(POLICY),'preflight':preflight}
    return ContainerLaunch(command,[*docker,'rm','--force',name],
        [*docker,'ps','--all','--quiet','--filter',f'name=^/{name}$'],
        [*docker,'inspect','--format','{{.Id}}',name],audit,{v:approved[v] for v in variables})
