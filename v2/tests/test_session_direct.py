"""Прямой вход в управляемую сессию: сквозная проверка обеих половин.

Стенд повторяет настоящую связку целиком, кроме tmux и QEMU. Запускаются
настоящие v2/session-responder.py (гостевая сторона) и v2/session.py (сторона
контейнера); между ними — мост, который делает то же, что порт virtio: копирует
байты между сокетом хоста и устройством гостя. Устройством служит pty в
неформатированном режиме, сокетом — обычный AF_UNIX, как у QEMU.

tmux подставной, но ведёт себя как настоящий в том, что здесь проверяется:
new-session -A подключается к существующей сессии и создаёт отсутствующую,
клиент живёт отдельно от сессии, SIGHUP отсоединяет клиента, сессия и её
процесс остаются жить, а SIGWINCH клиент видит и записывает новый размер.
Serial-консоли в стенде нет вовсе: её сокет создаётся пустым, и проверяется,
что в него не пришло ни байта.
"""
import importlib.util
import json
import os
import pty
import select
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
RESPONDER = ROOT / 'v2' / 'session-responder.py'
HOST = ROOT / 'v2' / 'session.py'
FRAME_DATA = 1
FRAME_CONTROL = 2

FAKE_TMUX = '''#!/usr/bin/env python3
"""Подставной tmux: ровно то поведение, на которое опирается служба.

Клиент переводит свой терминал в неформатированный режим, как настоящий: без
этого канонический режим придерживал бы ввод до перевода строки и добавлял эхо
ядра, и проверка зависела бы от момента. Pid агента записывает родитель, а не
сам агент: иначе файл появлялся бы позже, чем сессия в реестре.
"""
import os, signal, sys, time, tty
from pathlib import Path

work = Path(os.environ['FAKE_TMUX_STATE'])
registry = work / 'sessions'
(work / 'argv.log').open('a').write(' '.join(sys.argv[1:]) + '\\n')
names = registry.read_text().split() if registry.exists() else []

def known(name):
    return name in names

if 'show-options' in sys.argv:
    raise SystemExit(0 if (work / 'server').exists() else 1)

if 'has-session' in sys.argv:
    target = sys.argv[-1].lstrip('=')
    raise SystemExit(0 if known(target) else 1)

if 'new-session' not in sys.argv:
    raise SystemExit('подставной tmux такого не умеет: %s' % sys.argv[1:])

name = sys.argv[sys.argv.index('-s') + 1]
if not known(name):
    # Сессия с живущим в ней агентом: он переживает уход клиента.
    agent = os.fork()
    if agent == 0:
        os.setsid()
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        while True:
            time.sleep(3600)
    (work / ('agent-%s.pid' % name)).write_text(str(agent))
    names.append(name)
    registry.write_text('\\n'.join(names) + '\\n')
    first = '[создана %s]\\r\\n' % name
else:
    first = '[подключена %s]\\r\\n' % name


def note_size(number, frame):
    window = os.get_terminal_size(sys.stdin.fileno())
    (work / 'size.log').open('a').write('%dx%d\\n' % (window.columns, window.lines))


def attach_note(add):
    path = work / 'attached'
    names = path.read_text().split() if path.exists() else []
    if add and name not in names:
        names.append(name)
    if not add and name in names:
        names.remove(name)
    path.write_text('\\n'.join(names) + ('\\n' if names else ''))


def hangup(number, frame):
    attach_note(False)
    (work / 'client.log').open('a').write('hup %s\\n' % name)
    raise SystemExit(0)


tty.setraw(sys.stdin.fileno())
attach_note(True)
signal.signal(signal.SIGWINCH, note_size)
signal.signal(signal.SIGHUP, hangup)
note_size(0, None)
sys.stdout.write(first)
sys.stdout.flush()
while True:
    try:
        data = os.read(sys.stdin.fileno(), 1024)
    except (OSError, InterruptedError):
        continue
    if not data:
        break
    if data == b'MOUSE':
        os.write(sys.stdout.fileno(), b'\\x1b[?7;10')
        time.sleep(0.1)
        os.write(sys.stdout.fileno(), b'00;1006h' + 'кириллица'.encode())
        continue
    sys.stdout.write('эхо:' + data.decode('utf-8', 'replace'))
    sys.stdout.flush()
'''


