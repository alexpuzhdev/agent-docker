"""Проверка хостовой части канала сессий: пределы, срок и разбор ответа.

Гость здесь подставной: тест поднимает unix-сокет и отвечает тем, что нужно
проверить. Ни tmux, ни KVM, ни сети не требуется. Отдельно проверяется, что
читатель не касается пользовательской консоли: путь serial.sock в модуле не
упоминается и не открывается.
"""

import ast
import fcntl
import importlib.util
import re
import io
import json
import os
import pty
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import tty
import types
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / 'v2' / 'sessions.py'

GOOD = {'ok': True, 'api': 1, 'sessions': [
    {'name': '20261123T0915-9f3c', 'created': 1764500000,
     'attached': False, 'command': 'claude'},
    {'name': '20261123T0930-1a7b', 'created': 1764500900,
     'attached': True, 'command': 'bash'},
]}


class FakeGuest:
    """Подставной гость: отвечает заранее заданным ответом на один запрос."""

    def __init__(self, directory, reply, delay=0.0, raw=None, chunk=0, pause=0.0):
        self.path = os.path.join(directory, 'control.sock')
        self.reply = reply
        self.delay = delay
        self.raw = raw
        # chunk и pause воспроизводят ответ по частям: именно так складывался
        # срок, когда тайм-аут стоял на каждом чтении отдельно.
        self.chunk = chunk
        self.pause = pause
        self.requests = []
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                connection, _ = self.server.accept()
                with connection:
                    connection.settimeout(5)
                    try:
                        self.requests.append(connection.recv(4096))
                    except OSError:
                        pass
                    if self.delay:
                        time.sleep(self.delay)
                    payload = None
                    if self.raw is not None:
                        payload = self.raw
                    elif self.reply is not None:
                        payload = (json.dumps(self.reply) + '\n').encode()
                    if payload is None:
                        continue
                    if not self.chunk:
                        connection.sendall(payload)
                        continue
                    for start in range(0, len(payload), self.chunk):
                        connection.sendall(payload[start:start + self.chunk])
                        time.sleep(self.pause)
        except OSError:
            pass

    def close(self):
        self.server.close()


class SessionsChannelTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.guest = None

    def tearDown(self):
        if self.guest is not None:
            self.guest.close()

    def read(self, socket_path, operation='sessions', deadline='1', limit='65536'):
        environment = dict(os.environ,
                           AGENT_DOCKER_CONTROL_SOCK=socket_path,
                           AGENT_DOCKER_CONTROL_DEADLINE=deadline,
                           AGENT_DOCKER_CONTROL_LIMIT=limit)
        return subprocess.run([sys.executable, '-I', str(READER), operation],
                              capture_output=True, text=True, timeout=30,
                              env=environment)

    def serve(self, reply, **kwargs):
        self.guest = FakeGuest(self.directory, reply, **kwargs)
        return self.guest

    def test_reads_and_normalizes_list(self):
        guest = self.serve(GOOD)
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload['api'], 1)
        self.assertEqual([entry['name'] for entry in payload['sessions']],
                         ['20261123T0915-9f3c', '20261123T0930-1a7b'])
        self.assertEqual(payload['sessions'][1]['attached'], True)
        self.assertEqual(payload['sessions'][0]['command'], 'claude')
        # В канал уходит только разрешённая операция, произвольных команд нет.
        self.assertEqual(json.loads(guest.requests[0]), {'op': 'sessions'})

    def test_hello_reports_tmux_version(self):
        guest = self.serve({'ok': True, 'api': 1, 'tmux': '3.5a'})
        result = self.read(guest.path, 'hello')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'api': 1, 'tmux': '3.5a'})

    def test_hello_rejects_non_object_json(self):
        guest = self.serve(None, raw=b'null\n')
        result = self.read(guest.path, 'hello')
        self.assertEqual(result.returncode, 5, result.stderr)
        self.assertIn('объектом JSON', result.stderr)

    def test_missing_channel_names_old_image(self):
        result = self.read(os.path.join(self.directory, 'нет-такого.sock'))
        self.assertEqual(result.returncode, 3)
        self.assertIn('не настроен', result.stderr)

    def test_silent_guest_separated_from_missing_channel(self):
        """Канал есть, ответа нет: служба не запущена — это не отсутствие канала."""
        guest = self.serve(None)
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 6)
        self.assertIn('не ответила', result.stderr)

    def test_slow_guest_hits_deadline(self):
        guest = self.serve(GOOD, delay=3)
        started = time.monotonic()
        result = self.read(guest.path, deadline='1')
        self.assertEqual(result.returncode, 6)
        self.assertLess(time.monotonic() - started, 20)

    def test_slow_fragments_do_not_extend_deadline(self):
        """Срок единый: фрагменты по 80 мс не продлевают его складыванием.

        До исправления тайм-аут стоял на каждом чтении, и ответ из фрагментов
        принимался спустя 750 мс при сроке 150 мс.
        """
        guest = self.serve(GOOD, chunk=8, pause=0.08)
        started = time.monotonic()
        result = self.read(guest.path, deadline='0.15')
        spent = time.monotonic() - started
        self.assertEqual(result.returncode, 7, result.stdout)
        self.assertIn('срок', result.stderr)
        # Ответ начался, значит это именно просроченный ответ, а не молчание.
        self.assertNotEqual(result.returncode, 6)
        self.assertLess(spent, 5)

    def test_whole_reply_within_deadline_is_accepted(self):
        """Фрагментами, но в срок — ответ принимается: срок не занижен."""
        guest = self.serve(GOOD, chunk=16, pause=0.01)
        result = self.read(guest.path, deadline='5')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['sessions']), 2)

    def test_missing_permission_is_separated(self):
        """Служба работает, но не видит сервер tmux — отдельный код."""
        guest = self.serve({'ok': False, 'api': 1, 'error': 'no-permission'})
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 8)
        self.assertIn('UID', result.stderr)

    def test_oversized_reply_is_refused(self):
        guest = self.serve(None, raw=b'x' * 20000)
        result = self.read(guest.path, limit='4096')
        self.assertEqual(result.returncode, 4)
        self.assertIn('предел размера', result.stderr)

    def test_broken_json_is_refused(self):
        guest = self.serve(None, raw=b'{not json\n')
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 5)
        self.assertIn('JSON', result.stderr)

    def test_no_tmux_is_reported_separately(self):
        guest = self.serve({'ok': False, 'api': 1, 'error': 'no-tmux'})
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 9)
        self.assertIn('tmux', result.stderr)

    def test_wrong_api_is_refused(self):
        guest = self.serve({'ok': True, 'api': 99, 'sessions': []})
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 5)
        self.assertIn('версии контракта', result.stderr)

    def test_structure_is_validated(self):
        broken = [
            {'ok': True, 'api': 1, 'sessions': 'список'},
            {'ok': True, 'api': 1, 'sessions': ['строка']},
            {'ok': True, 'api': 1, 'sessions': [{'created': 1, 'attached': False, 'command': 'c'}]},
            {'ok': True, 'api': 1, 'sessions': [{'name': 'a', 'created': 'вчера',
                                                 'attached': False, 'command': 'c'}]},
            {'ok': True, 'api': 1, 'sessions': [{'name': 'a', 'created': 1,
                                                 'attached': 'да', 'command': 'c'}]},
            {'ok': True, 'api': 1, 'sessions': [{'name': 'a' * 300, 'created': 1,
                                                 'attached': False, 'command': 'c'}]},
        ]
        for reply in broken:
            with self.subTest(reply=reply):
                if self.guest is not None:
                    self.guest.close()
                self.directory = tempfile.mkdtemp()
                guest = self.serve(reply)
                result = self.read(guest.path)
                self.assertEqual(result.returncode, 5, result.stdout)

    def test_control_characters_are_stripped(self):
        guest = self.serve({'ok': True, 'api': 1, 'sessions': [
            {'name': 'и\x1b[2Jмя', 'created': 1, 'attached': False, 'command': 'claude'}]})
        result = self.read(guest.path)
        self.assertEqual(result.returncode, 0, result.stderr)
        name = json.loads(result.stdout)['sessions'][0]['name']
        self.assertNotIn('\x1b', name)

    def test_reader_never_touches_user_console(self):
        """Ни один строковый литерал кода не ведёт к пользовательской консоли.

        Проверяются именно литералы, а не весь текст: в пояснениях модуля
        serial-консоль упоминается нарочно, а в коде её быть не должно.
        """
        tree = ast.parse(READER.read_text(encoding='utf-8'))
        documentation = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                        and isinstance(first.value.value, str):
                    documentation.add(id(first.value))
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in documentation]
        self.assertTrue(literals)
        for literal in literals:
            self.assertNotIn('serial.sock', literal)
            self.assertNotIn('console.log', literal)
            self.assertNotIn('console.pending', literal)


