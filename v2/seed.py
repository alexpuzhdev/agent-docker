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
# instance-id постоянен намеренно. Его смена заставляет cloud-init считать
# гостя новым и повторно применить write_files и runcmd, а вместе с этим
# срабатывают и другие модули: на копии старого диска так сгенерировался новый
# ключ хоста ED25519 и изменился его отпечаток. Поэтому существующим гостям
# канал сессий ставится отдельным узким шагом — v2/install-sessions-guest.sh,
# — а не повторным провижинированием.
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
    # Правило udev и юнит лежат отдельными файлами: те же самые ставит
    # install-sessions-guest.sh существующему гостю, и расходиться им нельзя.
    '/etc/udev/rules.d/60-atrice-control.rules': (
        '0644', Path('/opt/agent-docker/atrice-control.rules').read_text()
    ),
    '/usr/local/lib/atrice/sessions-responder.py': (
        '0755', Path('/opt/agent-docker/sessions-responder.py').read_text()
    ),
    '/etc/systemd/system/atrice-sessions.service': (
        '0644', Path('/opt/agent-docker/atrice-sessions.service').read_text()
    ),
    # Каталог сокета tmux создаётся заранее и общий для агентов и службы: сама
    # служба создать его не может, у неё ProtectSystem=strict.
    '/usr/lib/tmpfiles.d/atrice-sessions.conf': (
        '0644', Path('/opt/agent-docker/atrice-sessions.tmpfiles.conf').read_text()
    ),
    '/etc/profile.d/atrice-tmux.sh': (
        '0644', Path('/opt/agent-docker/atrice-tmux-profile.sh').read_text()
    ),
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
        # Правило перечитывается и применяется к уже созданному порту: без
        # trigger права останутся прежними до следующей загрузки.
        ['udevadm', 'control', '--reload'],
        ['udevadm', 'trigger', '--subsystem-match=virtio-ports'],
        # Каталог сокета готовится до запуска службы и агентов: при первой
        # загрузке tmpfiles уже отработал, и файл нужно применить вручную.
        ['systemd-tmpfiles', '--create', '/usr/lib/tmpfiles.d/atrice-sessions.conf'],
        ['systemctl', 'enable', '--now', 'atrice-sessions.service'],
        ['systemctl', 'enable', '--now', 'agent-docker-prepare.service'],
        ['systemctl', 'restart', 'serial-getty@ttyS0.service'],
    ],
}
(seed / 'user-data').write_text('#cloud-config\n' + json.dumps(config) + '\n')
