import fcntl
import os
import select
import shlex
import socket
import sys
import termios
import tty
from pathlib import Path

operation, project, resume = sys.argv[1:]
workspace = Path('/guest')
ready_marker = b'\x1b]9999;agent-docker-shell-ready\x07'
checkpoint = workspace / 'console.pending'
serial_log = workspace / 'console.log'
commands = {
    'claude-auth': ['claude', 'auth', 'login'],
    'claude-logout': ['claude', 'auth', 'logout'],
    'claude': ['claude'] + (['--resume', resume] if resume else []),
}


def shell_is_ready():
    offset = int(checkpoint.read_text()) if checkpoint.exists() else 0
    with serial_log.open('rb') as log:
        end = log.seek(0, os.SEEK_END)
        log.seek(max(offset, end - 65536))
        return ready_marker in log.read()


def send_input(guest, data):
    checkpoint.write_text(str(serial_log.stat().st_size))
    guest.sendall(data)


if not sys.stdin.isatty():
    raise SystemExit('Запуск гостевой консоли требует интерактивного терминала.')
if project and (project.startswith('/') or '..' in project.split('/')):
    raise SystemExit('PROJECT должен быть путём относительно ~/projects.')
with (workspace / 'console.lock').open('a') as lock, socket.socket(socket.AF_UNIX) as guest:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit('Консоль этого гостя уже открыта.')
    if operation != 'vm-console' and not shell_is_ready():
        raise SystemExit('Автозапуск ждёт новый вход в Bash. Подключитесь через make vm-console и выйдите из Claude или Bash.')
    guest.connect(str(workspace / 'serial.sock'))
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        if operation != 'vm-console':
            size = os.get_terminal_size(fd)
            command = f'stty rows {max(size.lines, 24)} cols {max(size.columns, 80)}; export DISABLE_UPDATES=1; mkdir -p "$HOME/projects"; cd "$HOME/projects"'
            if project:
                command += ' && cd -- ' + shlex.quote(project)
            command += ' && exec ' + shlex.join(commands[operation]) + '; exit'
            send_input(guest, (command + '\r').encode())
        print('\r\nДля выхода из консоли нажмите Ctrl+].\r', flush=True)
        while True:
            readable, _, _ = select.select([guest, fd], [], [])
            if guest in readable:
                data = guest.recv(65536)
                if not data:
                    break
                os.write(sys.stdout.fileno(), data)
            if fd in readable:
                data = os.read(fd, 1024)
                if not data:
                    break
                before, escape, _ = data.partition(b'\x1d')
                if before:
                    send_input(guest, before)
                if escape:
                    break
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
