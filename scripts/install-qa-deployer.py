#!/usr/bin/env python3
"""Install the separate, one-shot QA deployment polling job on this Mac."""
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import sys


def main():
    home = Path.home()
    state = home / '.local/share/backdoor-deployer'
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for binary in ('gh', 'git', 'uv'):
        if not shutil.which(binary):
            raise SystemExit(f'Missing required command: {binary}')
    label = 'com.screddy.backdoor-deployer'
    plist = home / 'Library/LaunchAgents' / f'{label}.plist'
    service = home / 'projects/SRC/backdoor-service'
    router_plist = plist.with_name('com.screddy.backdoor-router.plist')
    if not service.is_dir() or not router_plist.is_file():
        raise SystemExit('The existing Mac router installation is required')
    path = ':'.join(dict.fromkeys([str(Path(shutil.which(b)).parent) for b in ('gh', 'git', 'uv')] + ['/usr/bin', '/bin', '/usr/sbin', '/sbin']))
    config = {
        'Label': label, 'RunAtLoad': True, 'StartInterval': 60,
        'ProgramArguments': [sys.executable, str(state / 'qa_deploy.py'),
            '--service', str(service), '--plist', str(router_plist),
            '--state', str(state), '--log', str(home / 'Library/Logs/backdoor-router.log')],
        'EnvironmentVariables': {'PATH': path, 'HOME': str(home)},
        'StandardOutPath': str(state / 'worker.log'),
        'StandardErrorPath': str(state / 'worker.log'),
    }
    domain = f'gui/{os.getuid()}'
    loaded = subprocess.run(['launchctl', 'print', f'{domain}/{label}'], capture_output=True)
    if loaded.returncode == 0:
        # Do not interrupt a deployment while upgrading its polling job.
        raise SystemExit('Deployer already installed; update its script between deployment transactions')
    source = Path(__file__).with_name('qa_deploy.py')
    temporary = state / 'qa_deploy.tmp'
    shutil.copyfile(source, temporary)
    temporary.chmod(0o700)
    temporary.replace(state / 'qa_deploy.py')
    plist.write_bytes(plistlib.dumps(config))
    subprocess.run(['launchctl', 'bootstrap', domain, str(plist)], check=True)
    print(f'Installed {label}; checks GitHub Deployments every 60 seconds')


if __name__ == '__main__':
    main()
