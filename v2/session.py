"""Прямой вход в управляемую сессию Atrice со стороны контейнера.

Это второй, независимый от serial-консоли путь в гостя: отдельный порт
virtio-serial (/guest/session.sock со стороны контейнера), на гостевой стороне
— служба atrice-session с собственным pty и собственным клиентом tmux. Поэтому
войти в сессию и создать новую можно и тогда, когда serial-оболочка занята
приложением или клиентом живой сессии: в serial здесь не уходит ни один байт,
и блокировка консоли (/guest/console.lock) не берётся вовсе.

Произвольных команд этот путь не передаёт: в запросе есть только имя сессии по
контракту канала и размеры окна, а команда клиента фиксирована в гостевой
службе. Канал списка сессий (atrice.control) не затрагивается и остаётся
читающим.

Операции:
    probe              одна строка JSON: ready, silent или absent — есть ли путь;
    open '' <имя>      открыть сессию с этим именем, создав её при отсутствии;
    --release '' <токен>  завершить реле этого входа, если его клиент ушёл.

Уход клиента различается двумя путями, как и у консоли. Исчезнувшее устройство
pty видно изнутри: чтение отдаёт EIO или EOF, узел /dev/pts/N пропадает. А
ушедший клиент docker exec при открытом мастере изнутри контейнера не виден
вовсе: реле продолжает работать и подтверждать путь keepalive, поэтому гостевая
служба держит клиента tmux подключённым, сессия остаётся attached, а проверка
пути отвечает silent. Про такой уход сообщает хост: manage.sh выдаёт каждому
входу токен (AGENT_DOCKER_SESSION_TOKEN) и по ловушке на HUP, TERM и выходе
вызывает session.py --release <токен>. Освобождается ровно то реле, которое
этот вход и запустил: совпадение по токену в окружении процесса и по его
командной строке. Чужое живое подключение не трогается.

Выход из сессии — Ctrl+], как и в консоли: клиент tmux в госте отсоединяется,
сессия и работающий в ней агент остаются живы, следующий вход подключается к
той же сессии. Размер окна передаётся кадром resize и доходит до клиента tmux
живой сессии: pty здесь наш, поэтому SIGWINCH работает как в обычном
терминале.
"""

import errno
import json
import os
import re
import select
import signal
import socket
import struct
import sys
import termios
import time
import tty
from pathlib import Path

from terminal_stream import TERMINAL_RESET, Stream

API = 1
FRAME_DATA = 1
FRAME_CONTROL = 2
FRAME_KEEPALIVE = 3
DATA_LIMIT = 65536
CONTROL_LIMIT = 4096
DETACH = 0x1d
# Сокет порта со стороны контейнера. Его создаёт QEMU при запуске гостя,
# поэтому у гостя, запущенного прежней сборкой, его нет — это и значит absent.
SOCKET = os.environ.get('AGENT_DOCKER_SESSION_SOCKET', '/guest/session.sock')
SESSION_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,63}$')
PROBE_DEADLINE = float(os.environ.get('AGENT_DOCKER_SESSION_PROBE', 3))
OPEN_DEADLINE = float(os.environ.get('AGENT_DOCKER_SESSION_OPEN', 10))
KEEPALIVE = float(os.environ.get('AGENT_DOCKER_SESSION_KEEPALIVE', 1))
# Токен этого входа: его выдаёт manage.sh и по нему же просит освободить реле.
TOKEN = os.environ.get('AGENT_DOCKER_SESSION_TOKEN', '')
TOKEN_NAME = 'AGENT_DOCKER_SESSION_TOKEN'
# Сколько ждать, пока завершаемое реле уйдёт само.
RELEASE_TIMEOUT = float(os.environ.get('AGENT_DOCKER_SESSION_RELEASE', 3))


class Gone(Exception):
    """Путь закрылся: гость отсоединил клиента или ушёл сам."""


class Leaving(Exception):
    """Нас просят уйти: HUP, TERM или освобождение с хоста.

    Это не отказ: сессия и агент в ней остаются жить, а клиента tmux гостевая
    служба отсоединит по нашему последнему кадру detach.
    """


def frame(kind, payload=b''):
    return struct.pack('>BI', kind, len(payload)) + payload


def control_frame(message):
    return frame(FRAME_CONTROL, json.dumps(message, ensure_ascii=False).encode())