class Bridge:
    """Порт virtio в миниатюре: сокет хоста ↔ устройство гостя."""

    def __init__(self, path):
        self.path = path
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(path)
        self.listener.listen()
        self.master, self.slave = pty.openpty()
        for descriptor in (self.master, self.slave):
            tty.setraw(descriptor)
        self.device = os.ttyname(self.slave)
        self.stop = threading.Event()
        self.link = None
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        # Соединения обслуживаются по одному, как и у порта virtio: второй
        # читатель устройства уводил бы кадры у действующего клиента.
        while not self.stop.is_set():
            try:
                self.listener.settimeout(0.2)
                link, _ = self.listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            self.link = link
            closed = threading.Event()
            threads = [threading.Thread(target=self.pump, args=(link, closed), daemon=True),
                       threading.Thread(target=self.drain, args=(link, closed), daemon=True)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.link = None
            try:
                link.close()
            except OSError:
                pass

    def pump(self, link, closed):
        while not (self.stop.is_set() or closed.is_set()):
            readable, _, _ = select.select([link], [], [], 0.2)
            if not readable:
                continue
            try:
                data = link.recv(65536)
            except OSError:
                break
            if not data:
                break
            try:
                os.write(self.master, data)
            except OSError:
                break
        closed.set()

    def drain(self, link, closed):
        while not (self.stop.is_set() or closed.is_set()):
            readable, _, _ = select.select([self.master], [], [], 0.2)
            if not readable:
                continue
            try:
                data = os.read(self.master, 65536)
            except OSError:
                break
            if not data:
                break
            try:
                link.sendall(data)
            except OSError:
                break
        closed.set()

    def close(self):
        self.stop.set()
        for closing in (self.listener, self.link):
            try:
                if closing:
                    closing.close()
            except OSError:
                pass
        for descriptor in (self.master, self.slave):
            try:
                os.close(descriptor)
            except OSError:
                pass


class DirectSessionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.state = self.work / 'tmux'
        self.state.mkdir()
        self.tmux = self.work / 'tmux-stub'
        self.tmux.write_text(FAKE_TMUX)
        self.tmux.chmod(0o755)
        self.socket_path = str(self.work / 'session.sock')
        # Пустой сокет serial-консоли: в него не должно уйти ни байта.
        self.serial = socket.socket(socket.AF_UNIX)
        self.addCleanup(self.serial.close)
        self.serial.bind(str(self.work / 'serial.sock'))
        self.serial.listen()
        self.bridge = None
        self.responder = None

    def start_guest(self, timeout='5', server=True):
        """Поднимает мост и настоящую гостевую службу на его устройстве."""
        if server:
            (self.state / 'server').touch()
        self.bridge = Bridge(self.socket_path)
        self.addCleanup(self.bridge.close)
        self.responder = subprocess.Popen(
            [sys.executable, '-I', str(RESPONDER)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=dict(os.environ, AGENT_DOCKER_SESSION_PORT=self.bridge.device,
                     AGENT_DOCKER_TMUX=str(self.tmux),
                     AGENT_DOCKER_TMUX_SOCKET_PATH=str(self.state / 'atrice'),
                     AGENT_DOCKER_SESSION_TIMEOUT=timeout,
                     FAKE_TMUX_STATE=str(self.state)))
        self.addCleanup(self.responder.stdout.close)
        self.addCleanup(self.stop_guest)
        return self.responder

    def stop_guest(self):
        if self.responder and self.responder.poll() is None:
            self.responder.kill()
            self.responder.wait(timeout=5)
        for pid_file in self.state.glob('agent-*.pid'):
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    def environment(self):
        return dict(os.environ, AGENT_DOCKER_SESSION_SOCKET=self.socket_path)

    def open_session(self, name, token='', *, mouse='app'):
        """Запускает хостовую половину на своём pty."""
        terminal, child = pty.openpty()
        self.resize(terminal, 40, 120)
        values = self.environment()
        values['AGENT_DOCKER_CONSOLE_MOUSE'] = mouse
        if token:
            values['AGENT_DOCKER_SESSION_TOKEN'] = token
        process = subprocess.Popen(
            [sys.executable, '-E', '-s', str(HOST), 'open', '', name],
            stdin=child, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=values)
        os.close(child)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process, terminal

    def release(self, token):
        """Освобождение с хоста: так прибирает за собой ловушка manage.sh."""
        run = subprocess.run(
            [sys.executable, '-E', '-s', str(HOST), '--release', '', token],
            capture_output=True, text=True, timeout=30, env=self.environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        return run

    def resize(self, terminal, lines, columns):
        import fcntl
        fcntl.ioctl(terminal, termios.TIOCSWINSZ,
                    struct.pack('HHHH', lines, columns, 0, 0))

    def probe(self):
        run = subprocess.run([sys.executable, '-E', '-s', str(HOST), 'probe'],
                             capture_output=True, text=True, timeout=20,
                             env=self.environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def ask(self, request, timeout=10, *, preceding=b''):
        """Одно управляющее сообщение службе напрямую, без хостовой половины.

        Ответ опознаётся по метке запроса, как это делает и хостовая половина:
        в порту может лежать кадр прошлой сессии (например её closed), и
        принять его за ответ своему запросу нельзя.
        """
        label = os.urandom(8).hex()
        with socket.socket(socket.AF_UNIX) as link:
            link.connect(self.socket_path)
            payload = json.dumps(dict(request, id=label)).encode()
            link.sendall(preceding + struct.pack('>BI', FRAME_CONTROL, len(payload)) + payload)
            link.settimeout(timeout)
            buffer = b''
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if len(buffer) >= 5:
                    kind, length = struct.unpack('>BI', buffer[:5])
                    if len(buffer) >= 5 + length:
                        message = buffer[5:5 + length]
                        buffer = buffer[5 + length:]
                        if kind != FRAME_CONTROL:
                            continue
                        answer = json.loads(message.decode())
                        if answer.get('id') not in (None, label):
                            continue
                        return answer
                chunk = link.recv(65536)
                if not chunk:
                    self.fail('служба закрыла порт без ответа')
                buffer += chunk
            self.fail('служба не ответила на запрос с меткой %s' % label)

    def read_until(self, process, needle, timeout=15):
        """Читает вывод до нужного куска, не залипая на пустом потоке.

        Чтение идёт через select: read1 на живом канале без данных ждёт
        бесконечно, и срок проверки тогда не наступает никогда.
        """
        collected = bytearray()
        deadline = time.monotonic() + timeout
        while needle not in collected and time.monotonic() < deadline:
            if not select.select([process.stdout], [], [], 0.2)[0]:
                continue
            chunk = process.stdout.read1(4096)
            if not chunk:
                break
            collected += chunk
        self.assertIn(needle, bytes(collected), bytes(collected))
        return bytes(collected)

    def wait_exit(self, process, timeout=5):
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close_terminal(self, terminal):
        try:
            os.close(terminal)
        except OSError:
            pass

    def sessions(self):
        registry = self.state / 'sessions'
        return registry.read_text().split() if registry.exists() else []

    def attached(self, name):
        """Подключён ли клиент к сессии по данным подставного tmux."""
        attached = self.state / 'attached'
        return name in (attached.read_text().split() if attached.exists() else [])

    def alive(self, name):
        pid_file = self.state / ('agent-%s.pid' % name)
        if not pid_file.exists():
            return False
        try:
            os.kill(int(pid_file.read_text()), 0)
        except OSError:
            return False
        return True

    def wait_gone(self, pattern, timeout=10):
        """Ждёт условия: файлы стенда появляются не мгновенно."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if pattern():
                    return True
            except (OSError, ValueError):
                pass
            time.sleep(0.05)
        return False

    def assertSerialUntouched(self):
        self.serial.settimeout(0.2)
        try:
            self.serial.accept()
        except (TimeoutError, socket.timeout):
            return
        self.fail('прямой вход не должен касаться serial-консоли')

    def test_creates_opens_and_reenters_without_serial(self):
        """Сценарий целиком: создать, открыть, выйти, вернуться, вторая сессия.

        Это и есть то, чего не умел прежний путь: сессия не просто создаётся в
        фоне, а сразу открывается, и всё это не касается serial-оболочки.
        """
        self.start_guest()
        self.assertEqual(self.probe(), {'api': 1, 'direct': 'ready'})

        first = 'claude-1111222233334444'
        process, terminal = self.open_session(first)
        self.read_until(process, 'Сессия создана'.encode())
        self.read_until(process, ('[создана %s]' % first).encode())
        self.assertEqual(self.sessions(), [first])
        self.assertTrue(self.alive(first))
        # Набранное доходит до клиента именно этой сессии.
        os.write(terminal, b'hello\r')
        self.read_until(process, 'эхо:hello'.encode())
        # Размер окна доходит до клиента живой сессии: pty здесь наш.
        self.resize(terminal, 50, 200)
        process.send_signal(signal.SIGWINCH)
        self.assertTrue(self.wait_gone(
            lambda: '200x50' in (self.state / 'size.log').read_text()),
            (self.state / 'size.log').read_text())
        # Ctrl+]: клиент отсоединён, сессия и агент живы.
        os.write(terminal, b'\x1d')
        self.assertEqual(process.wait(timeout=15), 0)
        self.assertTrue(self.wait_gone(
            lambda: 'hup %s' % first in (self.state / 'client.log').read_text()))
        self.assertEqual(self.sessions(), [first])
        self.assertTrue(self.alive(first))

        # Повторный вход в то же имя подключается, а не создаёт дубль.
        again, terminal = self.open_session(first)
        self.read_until(again, 'Сессия подключена'.encode())
        self.read_until(again, ('[подключена %s]' % first).encode())
        self.assertEqual(self.sessions(), [first])
        os.write(terminal, b'\x1d')
        self.assertEqual(again.wait(timeout=15), 0)

        # Вторая сессия рядом: обе живы и обе видны, прежний агент работает.
        second = 'claude-5555666677778888'
        other, terminal = self.open_session(second)
        self.read_until(other, ('[создана %s]' % second).encode())
        self.assertEqual(sorted(self.sessions()), sorted([first, second]))
        self.assertTrue(self.alive(first))
        self.assertTrue(self.alive(second))
        os.write(terminal, b'\x1d')
        self.assertEqual(other.wait(timeout=15), 0)
        self.assertEqual(sorted(self.sessions()), sorted([first, second]))
        # За весь сценарий в serial-консоль не ушло ни байта.
        self.assertSerialUntouched()

    def test_mouse_policy_preserves_native_selection(self) -> None:
        self.start_guest()
        for mouse, expected in (
            ('off', b'\x1b[?7h'),
            ('app', b'\x1b[?7;1000;1006h'),
        ):
            with self.subTest(mouse=mouse):
                process, terminal = self.open_session('claude-1111222233334444', mouse=mouse)
                self.addCleanup(self.close_terminal, terminal)
                try:
                    self.read_until(process, b'claude-1111222233334444]')
                    os.write(terminal, b'MOUSE')
                    result = self.read_until(process, 'кириллица'.encode())
                    self.assertIn(expected + 'кириллица'.encode(), result)
                finally:
                    os.write(terminal, b'\x1d')
                    self.assertEqual(process.wait(timeout=15), 0)
                with process.stdout:
                    self.assertIn(b'\x1b[?1000l', process.stdout.read())

    def test_surviving_relay_is_released_by_token(self):
        """Реле, пережившее своего клиента, освобождается с хоста по токену.

        Это и есть живой блокер: хостовый PTY закрыли без Ctrl+], клиент
        docker exec ушёл, а реле внутри контейнера осталось работать. Изнутри
        его уход не виден, keepalive продолжался, гостевая служба держала
        клиента tmux подключённым, сессия висела attached, а проверка пути
        отвечала silent — до ручного завершения процесса. Прежний тест смерти
        самого реле этого не воспроизводил.

        Проверяется весь путь: блокер воспроизводится, освобождение по токену
        его снимает без ручного kill, сессия и агент остаются живы, повторный
        вход проходит. Чужое живое подключение освобождение не трогает.
        """
        self.start_guest(timeout='30')
        token = 'a' * 32
        name = 'claude-7777888899990000'
        process, terminal = self.open_session(name, token=token)
        self.read_until(process, ('[создана %s]' % name).encode())
        self.assertEqual(self.sessions(), [name])

        # Клиент ушёл, а устройство терминала осталось открытым у нас: именно
        # так выглядит уход docker exec изнутри контейнера.
        self.assertIsNone(process.poll())
        self.assertEqual(self.probe(), {'api': 1, 'direct': 'silent'})
        self.assertTrue(self.attached(name))

        # Освобождение с чужим токеном ничего не делает.
        self.release('b' * 32)
        self.assertIsNone(process.poll())
        self.assertTrue(self.attached(name))

        # Освобождение с нашим токеном завершает ровно это реле.
        self.release(token)
        self.assertEqual(self.wait_exit(process, 15), 0)
        self.assertTrue(self.wait_gone(
            lambda: 'hup %s' % name in (self.state / 'client.log').read_text(), 15))
        self.assertTrue(self.wait_gone(lambda: not self.attached(name), 15))
        self.assertTrue(self.wait_gone(
            lambda: self.probe() == {'api': 1, 'direct': 'ready'}, 15))
        self.assertEqual(self.sessions(), [name])
        self.assertTrue(self.alive(name))

        # Повторный вход проходит без ручного завершения чего-либо.
        again, terminal = self.open_session(name, token='c' * 32)
        self.read_until(again, ('[подключена %s]' % name).encode())
        os.write(terminal, b'\x1d')
        self.assertEqual(self.wait_exit(again, 15), 0)

    def test_signal_detaches_client_at_once(self):
        """TERM и HUP уводят реле и сразу отсоединяют клиента сессии.

        Это путь ловушки manage.sh на самом хосте: ждать срока keepalive не
        нужно, служба получает кадр detach от уходящего реле.
        """
        self.start_guest(timeout='30')
        name = 'claude-1212343456567878'
        for number in (signal.SIGTERM, signal.SIGHUP):
            process, terminal = self.open_session(name)
            self.read_until(process, b'[')
            process.send_signal(number)
            self.assertEqual(self.wait_exit(process, 15), 0)
            self.assertTrue(self.wait_gone(lambda: not self.attached(name), 15))
            self.assertTrue(self.wait_gone(
                lambda: self.probe() == {'api': 1, 'direct': 'ready'}, 15))
            self.assertEqual(self.sessions(), [name])
            self.assertTrue(self.alive(name))
            self.close_terminal(terminal)

    def test_only_fixed_command_and_valid_names(self):
        """Произвольных команд путь не передаёт, имя проверяется.

        В запросе есть только имя по контракту канала и размеры окна, а argv
        клиента фиксирован в службе: подставной tmux записывает всё, с чем его
        звали, и там только проверка сервера, has-session и new-session -A.
        """
        self.start_guest()
        name = 'claude-9999aaaabbbbcccc'
        process, terminal = self.open_session(name)
        self.read_until(process, ('[создана %s]' % name).encode())
        os.write(terminal, b'\x1d')
        self.assertEqual(process.wait(timeout=15), 0)
        calls = (self.state / 'argv.log').read_text().splitlines()
        socket_path = str(self.state / 'atrice')
        self.assertEqual(calls, [
            '-N -S %s show-options -s exit-empty' % socket_path,
            '-N -S %s has-session -t =%s' % (socket_path, name),
            '-N -S %s new-session -A -s %s -- claude' % (socket_path, name)])
        for bad in ('', 'имя с пробелом', 'claude;rm -rf /', '-с-дефиса', 'a' * 65):
            refused, _ = self.open_session(bad)
            self.read_until(refused, 'Имя сессии не подходит'.encode())
            self.assertEqual(refused.wait(timeout=15), 1)
        self.assertEqual((self.state / 'argv.log').read_text().splitlines(), calls)
        # Операция в кадре типизирована: ничего, кроме ping и open, служба не
        # принимает, а произвольной команды в протоколе нет вовсе.
        for request in ({'api': 1, 'op': 'exec', 'cmd': 'rm -rf /'},
                        {'api': 1, 'op': 'send-keys', 'session': name, 'keys': 'q'},
                        {'api': 2, 'op': 'open', 'session': name}):
            answer = self.ask(request)
            self.assertIs(answer.get('ok'), False, answer)
            self.assertIn(answer.get('error'),
                          ('операция не поддержана', 'версия протокола не та'), answer)
        self.assertEqual((self.state / 'argv.log').read_text().splitlines(), calls)

    def test_absent_and_silent_paths_are_distinct(self):
        """Нет порта и нет службы — разные состояния, и оба не ломают вход."""
        # Порта нет вовсе: гость запущен прежней сборкой.
        self.assertEqual(self.probe(), {'api': 1, 'direct': 'absent'})
        process, _ = self.open_session('claude-0000111122223333')
        self.read_until(process, 'Прямого пути в сессии у этого гостя нет'.encode())
        self.assertEqual(process.wait(timeout=15), 1)
        # Порт есть, службы нет: отвечать некому.
        self.bridge = Bridge(self.socket_path)
        self.addCleanup(self.bridge.close)
        self.assertEqual(self.probe(), {'api': 1, 'direct': 'silent'})

    def test_first_request_after_disconnected_host_is_answered(self):
        self.check_first_request_after_disconnect()

    def test_first_request_buffered_after_detach_is_answered(self):
        payload = b'{"api":1,"op":"detach"}'
        detach = struct.pack('>BI', FRAME_CONTROL, len(payload)) + payload
        self.check_first_request_after_disconnect(detach)

    def check_first_request_after_disconnect(self, preceding=b''):
        self.start_guest(timeout='30')
        name = 'claude-0111222233334444'
        process, terminal = self.open_session(name)
        self.addCleanup(process.stdout.close)
        try:
            self.read_until(process, ('[создана %s]' % name).encode())
            process.kill()
            process.wait(timeout=5)
        finally:
            os.close(terminal)

        response = self.ask({'api': 1, 'op': 'ping'}, timeout=4, preceding=preceding)
        self.assertIs(response.get('ok'), True)
        self.assertTrue(self.alive(name))
        self.assertFalse(self.attached(name))

    def test_lost_open_acknowledgement_releases_tmux_client(self):
        spec = importlib.util.spec_from_file_location('session_responder', RESPONDER)
        responder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(responder)
        port = Mock()
        port.take.return_value = (FRAME_CONTROL, b'{"api":1,"op":"open","session":"claude-1234"}')
        port.send.side_effect = responder.Gone
        with patch.object(responder, 'tmux_check', return_value=True), \
             patch.object(responder, 'start_client', return_value=(42, 17)), \
             patch.object(responder, 'finish') as finish:
            with self.assertRaises(responder.Gone):
                responder.serve(port)
        finish.assert_called_once_with(42, 17)

    @unittest.skipUnless(shutil.which('tmux'), 'нужен настоящий tmux')
    def test_tmux_server_unit_starts_cold_and_preserves_sessions(self):
        unit = (ROOT / 'v2' / 'atrice-tmux-server.service').read_text()
        command = next(line.removeprefix('ExecStart=') for line in unit.splitlines()
                       if line.startswith('ExecStart='))
        tmux, flag, _, *arguments = shlex.split(command)
        socket_path = str(self.state / 'atrice')
        server = [shutil.which(tmux), flag, socket_path]
        self.addCleanup(subprocess.run, [*server, 'kill-server'], check=False,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        subprocess.run([*server, *arguments], check=True, timeout=5)
        name = 'atrice-unit-check'
        subprocess.run([*server, '-N', 'new-session', '-d', '-s', name, '--', 'sleep', '30'],
                       check=True, timeout=5)
        subprocess.run([*server, *arguments], check=True, timeout=5)
        subprocess.run([*server, '-N', 'has-session', '-t', '=' + name], check=True, timeout=5)
        subprocess.run([*server, '-N', 'kill-session', '-t', '=' + name], check=True, timeout=5)
        result = subprocess.run([*server, '-N', 'show-options', '-s', 'exit-empty'],
                                capture_output=True, text=True, check=True, timeout=5)
        self.assertEqual(result.stdout.strip(), 'exit-empty off')

    def test_missing_tmux_server_keeps_serial_fallback(self):
        self.start_guest(server=False)
        self.assertEqual(self.probe(), {'api': 1, 'direct': 'silent'})
        name = 'claude-0123456789abcdef'
        response = self.ask({'api': 1, 'op': 'open', 'session': name})
        self.assertIs(response.get('ok'), False)
        self.assertIn('сервер tmux недоступен', response.get('error'))
        self.assertNotIn('new-session', (self.state / 'argv.log').read_text())

    def test_lost_host_detaches_client_and_keeps_session(self):
        """Уход хоста без Ctrl+] отсоединяет клиента, сессия остаётся жить.

        Пропажа keepalive — единственный признак ухода: изнутри гостя уход
        клиента на порту virtio не виден. Сессия и агент при этом не трогаются.
        """
        self.start_guest(timeout='2')
        name = 'claude-ddddeeeeffff0000'
        process, terminal = self.open_session(name)
        self.read_until(process, ('[создана %s]' % name).encode())
        # Хостовый терминал закрыт без Ctrl+]: keepalive прекращается.
        process.kill()
        process.wait(timeout=10)
        os.close(terminal)
        self.assertTrue(self.wait_gone(
            lambda: 'hup %s' % name in (self.state / 'client.log').read_text(), 15))
        self.assertEqual(self.sessions(), [name])
        self.assertTrue(self.alive(name))
        # Путь снова свободен: служба не осталась внутри мёртвой сессии.
        # Прежде запросы нового клиента обновляли счётчик keepalive, сессия
        # жила вечно, и повторный вход становился невозможен совсем.
        self.assertTrue(self.wait_gone(
            lambda: self.probe() == {'api': 1, 'direct': 'ready'}, 20))
        # Следующий вход подключается к той же сессии.
        again, terminal = self.open_session(name)
        self.read_until(again, ('[подключена %s]' % name).encode())
        os.write(terminal, b'\x1d')
        self.assertEqual(again.wait(timeout=15), 0)


if __name__ == '__main__':
    unittest.main()