class DiagnoseTest(unittest.TestCase):
    """Диагностика подключения: каждое состояние показывается отдельно.

    Проверяется и то, что диагностика не падает: показать старый образ,
    молчащую службу или отсутствие tmux должен список на хосте, а не отказ
    команды. Обычная serial-консоль ни от одного из состояний не зависит, и
    диагностика её не касается — в tty не уходит ни байта.
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.guest = None
        self.lock = os.path.join(self.directory, 'console.lock')

    def tearDown(self):
        if self.guest is not None:
            self.guest.close()

    def environment(self, socket_path):
        return dict(os.environ,
                    AGENT_DOCKER_CONTROL_SOCK=socket_path,
                    AGENT_DOCKER_CONTROL_DEADLINE='1',
                    AGENT_DOCKER_CONSOLE_LOCK=self.lock)

    def diagnose(self, socket_path, stdin=None):
        result = subprocess.run([sys.executable, '-I', str(READER), 'diagnose'],
                                capture_output=True, text=True, timeout=30,
                                stdin=stdin, env=self.environment(socket_path))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def answering(self, hello, listing=None):
        """Подставной гость, отвечающий на hello и на sessions по-разному."""
        self.guest = TwoAnswerGuest(self.directory, hello, listing)
        return self.guest.path

    def test_old_image_without_channel(self):
        """Канала нет совсем: это состояние, а не отказ команды."""
        report = self.diagnose(os.path.join(self.directory, 'нет.sock'))
        self.assertEqual(report['state'], 'old-image')
        self.assertEqual(report['sessions'], [])
        self.assertIn('консоль', report['hint'])

    def test_service_absent_is_not_an_old_image(self):
        """Канал есть, служба молчит: причина другая и подсказка другая."""
        report = self.diagnose(self.answering(None))
        self.assertEqual(report['state'], 'no-service')
        self.assertIn('journalctl', report['hint'])

    def test_no_tmux_in_image(self):
        report = self.diagnose(self.answering({'ok': False, 'api': 1, 'error': 'no-tmux'}))
        self.assertEqual(report['state'], 'no-tmux')
        self.assertIsNone(report['tmux'])

    def test_empty_list_is_its_own_state(self):
        report = self.diagnose(self.answering(
            {'ok': True, 'api': 1, 'tmux': '3.5a', 'socket': 'absent'},
            {'ok': True, 'api': 1, 'sessions': [], 'skipped': 0}))
        self.assertEqual(report['state'], 'empty')
        self.assertEqual(report['tmux'], '3.5a')
        self.assertEqual(report['socket'], 'absent')
        self.assertEqual(report['sessions'], [])

    def test_live_list_is_returned(self):
        report = self.diagnose(self.answering(
            {'ok': True, 'api': 1, 'tmux': '3.5a', 'socket': 'present'}, GOOD))
        self.assertEqual(report['state'], 'live')
        self.assertEqual([entry['name'] for entry in report['sessions']],
                         ['20261123T0915-9f3c', '20261123T0930-1a7b'])

    def test_denied_socket_is_not_an_empty_list(self):
        report = self.diagnose(self.answering(
            {'ok': True, 'api': 1, 'tmux': '3.5a', 'socket': 'denied'},
            {'ok': False, 'api': 1, 'error': 'no-permission'}))
        self.assertEqual(report['state'], 'no-permission')
        self.assertIn('могут быть', report['hint'])

    def test_states_are_all_different(self):
        """Шесть состояний — шесть разных значений state."""
        cases = {
            'нет канала': (os.path.join(self.directory, 'нет.sock'), None, None),
            'нет службы': (None, None, None),
            'нет tmux': (None, {'ok': False, 'api': 1, 'error': 'no-tmux'}, None),
            'пусто': (None, {'ok': True, 'api': 1, 'tmux': '3.5a'},
                      {'ok': True, 'api': 1, 'sessions': []}),
            'живой список': (None, {'ok': True, 'api': 1, 'tmux': '3.5a'}, GOOD),
            'нет доступа': (None, {'ok': True, 'api': 1, 'tmux': '3.5a'},
                            {'ok': False, 'api': 1, 'error': 'no-permission'}),
        }
        states = {}
        for label, (path, hello, listing) in cases.items():
            if path is None:
                path = self.answering(hello, listing)
            states[label] = self.diagnose(path)['state']
            if self.guest is not None:
                self.guest.close()
                self.guest = None
                os.unlink(path)
        self.assertEqual(len(set(states.values())), len(cases), states)

    def test_busy_console_is_shown_separately(self):
        """Занятая консоль — отдельное поле, а не отказ диагностики."""
        path = self.answering({'ok': True, 'api': 1, 'tmux': '3.5a'},
                              {'ok': True, 'api': 1, 'sessions': []})
        self.assertEqual(self.diagnose(path)['console'], 'free')
        # Файл блокировки не создаётся проверкой: его отсутствие и означает,
        # что консоль не открывали ни разу.
        self.assertFalse(os.path.exists(self.lock))
        with open(self.lock, 'a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            report = self.diagnose(path)
            self.assertEqual(report['console'], 'busy')
            # Состояние канала при занятой консоли прежнее: это разные вещи.
            self.assertEqual(report['state'], 'empty')
        self.assertEqual(self.diagnose(path)['console'], 'free')

    def test_opening_the_list_writes_nothing_to_the_tty(self):
        """Ни байта в tty и ни одного подключения к консоли гостя.

        Консоль гостя здесь настоящая по форме: рядом с каналом лежит сокет
        serial.sock, а stdin команды — pty. После диагностики и после запроса
        списка в pty не должно появиться ничего, а serial.sock не должен
        увидеть ни одного подключения.
        """
        path = self.answering({'ok': True, 'api': 1, 'tmux': '3.5a'}, GOOD)
        console = socket.socket(socket.AF_UNIX)
        self.addCleanup(console.close)
        console.bind(os.path.join(self.directory, 'serial.sock'))
        console.listen(1)
        console.setblocking(False)
        log = Path(self.directory) / 'console.log'
        log.write_text('прежний журнал\n', encoding='utf-8')
        primary, secondary = pty.openpty()
        self.addCleanup(os.close, primary)
        # Подчинённый конец остаётся открытым до конца проверки: без него
        # чтение из pty даёт EIO и сказать, пусто ли в нём, уже нельзя.
        self.addCleanup(os.close, secondary)
        for operation in ('diagnose', 'sessions'):
            result = subprocess.run(
                [sys.executable, '-I', str(READER), operation],
                capture_output=True, text=True, timeout=30, stdin=secondary,
                env=self.environment(path))
            self.assertEqual(result.returncode, 0, result.stderr)
        os.set_blocking(primary, False)
        with self.assertRaises(BlockingIOError):
            os.read(primary, 4096)
        with self.assertRaises(BlockingIOError):
            console.accept()
        self.assertEqual(log.read_text(encoding='utf-8'), 'прежний журнал\n')


class TwoAnswerGuest:
    """Подставной гость с разными ответами на hello и sessions.

    Диагностика спрашивает дважды: сначала версию tmux и состояние сокета,
    затем список. Ответ None означает молчание — именно так выглядит
    незапущенная служба.
    """

    def __init__(self, directory, hello, listing):
        self.path = os.path.join(directory, 'control.sock')
        self.hello = hello
        self.listing = listing
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(4)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                connection, _ = self.server.accept()
                with connection:
                    connection.settimeout(5)
                    try:
                        request = connection.recv(4096)
                    except OSError:
                        continue
                    reply = self.hello if b'hello' in request else self.listing
                    if reply is None:
                        continue
                    connection.sendall((json.dumps(reply) + '\n').encode())
        except OSError:
            pass

    def close(self):
        self.server.close()


class ResponderStreamTest(unittest.TestCase):
    """Модель двунаправленного непозиционируемого потока гостевой службы.

    Порт virtio не позиционируется, и буферизованный текстовый режим r+ для
    него не годится. Проверяется и само ограничение, и то, что служба читает
    и пишет через дескриптор, которому seek не нужен.
    """

    def setUp(self):
        specification = importlib.util.spec_from_file_location(
            'responder', str(ROOT / 'v2' / 'sessions-responder.py'))
        self.responder = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(self.responder)

    def terminal(self):
        """Непозиционируемый двунаправленный поток: пара PTY без эха."""
        primary, secondary = pty.openpty()
        self.addCleanup(os.close, primary)
        self.addCleanup(os.close, secondary)
        # Без raw слуга отражал бы запрос в ответ и разбор сломался бы.
        tty.setraw(secondary)
        tty.setraw(primary)
        return primary, secondary

    def test_buffered_text_mode_fails_on_non_seekable(self):
        _, secondary = self.terminal()
        with self.assertRaises(io.UnsupportedOperation):
            open(os.ttyname(secondary), 'r+', buffering=1)

    def test_channel_serves_request_over_non_seekable_stream(self):
        primary, secondary = self.terminal()
        self.responder.version = lambda: '3.5a'
        self.responder.sessions = lambda: (
            [{'name': 'стенд', 'created': 1, 'attached': False, 'command': 'claude'}], 0)
        os.write(primary, b'{"op": "sessions"}\n')
        self.assertTrue(self.responder.serve(self.responder.Channel(secondary)))
        collected = bytearray()
        while b'\n' not in collected:
            collected += os.read(primary, 4096)
        reply = json.loads(bytes(collected).partition(b'\n')[0])
        self.assertEqual(reply['api'], 1)
        self.assertEqual(reply['sessions'][0]['name'], 'стенд')

    def test_request_limit_is_respected(self):
        primary, secondary = self.terminal()
        channel = self.responder.Channel(secondary)
        os.write(primary, b'x' * 64 + b'\n')
        self.assertEqual(len(channel.read_line(limit=16)), 16)

    def test_write_line_adds_terminator_once(self):
        primary, secondary = self.terminal()
        channel = self.responder.Channel(secondary)
        channel.write_line(b'{"ok": true}')
        channel.write_line(b'{"ok": false}\n')
        collected = bytearray()
        while collected.count(b'\n') < 2:
            collected += os.read(primary, 4096)
        self.assertEqual(bytes(collected).count(b'\n'), 2)

    def test_responder_uses_no_shell_and_no_network(self):
        """Ни оболочки, ни сети, ни управляющих команд tmux — по коду, не по тексту.

        Проверяются импорты, ключевые слова вызовов и строковые литералы кода:
        в пояснениях модуля управляющие команды названы нарочно, чтобы
        объяснить, чего в нём нет.
        """
        tree = ast.parse((ROOT / 'v2' / 'sessions-responder.py').read_text(encoding='utf-8'))
        imported = set()
        documentation = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or '')
            elif isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                        and isinstance(first.value.value, str):
                    documentation.add(id(first.value))
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    self.assertNotEqual(keyword.arg, 'shell')
        for module in ('socket', 'http.client', 'urllib.request', 'pty', 'os.system'):
            self.assertNotIn(module, imported)
        literals = [node.value for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in documentation]
        self.assertTrue(literals)
        # Сравнение точное: элементы argv — это целые слова команд, а строка
        # формата законно содержит поле #{session_attached}.
        forbidden = {'send-keys', 'kill-session', 'kill-server', 'kill-pane', 'attach',
                     'attach-session', 'new-session', 'respawn-pane', 'run-shell',
                     'source-file', 'set-option'}
        for literal in literals:
            self.assertNotIn(literal, forbidden)
        # Команды, которые служба действительно передаёт tmux, перечислены явно.
        allowed = {'list-sessions', 'list-panes', 'show-options', '-V', '-S', '-F'}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, 'id', None)
            if name != 'tmux':
                continue
            words = [argument.value for argument in node.args
                     if isinstance(argument, ast.Constant) and isinstance(argument.value, str)]
            self.assertTrue(words)
            self.assertIn(words[0], allowed)


def load_responder():
    """Загружает гостевую службу как модуль: tmux и KVM для этого не нужны."""
    specification = importlib.util.spec_from_file_location(
        'responder', str(ROOT / 'v2' / 'sessions-responder.py'))
    responder = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(responder)
    return responder


class TmuxFormatTest(unittest.TestCase):
    """Разбор вывода tmux list-sessions на реальных и неудобных данных."""

    def setUp(self):
        self.responder = load_responder()
        # Сокет настоящий: служба сначала проверяет его stat по точному пути,
        # и без сокета разбор вывода не начался бы вовсе.
        directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, directory, True)
        self.socket_path = os.path.join(directory, 'atrice')
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(self.socket_path)
        self.addCleanup(listener.close)
        self.responder.SOCKET_PATH = self.socket_path
        self.responder.LEGACY_SOCKET_PATH = ''

    def tmux_output(self, text, code=0, stderr=''):
        """Подменяет вызов tmux заранее снятым выводом.

        Версия тоже подменяется: answer сначала спрашивает её, и без этого
        проверка разбора упиралась бы в отсутствие tmux на машине сборки.
        """
        result = subprocess.CompletedProcess(['tmux'], code, text, stderr)
        self.calls = []

        def fake_tmux(socket_path, *arguments):
            self.calls.append((socket_path, arguments))
            return result

        self.responder.tmux = fake_tmux
        self.responder.version = lambda: '3.5a'


    def test_format_uses_no_control_bytes(self):
        """Причина бага: управляющий байт в -F tmux экранирует в выводе."""
        for character in self.responder.LIST_FORMAT:
            self.assertGreaterEqual(ord(character), 0x20)
        self.assertNotIn('\x1f', self.responder.LIST_FORMAT)

    def test_escaped_us_output_is_not_mistaken_for_empty(self):
        """Пойманный на Debian KVM вывод: US превращён в буквальные \\037.

        tmux 3.5a печатает ASCII US четырьмя байтами 5c 30 33 37. Раньше
        разбор молча отбрасывал такую строку и отдавал [], и живая сессия
        выглядела как отсутствие сессий. Теперь это отдельная неисправность.
        """
        caught = 'claude-1234567890abcdef\\0371764500000\\0370\\037claude\n'
        self.assertIn(b'\x5c\x30\x33\x37', caught.encode())
        self.tmux_output(caught)
        self.assertEqual(self.responder.sessions(), 'unparsed')
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': False, 'api': 1, 'error': 'format-unparsed'})

    def test_live_session_is_listed_with_new_separator(self):
        separator = self.responder.SEPARATOR
        self.tmux_output(separator.join(
            ['claude-1234567890abcdef', '1764500000', '1', 'claude']) + '\n')
        listed, skipped = self.responder.sessions()
        self.assertEqual(skipped, 0)
        self.assertEqual(listed, [{'name': 'claude-1234567890abcdef',
                                   'created': 1764500000, 'attached': True,
                                   'command': 'claude'}])

    def test_fields_with_spaces_and_odd_characters(self):
        separator = self.responder.SEPARATOR
        lines = [
            separator.join(['сессия с пробелами', '1', '0', 'bash -l']),
            # Свободное поле последнее, поэтому разделитель внутри него
            # остаётся частью команды и не сдвигает разбор.
            separator.join(['20261123T0915-9f3c', '2', '1',
                            'python3 -c a' + separator + 'b']),
            separator.join(['имя:с:двоеточиями', '3', '0', 'tmux']),
            separator.join(['таб\tи\x7fуправляющие', '4', '1', 'claude']),
        ]
        self.tmux_output('\n'.join(lines) + '\n')
        listed, skipped = self.responder.sessions()
        self.assertEqual(skipped, 0)
        self.assertEqual([entry['name'] for entry in listed],
                         ['сессия с пробелами', '20261123T0915-9f3c',
                          'имя:с:двоеточиями', 'таб\tи\x7fуправляющие'])
        self.assertEqual(listed[1]['command'], 'python3 -c a' + separator + 'b')

    def test_partially_broken_output_keeps_good_lines(self):
        separator = self.responder.SEPARATOR
        self.tmux_output(separator.join(['живая', '7', '0', 'claude']) + '\n'
                         + 'мусор без разделителя\n')
        listed, skipped = self.responder.sessions()
        self.assertEqual([entry['name'] for entry in listed], ['живая'])
        self.assertEqual(skipped, 1)
        answer = self.responder.answer({'op': 'sessions'})
        self.assertTrue(answer['ok'])
        self.assertEqual(answer['skipped'], 1)

    def test_empty_list_stays_empty_list(self):
        self.tmux_output('')
        self.assertEqual(self.responder.sessions(), ([], 0))
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': True, 'api': 1, 'sessions': [], 'skipped': 0})

    def test_no_server_running_is_not_an_error(self):
        self.tmux_output('', code=1, stderr='no server running on /tmp/tmux-1000/atrice')
        self.assertEqual(self.responder.sessions(), ([], 0))
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': True, 'api': 1, 'sessions': [], 'skipped': 0})

    def test_absent_server_before_first_start_is_empty_list(self):
        """Снятая на стенде строка: сокета ещё нет до первого запуска сервера.

        tmux 3.5a на восстановленной Debian KVM печатает именно это, и канал
        отвечал отказом вместо пустого списка. Проверяется и answer: на этой
        ветке sessions раньше возвращал голый список, и распаковка пары в
        answer падала, то есть служба умирала на ровном месте.
        """
        caught = 'error connecting to /tmp/tmux-1000/atrice (No such file or directory)'
        self.tmux_output('', code=1, stderr=caught)
        self.assertEqual(self.responder.classify_failure(caught), 'absent')
        self.assertEqual(self.responder.sessions(), ([], 0))
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': True, 'api': 1, 'sessions': [], 'skipped': 0})

    def test_denied_never_becomes_empty_list(self):
        """Отказ доступа проверяется раньше отсутствия сервера."""
        for stderr in ('error connecting to /tmp/tmux-1000/atrice (Permission denied)',
                       'Permission denied',
                       'error connecting to /tmp/tmux-1000/atrice: Permission denied'):
            with self.subTest(stderr=stderr):
                self.assertEqual(self.responder.classify_failure(stderr), 'denied')
                self.tmux_output('', code=1, stderr=stderr)
                self.assertEqual(self.responder.sessions(), 'denied')
                self.assertEqual(self.responder.answer({'op': 'sessions'}),
                                 {'ok': False, 'api': 1, 'error': 'no-permission'})

    def test_other_failures_stay_failures(self):
        """Прочие отказы не превращаются ни в пустой список, ни в no-permission."""
        for stderr in ('error connecting to /tmp/tmux-1000/atrice (Connection refused)',
                       'lost server',
                       'unknown command: list-sessions',
                       ''):
            with self.subTest(stderr=stderr):
                self.assertEqual(self.responder.classify_failure(stderr), 'unknown')
                self.tmux_output('', code=1, stderr=stderr)
                self.assertIsNone(self.responder.sessions())
                self.assertEqual(self.responder.answer({'op': 'sessions'}),
                                 {'ok': False, 'api': 1, 'error': 'tmux-failed'})

    def test_failures_are_four_distinct_outcomes(self):
        """Отсутствие tmux, отсутствие сервера, отказ доступа и прочее — разное."""
        self.tmux_output('', code=1, stderr='no server running')
        self.responder.version = lambda: None
        self.assertEqual(self.responder.answer({'op': 'sessions'})['error'], 'no-tmux')
        outcomes = {}
        for stderr in ('error connecting to /x (No such file or directory)',
                       'error connecting to /x (Permission denied)',
                       'lost server'):
            self.tmux_output('', code=1, stderr=stderr)
            answer = self.responder.answer({'op': 'sessions'})
            outcomes[stderr] = answer.get('error', 'ok')
        self.assertEqual(sorted(outcomes.values()),
                         ['no-permission', 'ok', 'tmux-failed'])

    def test_tmux_runs_in_c_locale(self):
        """Локаль зафиксирована: строку ошибки даёт strerror и она переводится."""
        captured = {}

        def fake_run(argv, **keywords):
            captured['argv'] = argv
            captured['env'] = keywords.get('env')
            return subprocess.CompletedProcess(argv, 0, '', '')

        original = self.responder.subprocess
        self.responder.subprocess = types.SimpleNamespace(
            run=fake_run, SubprocessError=subprocess.SubprocessError)
        self.addCleanup(setattr, self.responder, 'subprocess', original)
        self.responder.tmux(self.socket_path, 'list-sessions')
        self.assertEqual(captured['argv'][:3], ['tmux', '-S', self.socket_path])
        self.assertEqual(captured['env']['LC_ALL'], 'C')
        self.assertEqual(captured['env']['LANG'], 'C')
        self.responder.version()
        self.assertEqual(captured['argv'], ['tmux', '-V'])
        self.assertEqual(captured['env']['LC_ALL'], 'C')

    def test_our_identifier_format_is_separator_safe(self):
        """Наши идентификаторы: только буквы, цифры и дефис.

        Такое имя допустимо для tmux (в нём нет точки и двоеточия), не
        содержит разделителя и не требует экранирования в выводе -F.
        """
        pattern = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,63}$')
        for identifier in ('20261123T0915-9f3c', 'claude-1234567890abcdef'):
            self.assertRegex(identifier, pattern)
            self.assertNotIn(self.responder.SEPARATOR, identifier)
            self.assertTrue(all(ord(character) >= 0x20 for character in identifier))
        for bad in ('имя с пробелом', 'с.точкой', 'с:двоеточием',
                    'с' + self.responder.SEPARATOR + 'разделителем'):
            self.assertNotRegex(bad, pattern)


class TmuxSocketTest(unittest.TestCase):
    """Поиск сокета tmux без запуска tmux.

    Пойманная на чистой Debian KVM неисправность: первый запрос до любого
    запуска tmux отвечал tmux-failed, а после обычного вызова tmux в консоли
    тот же запрос отвечал пустым списком. Причина — метка -L заставляет tmux
    создать каталог сокета, а у службы ProtectSystem=strict: вся иерархия
    только для чтения, mkdir отвечает EROFS. Теперь каталог создаётся заранее,
    а служба проверяет точный путь сокета через stat и tmux для этого не
    вызывает вовсе.
    """

    def setUp(self):
        self.responder = load_responder()
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.socket_path = os.path.join(self.directory, 'atrice')
        self.responder.SOCKET_PATH = self.socket_path
        self.responder.LEGACY_SOCKET_PATH = ''
        self.responder.version = lambda: '3.5a'
        # Любой вызов tmux на этом стенде — неисправность: там, где служба
        # должна молчать, она обязана отвечать, не запуская ничего.
        self.calls = []

        def refuse_tmux(socket_path, *arguments):
            self.calls.append((socket_path, arguments))
            raise AssertionError('tmux вызван там, где он не нужен')

        self.responder.tmux = refuse_tmux

    def listen(self, path=None):
        """Создаёт настоящий сокет: именно его служба и должна найти."""
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(path or self.socket_path)
        self.addCleanup(listener.close)
        return listener

    def answer_list(self, lines):
        """Подменяет вывод tmux, запоминая путь сокета в вызове."""
        result = subprocess.CompletedProcess(['tmux'], 0, lines, '')

        def fake_tmux(socket_path, *arguments):
            self.calls.append((socket_path, arguments))
            return result

        self.responder.tmux = fake_tmux

    def test_clean_start_answers_empty_list_without_running_tmux(self):
        """Чистый старт: каталог сокета есть, сокета нет, tmux не запускался."""
        self.assertEqual(self.responder.socket_state(), ('absent', None))
        self.assertEqual(self.responder.sessions(), ([], 0))
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': True, 'api': 1, 'sessions': [], 'skipped': 0})
        self.assertEqual(self.calls, [])

    def test_missing_socket_directory_is_empty_list_too(self):
        """Каталога сокета ещё нет: ответ тот же, и ошибки доступа нет."""
        self.responder.SOCKET_PATH = os.path.join(self.directory, 'нет', 'atrice')
        self.assertEqual(self.responder.socket_state(), ('absent', None))
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': True, 'api': 1, 'sessions': [], 'skipped': 0})
        self.assertEqual(self.calls, [])

    def test_live_socket_is_queried_by_exact_path(self):
        """Сокет на месте: tmux вызывается с -S и ровно этим путём."""
        self.listen()
        self.assertEqual(self.responder.socket_state(),
                         ('present', self.socket_path))
        self.answer_list(self.responder.SEPARATOR.join(
            ['20261123T0915-9f3c', '1764500000', '1', 'claude']) + '\n')
        answer = self.responder.answer({'op': 'sessions'})
        self.assertTrue(answer['ok'])
        self.assertEqual([entry['name'] for entry in answer['sessions']],
                         ['20261123T0915-9f3c'])
        self.assertEqual(self.calls[0][0], self.socket_path)
        self.assertIn('list-sessions', self.calls[0][1])

    def test_legacy_socket_is_found_after_the_dedicated_one(self):
        """Сессия, запущенная прежним способом в /tmp, не теряется."""
        legacy = os.path.join(self.directory, 'tmux-1000-atrice')
        self.responder.LEGACY_SOCKET_PATH = legacy
        self.listen(legacy)
        self.assertEqual(self.responder.socket_state(), ('present', legacy))
        # Выделенный сокет главнее: появившись, он и будет опрошен.
        self.listen()
        self.assertEqual(self.responder.socket_state(),
                         ('present', self.socket_path))

    @unittest.skipIf(os.geteuid() == 0, 'root обходит права доступа')
    def test_denied_socket_directory_is_not_an_empty_list(self):
        """Отказ доступа к каталогу сокета остаётся отказом.

        Это главное требование: пустой список означает «сессий нет», и
        подменять им отказ нельзя — живые сессии в этот момент могут быть.
        """
        closed = os.path.join(self.directory, 'закрытый')
        os.mkdir(closed, 0o700)
        self.addCleanup(os.chmod, closed, 0o700)
        self.responder.SOCKET_PATH = os.path.join(closed, 'atrice')
        self.listen(self.responder.SOCKET_PATH)
        os.chmod(closed, 0o000)
        state, path = self.responder.socket_state()
        self.assertEqual(state, 'denied')
        self.assertEqual(path, self.responder.SOCKET_PATH)
        self.assertEqual(self.responder.sessions(), 'denied')
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': False, 'api': 1, 'error': 'no-permission'})
        self.assertEqual(self.calls, [])

    @unittest.skipIf(os.geteuid() == 0, 'root обходит права доступа')
    def test_denied_wins_over_absent_legacy_path(self):
        """Закрытый путь не маскируется отсутствием второго."""
        closed = os.path.join(self.directory, 'закрытый2')
        os.mkdir(closed, 0o700)
        self.addCleanup(os.chmod, closed, 0o700)
        self.responder.SOCKET_PATH = os.path.join(closed, 'atrice')
        self.listen(self.responder.SOCKET_PATH)
        os.chmod(closed, 0o000)
        self.responder.LEGACY_SOCKET_PATH = os.path.join(self.directory, 'нет')
        self.assertEqual(self.responder.socket_state()[0], 'denied')

    def test_file_in_place_of_socket_is_a_failure(self):
        """По пути сокета лежит обычный файл: это не «сессий нет»."""
        Path(self.socket_path).write_text('не сокет')
        self.assertEqual(self.responder.socket_state(),
                         ('unknown', self.socket_path))
        self.assertIsNone(self.responder.sessions())
        self.assertEqual(self.responder.answer({'op': 'sessions'}),
                         {'ok': False, 'api': 1, 'error': 'tmux-failed'})
        self.assertEqual(self.calls, [])

    def test_repeated_requests_are_idempotent(self):
        """Идемпотентность: опрос ничего не меняет и отвечает одинаково."""
        before = sorted(os.listdir(self.directory))
        empty = [self.responder.answer({'op': 'sessions'}) for _ in range(3)]
        self.assertEqual(empty[0], empty[1])
        self.assertEqual(empty[1], empty[2])
        self.assertEqual(sorted(os.listdir(self.directory)), before)
        self.listen()
        self.answer_list(self.responder.SEPARATOR.join(
            ['живая', '1764500000', '0', 'claude']) + '\n')
        live = [self.responder.answer({'op': 'sessions'}) for _ in range(3)]
        self.assertEqual(live[0], live[1])
        self.assertEqual(live[1], live[2])
        self.assertEqual(len(live[0]['sessions']), 1)
        # Опрос не создаёт ни каталогов, ни файлов рядом с сокетом.
        self.assertEqual(sorted(os.listdir(self.directory)), ['atrice'])

    def test_hello_reports_socket_state(self):
        """hello показывает состояние сокета до запроса списка."""
        self.assertEqual(self.responder.answer({'op': 'hello'}),
                         {'ok': True, 'api': 1, 'tmux': '3.5a', 'socket': 'absent'})
        self.listen()
        self.assertEqual(self.responder.answer({'op': 'hello'})['socket'], 'present')

    def test_responder_creates_nothing(self):
        """В коде службы нет ни создания каталогов, ни bind, ни chmod."""
        source = (ROOT / 'v2' / 'sessions-responder.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        forbidden = {'mkdir', 'makedirs', 'bind', 'chmod', 'chown', 'rename',
                     'remove', 'unlink', 'rmdir'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, 'attr', None) or getattr(node.func, 'id', None)
                self.assertNotIn(name, forbidden)
        self.assertNotIn("'-L'", source)

    def test_socket_directory_is_prepared_in_advance(self):
        """Каталог сокета задан файлом tmpfiles и закрыт для посторонних."""
        configuration = (ROOT / 'v2' / 'atrice-sessions.tmpfiles.conf').read_text(
            encoding='utf-8')
        responder_source = (ROOT / 'v2' / 'sessions-responder.py').read_text(
            encoding='utf-8')
        profile = (ROOT / 'v2' / 'atrice-tmux-profile.sh').read_text(encoding='utf-8')
        path = re.search(r'^ATRICE_TMUX_SOCKET=(\S+)$', profile,
                         re.MULTILINE).group(1)
        # Один и тот же путь по обе стороны: служба смотрит ровно его, агент
        # запускает tmux ровно на нём.
        self.assertIn("'%s'" % path, responder_source)
        directory = os.path.dirname(path)
        rows = [line.split() for line in configuration.splitlines()
                if line.strip() and not line.startswith('#')]
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row[0], 'd')
        owned = [row for row in rows if row[1] == directory]
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0][2:5], ['0770', 'agent', 'agent'])
        # Корневой каталог тоже создаётся заранее, иначе tmpfiles не создаст
        # вложенный.
        self.assertIn(os.path.dirname(directory), [row[1] for row in rows])


class SeedInstallTest(unittest.TestCase):
    """Доставка службы: содержимое файлов, права и отсутствие репровижининга."""

    @classmethod
    def setUpClass(cls):
        cls.seed = (ROOT / 'v2' / 'seed.py').read_text(encoding='utf-8')
        cls.rule = (ROOT / 'v2' / 'atrice-control.rules').read_text(encoding='utf-8')
        cls.unit = (ROOT / 'v2' / 'atrice-sessions.service').read_text(encoding='utf-8')

    def test_udev_rule_matches_only_this_port(self):
        self.assertIn('SUBSYSTEM=="virtio-ports"', self.rule)
        self.assertIn('ATTR{name}=="atrice.control"', self.rule)
        self.assertIn('GROUP="agent"', self.rule)
        self.assertIn('MODE="0660"', self.rule)
        # Широкого доступа ко всем портам быть не должно.
        self.assertNotIn('ATTR{name}=="*"', self.rule)
        self.assertNotIn('MODE="0666"', self.rule)
        self.assertNotIn('OWNER="root"', self.rule)
        for line in self.rule.splitlines():
            if line.startswith('#') or not line.strip():
                continue
            self.assertIn('atrice.control', line)

    def test_service_runs_as_agent_without_root(self):
        self.assertIn('User=agent', self.unit)
        self.assertIn('Group=agent', self.unit)
        self.assertNotIn('User=root', self.unit)
        self.assertIn('WantedBy=multi-user.target', self.unit)
        self.assertIn('sessions-responder.py', self.unit)
        # Сети у службы нет структурно, а не по договорённости.
        self.assertIn('PrivateNetwork=yes', self.unit)
        self.assertIn('NoNewPrivileges=yes', self.unit)
        # PrivateTmp отрезал бы сокет tmux агентов: директивы быть не должно.
        self.assertNotIn('PrivateTmp=', self.unit)

    def test_service_keeps_its_restrictions(self):
        """Ограничения службы не ослаблены ради каталога сокета.

        Соблазн был: при ProtectSystem=strict mkdir каталога сокета отвечает
        EROFS, и проще всего было бы выдать службе право записи. Вместо этого
        каталог создаётся заранее, а строгие директивы остаются на месте.
        """
        self.assertIn('ProtectSystem=strict', self.unit)
        self.assertIn('ProtectHome=read-only', self.unit)
        self.assertIn('RestrictAddressFamilies=AF_UNIX', self.unit)
        self.assertIn('RestrictSUIDSGID=yes', self.unit)
        self.assertIn('ProtectKernelTunables=yes', self.unit)
        self.assertIn('ProtectControlGroups=yes', self.unit)
        for weakening in ('ReadWritePaths=', 'ProtectSystem=full', 'ProtectSystem=yes',
                          'ProtectSystem=no', 'ProtectHome=no', 'StateDirectory=',
                          'RuntimeDirectory=', 'PrivateNetwork=no',
                          'NoNewPrivileges=no', 'User=root', 'AmbientCapabilities=',
                          'CapabilityBoundingSet=CAP'):
            self.assertNotIn(weakening, self.unit)

    def test_seed_delivers_all_five_files(self):
        for path, origin in (
                ('/etc/udev/rules.d/60-atrice-control.rules', 'atrice-control.rules'),
                ('/etc/systemd/system/atrice-sessions.service', 'atrice-sessions.service'),
                ('/usr/local/lib/atrice/sessions-responder.py', 'sessions-responder.py'),
                ('/usr/lib/tmpfiles.d/atrice-sessions.conf',
                 'atrice-sessions.tmpfiles.conf'),
                ('/etc/profile.d/atrice-tmux.sh', 'atrice-tmux-profile.sh')):
            self.assertIn(path, self.seed)
            self.assertIn("'/opt/agent-docker/%s'" % origin, self.seed)
        self.assertIn("'0755', Path('/opt/agent-docker/sessions-responder.py')", self.seed)

    def test_runcmd_prepares_socket_directory_before_service(self):
        """Каталог сокета создаётся до запуска службы, а не ею."""
        prepare = self.seed.index("'systemd-tmpfiles', '--create'")
        start = self.seed.index("'systemctl', 'enable', '--now', 'atrice-sessions.service'")
        self.assertLess(prepare, start)
        self.assertIn('/usr/lib/tmpfiles.d/atrice-sessions.conf', self.seed)

    def test_instance_id_has_no_revision(self):
        """Смена instance-id перепровижинирует гостя и меняет ключ хоста.

        На копии старого диска это сгенерировало новый ключ ED25519, поэтому
        идентификатор обязан остаться постоянным, а существующие гости
        получают канал отдельным узким шагом.
        """
        self.assertIn(r"instance-id: agent-docker-{name}\nlocal-hostname", self.seed)
        self.assertNotIn('SEED_REVISION', self.seed)
        self.assertNotIn('-r{', self.seed)

    def test_runcmd_enables_service_and_reloads_udev(self):
        for step in ("'udevadm', 'control', '--reload'",
                     "'udevadm', 'trigger', '--subsystem-match=virtio-ports'",
                     "'systemctl', 'enable', '--now', 'atrice-sessions.service'"):
            self.assertIn(step, self.seed)

    def test_container_image_carries_unit_and_rule(self):
        """Без расширения COPY юнит и правило не попадут в контейнер."""
        dockerfile = (ROOT / 'v2' / 'Dockerfile').read_text(encoding='utf-8')
        copy = [line for line in dockerfile.splitlines() if line.startswith('COPY')]
        self.assertTrue(copy)
        self.assertIn('*.service', copy[0])
        self.assertIn('*.rules', copy[0])
        self.assertIn('*.conf', copy[0])

    def test_installer_touches_only_its_five_paths(self):
        """Установщик для существующих гостей не знает чужих путей."""
        source = (ROOT / 'v2' / 'install-sessions-guest.sh').read_text(encoding='utf-8')
        # Пояснения скрипта перечисляют чужие пути нарочно — чтобы сказать,
        # чего он не трогает. Проверяется код без строк-комментариев.
        code = '\n'.join(line for line in source.splitlines()
                         if not line.lstrip().startswith('#'))
        for path in ('/usr/local/lib/atrice/sessions-responder.py',
                     '/etc/systemd/system/atrice-sessions.service',
                     '/etc/udev/rules.d/60-atrice-control.rules',
                     '/usr/lib/tmpfiles.d/atrice-sessions.conf',
                     '/etc/profile.d/atrice-tmux.sh'):
            self.assertIn(path, code)
        for foreign in ('ssh_host', 'credentials', 'profile.json', '/etc/hosts',
                        'authorized_keys', 'machine-id', '/home/'):
            self.assertNotIn(foreign, code)


if __name__ == '__main__':
    unittest.main()