class Link:
    """Кадры на сокете порта: тип, длина, содержимое."""

    def __init__(self, link):
        self.link = link
        self.buffer = b''

    def send(self, data):
        self.link.sendall(data)

    def take(self):
        if len(self.buffer) < 5:
            return None
        kind, length = struct.unpack('>BI', self.buffer[:5])
        if length > DATA_LIMIT:
            raise Gone
        if len(self.buffer) < 5 + length:
            return None
        payload = self.buffer[5:5 + length]
        self.buffer = self.buffer[5 + length:]
        return kind, payload

    def fill(self):
        chunk = self.link.recv(65536)
        if not chunk:
            raise Gone
        self.buffer += chunk

    def control(self, deadline, label=None):
        """Ждёт одно управляющее сообщение до срока.

        Сообщение с чужой меткой запроса пропускается: в порту мог остаться
        кадр прошлого входа, и принять его за ответ на свой запрос нельзя.
        Служба прежней сборки метку не возвращает — такой ответ принимается.
        """
        end = time.monotonic() + deadline
        while True:
            taken = self.take()
            if taken is not None:
                kind, payload = taken
                if kind != FRAME_CONTROL or len(payload) > CONTROL_LIMIT:
                    continue
                try:
                    message = json.loads(payload.decode('utf-8', 'replace'))
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if label is not None and message.get('id') not in (None, label):
                    continue
                return message
            left = end - time.monotonic()
            if left <= 0:
                return None
            self.link.settimeout(left)
            try:
                self.fill()
            except (TimeoutError, socket.timeout):
                return None


def terminal_gone(descriptor):
    """Исчезло ли устройство терминала.

    Закрытый мастер pty удаляет узел /dev/pts/N, и это видно без всякой записи
    в терминал. Живое подключение — хоть работающее, хоть простаивающее — узел
    сохраняет, поэтому проверка его не отключает.
    """
    try:
        name = os.ttyname(descriptor)
    except OSError:
        return True
    return not os.path.exists(name)


def leaving(number, frame_object):
    """HUP и TERM означают уход клиента, а не отказ команды."""
    raise Leaving


def relay_processes(token):
    """Процессы реле этого входа: совпадение по токену и командной строке.

    Условия намеренно строгие: это должен быть процесс нашего session.py в
    операции open, и в его окружении должен стоять ровно тот токен, который
    выдали этому входу. Иначе не делается ничего — чужое живое подключение
    освобождение не трогает.
    """
    found = []
    if not token:
        return found
    needle = ('%s=%s' % (TOKEN_NAME, token)).encode()
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        try:
            command = Path('/proc/%d/cmdline' % pid).read_bytes().split(b'\0')
            environ = Path('/proc/%d/environ' % pid).read_bytes()
        except OSError:
            continue
        if not any(part.endswith(b'session.py') for part in command):
            continue
        if b'open' not in command:
            continue
        if needle not in environ.split(b'\0'):
            continue
        found.append(pid)
    return found


def release(token):
    """Завершает реле этого входа: его клиент ушёл, а оно осталось.

    Ушедший клиент docker exec изнутри контейнера не виден, и реле продолжало
    подтверждать путь keepalive: сессия оставалась attached, а проверка пути
    отвечала silent, пока процесс не завершали руками. Теперь об уходе
    сообщает хост по ловушке, и завершается ровно названное реле.
    """
    for pid in relay_processes(token):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
        deadline = time.monotonic() + RELEASE_TIMEOUT
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.05)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return 0


def connect():
    link = socket.socket(socket.AF_UNIX)
    link.connect(SOCKET)
    return link


def probe():
    """Есть ли прямой путь: absent — порта нет, silent — служба не ответила."""
    state = 'absent'
    try:
        with connect() as link:
            channel = Link(link)
            label = os.urandom(8).hex()
            channel.send(control_frame({'api': API, 'op': 'ping', 'id': label}))
            answer = channel.control(PROBE_DEADLINE, label)
            state = 'ready' if answer and answer.get('ok') is True else 'silent'
    except (FileNotFoundError, ConnectionRefusedError):
        state = 'absent'
    except OSError:
        state = 'silent'
    print(json.dumps({'api': API, 'direct': state}, ensure_ascii=False))
    return 0


def size(descriptor):
    try:
        window = os.get_terminal_size(descriptor)
    except OSError:
        return 24, 80
    return max(window.lines, 24), max(window.columns, 80)


def restore(descriptor):
    try:
        os.write(descriptor, TERMINAL_RESET)
    except OSError:
        pass


