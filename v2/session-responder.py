"""Гостевая служба прямого входа в управляемую сессию Atrice.

Зачем она есть. Единственным путём в гостя была serial-консоль, и пока в ней
работает приложение или клиент tmux живой сессии, отправить туда что-либо
нельзя: байты ушли бы этому процессу как нажатия клавиш. Поэтому ни создать
сессию рядом, ни войти в уже живую при занятом serial было нечем. Служба даёт
второй, независимый терминальный путь: отдельный порт virtio-serial, свой pty
и свой клиент tmux. Serial-консоли она не касается вовсе.

Чего в ней нет намеренно. Произвольных команд: argv клиента фиксирован в коде,
из запроса берётся только имя сессии по контракту канала и размеры окна.
Оболочки нет — exec идёт списком аргументов. Сети нет структурно
(PrivateNetwork=yes), портов не публикуется, туннелей не открывается. Канал
списка сессий (atrice.control) не затрагивается и остаётся читающим: это
отдельный порт и отдельная служба, и включается она явным шагом оператора.

Протокол. Один порт — один поток, поэтому обе стороны обмениваются кадрами:
тип (1 байт), длина (4 байта, big-endian), содержимое. Типы: 1 — байты
терминала, 2 — типизированное управление строкой JSON, 3 — keepalive без
содержимого. Первым кадром хост присылает управление:

    {"api":1,"op":"open","session":"claude-<16 hex>","rows":40,"cols":120}
    {"api":1,"op":"ping"}

Ответ — одна строка JSON: {"api":1,"ok":true,"session":...,"created":bool}
или {"api":1,"ok":false,"error":"..."}. Дальше идут кадры терминала, а в конце
служба присылает {"api":1,"event":"closed","status":N}. Во время работы хост
может прислать {"op":"resize","rows":R,"cols":C} и {"op":"detach"}.

Открытие идёт через tmux new-session -A: с тем же именем он подключается к
существующей сессии, а не создаёт вторую. Поэтому повторный запрос не плодит
случайных дублей. Служба подключается к серверу tmux, который запущен
отдельно под пользователем agent и создаёт процессы Claude.

Уход хоста. На порту virtio уход клиента изнутри гостя не виден, поэтому хост
присылает keepalive, а служба считает его пропажу уходом клиента: клиенту tmux
посылается SIGHUP, он отсоединяется, сессия и агент в ней остаются живы. Так же
обрабатывается явный detach. Следующий вход подключается к той же сессии.

Коды выхода: 13 — нет прав на порт, 14 — порта нет.
"""

import errno
import fcntl
import json
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import time

PORT = os.environ.get('AGENT_DOCKER_SESSION_PORT', '/dev/virtio-ports/atrice.session')
# Сокет tmux тот же, что у агентов и у канала списка: служба не создаёт свой.
SOCKET_PATH = os.environ.get('AGENT_DOCKER_TMUX_SOCKET_PATH',
                             '/run/atrice/sessions/atrice')
TMUX = os.environ.get('AGENT_DOCKER_TMUX', 'tmux')
# Имя сессии — по контракту канала: ни точки, ни двоеточия, ни пробелов.
SESSION_NAME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,63}$')
API = 1
FRAME_DATA = 1
FRAME_CONTROL = 2
FRAME_KEEPALIVE = 3
CONTROL_LIMIT = 4096
DATA_LIMIT = 65536
# Сколько ждать keepalive, прежде чем считать клиента ушедшим.
CLIENT_TIMEOUT = float(os.environ.get('AGENT_DOCKER_SESSION_TIMEOUT', 5))
# Размеры окна: границы те же, что у консоли.
MIN_ROWS, MIN_COLS = 24, 80
MAX_ROWS, MAX_COLS = 1000, 1000


class Gone(Exception):
    """Хост ушёл: клиент tmux отсоединяется, сессия остаётся жить."""


class Replacement(Gone):
    """Новый запрос уже прочитан и ожидает обработки после отсоединения."""


def frame(kind, payload=b''):
    return struct.pack('>BI', kind, len(payload)) + payload


def control_frame(message):
    return frame(FRAME_CONTROL, json.dumps(message, ensure_ascii=False).encode())


