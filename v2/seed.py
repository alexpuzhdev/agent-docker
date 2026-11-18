import ipaddress
import json
import os
import re
from pathlib import Path

seed = Path('/guest/seed')
seed.mkdir(exist_ok=True)
name = os.environ['VM_NAME']
ip = str(ipaddress.IPv4Address(os.environ['GATEWAY_IP']))
hosts = [f'{ip} git.agent-docker.test\n']
for line in Path('/config/allowed-domains').read_text().splitlines():
    domain = line.partition('#')[0].strip().lower()
    if not domain:
        continue
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*', domain):
        raise SystemExit('В allowed-domains ожидается одно полное доменное имя на строку.')
    hosts.append(f'{ip} {domain}\n')
(seed / 'allowed-hosts').write_text(''.join(hosts))
(seed / 'meta-data').write_text(f'instance-id: agent-docker-{name}\nlocal-hostname: {name}\n')

source = 'https://' + os.environ['CLAUDE_DOWNLOAD_HOST']
debian = 'https://' + 'deb.debian.org'
files = {
    '/etc/profile.d/agent-docker-serial.sh': (
        '0644', Path('/opt/agent-docker/serial-profile.sh').read_text()
    ),
    '/usr/local/sbin/agent-docker-prepare': (
        '0755', Path('/opt/agent-docker/prepare-guest.sh').read_text()
    ),
    '/etc/apt/claude-code.asc': ('0644', Path('/images/claude-code.asc').read_text()),
    '/etc/apt/agent-docker.list': ('0644',
        f'deb [signed-by=/etc/apt/claude-code.asc] {source}/claude-code/apt/stable stable main\n'
        f'deb {debian}/debian trixie main\n'
        f'deb {debian}/debian-security trixie-security main\n'),
    '/etc/environment.d/90-claude.conf': ('0644', 'DISABLE_UPDATES=1\n'),
    '/etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf': ('0644',
        '[Service]\nExecStart=\nExecStart=-/sbin/agetty --autologin agent --noclear %I $TERM\n'),
    '/etc/systemd/system/agent-docker-prepare.service': ('0644',
        '[Unit]\nWants=network-online.target\nAfter=network-online.target\n'
        '[Service]\nType=oneshot\nExecStart=/usr/local/sbin/agent-docker-prepare\n'
        'StandardOutput=journal+console\nStandardError=journal+console\n'
        '[Install]\nWantedBy=multi-user.target\n'),
}
config = {
    'users': [{'name': 'agent', 'shell': '/bin/bash', 'lock_passwd': True}],
    'ssh_pwauth': False,
    'package_update': False,
    'package_upgrade': False,
    'write_files': [
        {'path': path, 'permissions': mode, 'content': content}
        for path, (mode, content) in files.items()
    ],
    'runcmd': [
        ['systemctl', 'daemon-reload'],
        ['systemctl', 'enable', '--now', 'agent-docker-prepare.service'],
        ['systemctl', 'restart', 'serial-getty@ttyS0.service'],
    ],
}
(seed / 'user-data').write_text('#cloud-config\n' + json.dumps(config) + '\n')