def relay(channel, descriptor, wakeup):
    """Байты терминала и кадры порта навстречу друг другу до Ctrl+].

    Все кадры отправляет только этот цикл. Обработчик SIGWINCH сюда не пишет:
    запись из обработчика шла бы вперемешку с кадром, который цикл отправляет в
    этот момент, поток на стороне гостя терял синхронизацию, и вход зависал.
    Обработчик лишь запоминает размер и будит цикл через самопровод.
    """
    beat = 0.0
    stream = Stream(os.environ.get('AGENT_DOCKER_CONSOLE_MOUSE') == 'off')
    while True:
        now = time.monotonic()
        if now - beat >= KEEPALIVE:
            # Уход клиента изнутри гостя не виден, поэтому путь подтверждается
            # keepalive: без него служба отсоединит клиента сама.
            channel.send(frame(FRAME_KEEPALIVE))
            beat = now
        size_now = pending.pop('size', None)
        if size_now is not None:
            channel.send(control_frame({'api': API, 'op': 'resize',
                                        'rows': size_now[0], 'cols': size_now[1]}))
        readable, _, _ = select.select([channel.link, descriptor, wakeup], [], [], KEEPALIVE)
        if not readable and terminal_gone(descriptor):
            # Устройство терминала пропало: клиент закрыл его, не нажав
            # Ctrl+]. Гостю об этом скажет наш последний кадр detach.
            raise Leaving
        if wakeup in readable:
            try:
                os.read(wakeup, 4096)
            except OSError:
                pass
        if channel.link in readable:
            channel.fill()
            while True:
                taken = channel.take()
                if taken is None:
                    break
                kind, payload = taken
                if kind == FRAME_DATA:
                    os.write(sys.stdout.fileno(), stream.clean(payload))
                elif kind == FRAME_CONTROL:
                    raise Gone
        if descriptor in readable:
            try:
                data = os.read(descriptor, 1024)
            except OSError as error:
                if error.errno in (errno.EIO, errno.ENXIO, errno.EBADF):
                    raise Gone
                raise
            if not data:
                raise Gone
            before, escape, _ = data.partition(bytes([DETACH]))
            if before:
                channel.send(frame(FRAME_DATA, before))
            if escape:
                # Ctrl+]: клиента отсоединяет сама служба, сессия остаётся жить.
                channel.send(control_frame({'api': API, 'op': 'detach'}))
                return


# Размер окна, замеченный обработчиком сигнала: отправляет его сам цикл релея.
pending = {}


def resized(descriptor, wakeup):
    """Обработчик SIGWINCH: только запомнить размер и разбудить цикл."""
    def handler(number, frame_object):
        pending['size'] = size(descriptor)
        try:
            os.write(wakeup, b'!')
        except OSError:
            pass

    return handler


def open_session(name):
    if not SESSION_NAME.match(name or ''):
        raise SystemExit('Имя сессии не подходит: ожидались буквы, цифры и дефис.')
    if not sys.stdin.isatty():
        raise SystemExit('Прямой вход в сессию требует интерактивного терминала.')
    try:
        link = connect()
    except (FileNotFoundError, ConnectionRefusedError):
        raise SystemExit('Прямого пути в сессии у этого гостя нет: '
                         'порт появляется при следующем запуске ВМ.')
    except OSError as error:
        raise SystemExit('Прямой путь в сессии недоступен: %s' % error)
    with link:
        channel = Link(link)
        descriptor = sys.stdin.fileno()
        rows, cols = size(descriptor)
        label = os.urandom(8).hex()
        channel.send(control_frame({'api': API, 'op': 'open', 'session': name,
                                    'rows': rows, 'cols': cols, 'id': label}))
        answer = channel.control(OPEN_DEADLINE, label)
        if answer is None:
            raise SystemExit('Служба прямого входа не ответила: она не установлена '
                             'или не запущена в этом госте.')
        if answer.get('ok') is not True:
            raise SystemExit('Прямой вход отказал: %s' %
                             (answer.get('error') or 'причина не названа'))
        saved = termios.tcgetattr(descriptor)
        read_end, write_end = os.pipe()
        os.set_blocking(write_end, False)
        try:
            tty.setraw(descriptor)
            if os.environ.get('AGENT_DOCKER_CONSOLE_MOUSE') == 'off':
                restore(sys.stdout.fileno())
            signal.signal(signal.SIGWINCH, resized(descriptor, write_end))
            signal.signal(signal.SIGHUP, leaving)
            signal.signal(signal.SIGTERM, leaving)
            print('\r\nСессия %s. Для выхода нажмите Ctrl+].\r'
                  % ('создана' if answer.get('created') else 'подключена'), flush=True)
            relay(channel, descriptor, read_end)
        except Leaving:
            # Уход без Ctrl+]: просим службу отсоединить клиента, чтобы сессия
            # сразу перестала быть подключённой и путь освободился.
            try:
                channel.send(control_frame({'api': API, 'op': 'detach'}))
            except OSError:
                pass
        except Gone:
            pass
        finally:
            for number in (signal.SIGWINCH, signal.SIGHUP, signal.SIGTERM):
                signal.signal(number, signal.SIG_DFL)
            for end in (read_end, write_end):
                try:
                    os.close(end)
                except OSError:
                    pass
            restore(sys.stdout.fileno())
            try:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
            except (termios.error, OSError):
                pass
    return 0


def main():
    operation = sys.argv[1] if len(sys.argv) > 1 else ''
    if operation == 'probe':
        return probe()
    if operation == '--release':
        # Третий аргумент — токен того входа, чьё реле просят завершить.
        return release(sys.argv[3] if len(sys.argv) > 3 else '')
    if operation == 'open':
        return open_session(sys.argv[3] if len(sys.argv) > 3 else '')
    raise SystemExit('Неизвестная операция прямого входа: %s' % operation)


if __name__ == '__main__':
    raise SystemExit(main())