class Port:
    """Кадры на одном непозиционируемом порту virtio.

    Порт открывается одним дескриптором: текстовый режим r+ строит
    BufferedRandom, которому нужен seek, и на таком устройстве это даёт
    io.UnsupportedOperation. Поэтому чтение идёт os.read без буферизации, а
    незавершённый кадр остаётся в буфере до следующего чтения.
    """

    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.buffer = b''

    def send(self, data):
        """Пишет кадр, не зависая навсегда на неготовом порту.

        Ушедший хост на порту virtio не виден, и запись при отключённом
        клиенте может не приниматься. Обычная блокирующая запись оставляла
        службу висеть внутри сессии: она больше не отвечала на запросы, и
        повторный вход становился невозможен до перезапуска. Поэтому запись
        ограничена сроком: истёк — считаем, что хост ушёл.
        """
        deadline = time.monotonic() + CLIENT_TIMEOUT
        while data:
            left = deadline - time.monotonic()
            if left <= 0:
                raise Gone
            _, ready, _ = select.select([], [self.descriptor], [], left)
            if not ready:
                continue
            data = data[os.write(self.descriptor, data):]

    def fill(self):
        chunk = os.read(self.descriptor, 65536)
        if not chunk:
            raise Gone
        self.buffer += chunk

    def take(self):
        """Один разобранный кадр или None, если он ещё не пришёл целиком."""
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


def parsed(payload):
    """Типизированное управление: только объект JSON нашей версии."""
    if len(payload) > CONTROL_LIMIT:
        return None
    try:
        message = json.loads(payload.decode('utf-8', 'replace'))
    except ValueError:
        return None
    if not isinstance(message, dict):
        return None
    return message


def window(message):
    """Размеры окна из запроса, приведённые к разумным границам."""
    rows, cols = message.get('rows'), message.get('cols')
    if not isinstance(rows, int) or not isinstance(cols, int):
        return MIN_ROWS, MIN_COLS
    return (min(max(rows, MIN_ROWS), MAX_ROWS), min(max(cols, MIN_COLS), MAX_COLS))


def set_size(descriptor, rows, cols):
    try:
        fcntl.ioctl(descriptor, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
    except OSError:
        pass


def tmux_check(*arguments):
    """Ограниченная по времени проверка существующего сервера tmux."""
    try:
        result = subprocess.run(
            [TMUX, '-N', '-S', SOCKET_PATH, *arguments],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def start_client(name, rows, cols):
    """Клиент tmux на своём pty: argv фиксирован, оболочки нет.

    new-session -A подключается к сессии с этим именем, если она есть, и
    создаёт её, если нет. Прежние сессии не затрагиваются.
    """
    child, descriptor = pty.fork()
    if child == 0:                                    # pragma: no cover - в ребёнке
        try:
            os.environ['ATRICE_TMUX_SOCKET'] = SOCKET_PATH
            os.environ['TERM'] = os.environ.get('TERM') or 'xterm-256color'
            os.execvp(TMUX, [TMUX, '-N', '-S', SOCKET_PATH, 'new-session', '-A',
                             '-s', name, '--', 'claude'])
        finally:
            os._exit(127)
    set_size(descriptor, rows, cols)
    return child, descriptor


def finish(child, descriptor):
    """Отсоединяет клиента, не касаясь сессии и работающего в ней агента.

    SIGHUP завершает именно клиента tmux: сессия tmux переживает уход своего
    клиента, поэтому агент продолжает работать, а следующий вход подключается
    к той же сессии.
    """
    try:
        os.kill(child, signal.SIGHUP)
    except OSError:
        pass
    deadline = time.monotonic() + 3
    status = None
    while time.monotonic() < deadline:
        finished, state = os.waitpid(child, os.WNOHANG)
        if finished:
            status = state
            break
        time.sleep(0.05)
    if status is None:
        try:
            os.kill(child, signal.SIGKILL)
        except OSError:
            pass
        status = os.waitpid(child, 0)[1]
    try:
        os.close(descriptor)
    except OSError:
        pass
    return os.waitstatus_to_exitcode(status) if status is not None else 0


def writable(descriptor, deadline):
    """Ждёт готовности pty к записи, не зависая навсегда.

    Клиент на том конце может перестать читать, и обычная запись в pty тогда
    блокируется без срока: служба замерла бы вместе с ним и не заметила ухода
    хоста. Истёкший срок означает заклинивший клиент — его отсоединяют, а
    сессия и работающий в ней агент остаются жить.
    """
    left = deadline - time.monotonic()
    if left <= 0:
        raise Gone
    _, ready, _ = select.select([], [descriptor], [], left)
    return bool(ready)


def send_to_client(descriptor, payload):
    deadline = time.monotonic() + CLIENT_TIMEOUT
    while payload:
        if not writable(descriptor, deadline):
            continue
        payload = payload[os.write(descriptor, payload):]


def relay(port, child, descriptor):
    """Кадры порта и байты pty навстречу друг другу до ухода хоста."""
    seen = time.monotonic()
    partial = None
    while True:
        readable, _, _ = select.select([port.descriptor, descriptor], [], [], 1)
        now = time.monotonic()
        if port.descriptor in readable:
            port.fill()
            while True:
                taken = port.take()
                if taken is None:
                    # Кадр пришёл не целиком. Если он не собирается слишком
                    # долго, поток рассинхронизирован — продолжать нельзя:
                    # байты ушли бы клиенту как попало.
                    if port.buffer:
                        partial = partial or now
                        if now - partial > CLIENT_TIMEOUT:
                            raise Gone
                    else:
                        partial = None
                    break
                partial = None
                kind, payload = taken
                if kind == FRAME_CONTROL:
                    message = parsed(payload)
                    if not isinstance(message, dict):
                        continue
                    if message.get('op') in ('open', 'ping'):
                        # Это уже другой клиент: прежний хост ушёл, не
                        # попрощавшись. Его сессию отпускаем, а запрос
                        # возвращаем в буфер — ответит на него serve.
                        # Без этого запросы нового клиента обновляли счётчик
                        # keepalive, мёртвая сессия жила вечно, и повторный
                        # вход становился невозможен.
                        port.buffer = frame(FRAME_CONTROL, payload) + port.buffer
                        raise Replacement
                    seen = now
                    if message.get('op') == 'resize':
                        set_size(descriptor, *window(message))
                    elif message.get('op') == 'detach':
                        return
                    continue
                # Признак живого хоста — только кадры его сессии.
                seen = now
                if kind == FRAME_DATA:
                    # Байты терминала идут клиенту tmux, и только ему.
                    send_to_client(descriptor, payload)
        if descriptor in readable:
            try:
                data = os.read(descriptor, 65536)
            except OSError as error:
                if error.errno in (errno.EIO, errno.EBADF):
                    raise Gone from error
                raise
            if not data:
                raise Gone
            try:
                port.send(frame(FRAME_DATA, data))
            except Gone:
                # Порт не принимает: хост ушёл. Вывод сессии дальше не идёт,
                # но сама сессия и агент в ней продолжают работать.
                raise
        if now - seen > CLIENT_TIMEOUT:
            # Keepalive пропал: уход клиента изнутри гостя иначе не виден.
            raise Gone


def serve(port):
    """Один запрос: ping или открытие сессии с последующим релеем."""
    while True:
        taken = port.take()
        if taken is not None:
            break
        port.fill()
    kind, payload = taken
    if kind != FRAME_CONTROL:
        port.send(control_frame({'api': API, 'ok': False, 'error': 'ожидалось управление'}))
        return
    message = parsed(payload)
    if not isinstance(message, dict) or message.get('api') != API:
        port.send(control_frame({'api': API, 'ok': False, 'error': 'версия протокола не та'}))
        return
    operation = message.get('op')
    # Метка запроса возвращается во всех ответах на него: кадр, оставшийся от
    # прошлого входа, не должен быть принят следующим за ответ ему.
    label = message.get('id') if isinstance(message.get('id'), str) else None
    if operation == 'ping':
        port.send(control_frame({'api': API, 'ok': tmux_check('show-options', '-s', 'exit-empty'),
                                 'service': 'session', 'id': label}))
        return
    if operation != 'open':
        port.send(control_frame({'api': API, 'ok': False, 'id': label,
                                 'error': 'операция не поддержана'}))
        return
    name = message.get('session')
    if not isinstance(name, str) or not SESSION_NAME.match(name):
        port.send(control_frame({'api': API, 'ok': False, 'id': label,
                                 'error': 'имя сессии не подходит'}))
        return
    if not tmux_check('show-options', '-s', 'exit-empty'):
        port.send(control_frame({'api': API, 'ok': False, 'id': label,
                                 'error': 'сервер tmux недоступен'}))
        return
    rows, cols = window(message)
    created = not tmux_check('has-session', '-t', '=' + name)
    child, descriptor = start_client(name, rows, cols)
    try:
        port.send(control_frame({'api': API, 'ok': True, 'id': label,
                                 'session': name, 'created': created}))
        try:
            relay(port, child, descriptor)
        except Replacement:
            raise
        except Gone:
            port.buffer = b''
    finally:
        status = finish(child, descriptor)
    try:
        port.send(control_frame({'api': API, 'event': 'closed', 'id': label, 'status': status}))
    except Gone:
        # Хоста уже нет: сообщать некому, и это не мешает принять следующий
        # запрос — сессия и агент в ней остались живы.
        pass


def main():
    try:
        descriptor = os.open(PORT, os.O_RDWR)
    except PermissionError:
        print('нет прав на %s' % PORT, file=sys.stderr)
        return 13
    except FileNotFoundError:
        print('порта %s нет' % PORT, file=sys.stderr)
        return 14
    port = Port(descriptor)
    while True:
        try:
            serve(port)
        except Replacement:
            continue
        except Gone:
            port.buffer = b''
        except OSError as error:
            if error.errno not in (errno.EIO, errno.ENXIO):
                raise
            port.buffer = b''


if __name__ == '__main__':
    raise SystemExit(main())
