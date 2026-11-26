"""Гостевая служба канала сессий: отвечает списком живых сессий tmux.

Служба намеренно бедна. Она принимает две операции — hello и sessions, —
выполняет только читающие команды tmux с фиксированным списком аргументов и
никогда не запускает оболочку. Ни send-keys, ни kill-session, ни attach здесь
нет: канал существует, чтобы смотреть, а не управлять, и работающий агент не
должен замечать опрос.

Устройство канала — /dev/virtio-ports/atrice.control. Сети служба не
открывает и портов не слушает; запускается от обычного пользователя, под тем
же UID, что и агенты, иначе не увидит их сервер tmux.

Сокет tmux один и общий: /run/atrice/sessions/atrice. Каталог сокета создаётся
заранее, systemd-tmpfiles, и ни агент, ни служба его не создают — служба при
ProtectSystem=strict и не может. Поэтому ответ не зависит от того, запускался
ли tmux в этом госте хоть раз: отсутствие сокета определяется stat по точному
пути, без вызова tmux, и означает пустой список, а отказ доступа к сокету
остаётся отказом.

Про модель потока. Порт virtio — двунаправленное непозиционируемое
устройство, и open(port, 'r+') для него не годится: текстовый режим r+ строит
BufferedRandom, которому нужен seek, и на непозиционируемом устройстве это
даёт io.UnsupportedOperation. Поэтому порт открывается одним файловым
дескриптором через os.open и читается os.read без буферизации: чтение идёт до
перевода строки, запись — целиком одним вызовом.

Коды выхода: 13 — нет прав на порт, 14 — порта нет. Служба в этих случаях
завершается, а перезапуск оставлен systemd: причина видна в journalctl.
"""

import json
import os
import stat
import subprocess
import sys

PORT = os.environ.get('AGENT_DOCKER_CONTROL_PORT', '/dev/virtio-ports/atrice.control')
# Сокет tmux задаётся полным путём, а не меткой -L. Метка заставляет tmux
# самому создать каталог сокета (<TMUX_TMPDIR>/tmux-<uid>), и на чистой Debian
# KVM первый запрос до любого запуска tmux падал именно на этом: у службы
# ProtectSystem=strict, вся иерархия только для чтения, mkdir отвечает EROFS,
# и отказ выглядел как tmux-failed. Обычный вызов tmux в консоли готовил
# каталог, после чего тот же запрос отвечал пустым списком — то есть ответ
# зависел от первого запуска tmux. Каталог теперь создаётся заранее
# (/usr/lib/tmpfiles.d/atrice-sessions.conf) и общий для агентов и службы, а с
# -S tmux каталогов не создаёт вовсе: сюда он только подключается.
SOCKET_PATH = os.environ.get('AGENT_DOCKER_TMUX_SOCKET_PATH',
                             '/run/atrice/sessions/atrice')
# Прежний путь сокета по метке atrice в /tmp. Он проверяется вторым и только
# на существование: гость или стенд, где сессия уже запущена прежним способом,
# не должен остаться без списка. Пустое значение отключает поиск.
LEGACY_SOCKET_PATH = os.environ.get('AGENT_DOCKER_TMUX_SOCKET_LEGACY',
                                    '/tmp/tmux-%d/atrice' % os.getuid())
REQUEST_LIMIT = 4096
API = 1
# Разделитель печатный намеренно. Управляющий байт здесь не годится: tmux 3.5a
# экранирует непечатные символы в выводе -F, и ASCII US превращался в четыре
# буквальных байта \037. Разбор не находил разделителя, отбрасывал все строки
# и отдавал пустой список — живые сессии выглядели как их отсутствие.
SEPARATOR = '|@|'
# Свободное поле идёт последним и отделяется с ограничением числа частей:
# пробелы и неожиданные символы в имени команды тогда не ломают остальные поля.
LIST_FORMAT = SEPARATOR.join(['#{session_name}', '#{session_created}',
                              '#{session_attached}', '#{pane_current_command}'])
# Признаки в stderr tmux. Порядок важен: отказ доступа проверяется первым,
# иначе "error connecting to ... (Permission denied)" попал бы в отсутствие
# сервера и отказ превратился бы в пустой список.
DENIED = ('permission denied',)
# Сервера ещё нет: до первого запуска сокета просто не существует. tmux 3.5a
# в этом случае пишет "error connecting to <путь> (No such file or directory)",
# а запущенный ранее и закрытый сервер даёт "no server running on <путь>".
ABSENT = ('no server running', 'no such file or directory')


class Channel:
    """Двунаправленный непозиционируемый поток поверх одного дескриптора."""

    def __init__(self, descriptor):
        self.descriptor = descriptor

    def read_line(self, limit=REQUEST_LIMIT):
        """Читает запрос до перевода строки, не выходя за предел размера."""
        collected = bytearray()
        while b'\n' not in collected:
            if len(collected) >= limit:
                return bytes(collected[:limit])
            piece = os.read(self.descriptor, 1)
            if not piece:
                break
            collected += piece
        return bytes(collected)

    def write_line(self, data):
        """Пишет ответ целиком: короткие записи дописываются."""
        payload = data if data.endswith(b'\n') else data + b'\n'
        sent = 0
        while sent < len(payload):
            sent += os.write(self.descriptor, payload[sent:])


def tmux_environment():
    """Окружение для tmux: C-локаль, чтобы stderr разбирался предсказуемо.

    Строку ошибки даёт strerror, и в другой локали она переводится. Разбор по
    переведённому тексту не срабатывал, и отсутствие сервера выглядело как
    неизвестный отказ.
    """
    environment = dict(os.environ)
    environment['LC_ALL'] = 'C'
    environment['LANG'] = 'C'
    return environment


def tmux(socket_path, *arguments):
    """Выполняет читающую команду tmux без оболочки на названном сокете."""
    return subprocess.run(['tmux', '-S', socket_path, *arguments],
                          capture_output=True, text=True, timeout=5,
                          env=tmux_environment())


def socket_candidates():
    """Пути сокета по порядку: выделенный каталог, затем прежний в /tmp."""
    paths = [SOCKET_PATH]
    if LEGACY_SOCKET_PATH and LEGACY_SOCKET_PATH != SOCKET_PATH:
        paths.append(LEGACY_SOCKET_PATH)
    return paths


def socket_state():
    """Ищет сокет сервера tmux, не запуская tmux.

    Возвращает пару (состояние, путь): 'present' — сокет на месте, 'absent' —
    точного сокета нет ни по одному пути, 'denied' — путь есть, но доступ к
    нему закрыт, 'unknown' — по пути лежит не сокет или stat отказал иначе.

    Проверка именно точного пути и именно через stat: отсутствие сокета — это
    отсутствие сессий, а отказ доступа маскировать пустым списком нельзя, и
    различить их по тексту stderr tmux нельзя тем более, потому что до него
    дело не доходит — tmux с меткой падает раньше, на создании каталога.
    """
    denied = None
    strange = None
    for path in socket_candidates():
        try:
            found = os.stat(path)
        except (FileNotFoundError, NotADirectoryError):
            continue
        except PermissionError:
            # Отказ доступа запоминается и не превращается в пустой список:
            # сессии могут быть живы, просто служба их не видит.
            denied = denied or path
            continue
        except OSError:
            strange = strange or path
            continue
        if stat.S_ISSOCK(found.st_mode):
            return 'present', path
        # По пути сокета лежит обычный файл или каталог: это не сервер tmux и
        # не его отсутствие, а неисправная настройка, и молчать о ней нельзя.
        strange = strange or path
    if denied is not None:
        return 'denied', denied
    if strange is not None:
        return 'unknown', strange
    return 'absent', None


def classify_failure(stderr):
    """Различает отказ доступа, отсутствие сервера и прочие отказы tmux."""
    problem = stderr.lower()
    if any(mark in problem for mark in DENIED):
        return 'denied'
    if any(mark in problem for mark in ABSENT):
        return 'absent'
    return 'unknown'


def version():
    try:
        result = subprocess.run(['tmux', '-V'], capture_output=True, text=True,
                                timeout=5, env=tmux_environment())
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip().removeprefix('tmux ') or None


def sessions():
    """Список живых сессий.

    Возвращает пару (список записей, число пропущенных строк), 'denied' при
    отказе доступа к серверу tmux и 'unparsed', если tmux что-то напечатал, но
    ни одна строка не разобралась. Последнее важно отличать от пустого списка:
    именно так молча терялись живые сессии, когда формат вывода не совпал.

    Подтверждённое отсутствие сервера — это успех с пустым списком, а не
    отказ: до первого запуска агента сессий просто нет. Этот случай решается
    до вызова tmux, по точному пути сокета, поэтому ответ не зависит от того,
    запускал ли кто-нибудь tmux в этом госте раньше.
    """
    state, path = socket_state()
    if state == 'denied':
        return 'denied'
    if state == 'unknown':
        return None
    if state == 'absent':
        # Сокета нет: сервера нет, сессий нет. tmux не вызывается вовсе.
        return [], 0
    try:
        result = tmux(path, 'list-sessions', '-F', LIST_FORMAT)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        failure = classify_failure(result.stderr)
        # Служба работает не под тем UID и чужой сокет ей недоступен.
        if failure == 'denied':
            return 'denied'
        # Сервера нет — это не ошибка, а пустой список. Пара возвращается и
        # здесь: распаковка в answer одинакова для всех успешных ответов.
        if failure == 'absent':
            return [], 0
        return None
    found = []
    skipped = 0
    for line in result.stdout.splitlines():
        fields = line.split(SEPARATOR, 3)
        if len(fields) != 4:
            skipped += 1
            continue
        try:
            created = int(fields[1])
        except ValueError:
            skipped += 1
            continue
        found.append({'name': fields[0], 'created': created,
                      'attached': fields[2] == '1', 'command': fields[3]})
    if skipped and not found:
        return 'unparsed'
    return found, skipped


def answer(request):
    installed = version()
    if installed is None:
        return {'ok': False, 'api': API, 'error': 'no-tmux'}
    operation = request.get('op') if isinstance(request, dict) else None
    if operation == 'hello':
        # socket — добавка в рамках api 1: состояние сокета видно до запроса
        # списка, и диагностика на хосте различает «сессий нет» и «служба не
        # видит сокет». Читатель, который поля не знает, его не заметит.
        return {'ok': True, 'api': API, 'tmux': installed,
                'socket': socket_state()[0]}
    if operation != 'sessions':
        return {'ok': False, 'api': API, 'error': 'unknown-op'}
    found = sessions()
    if found == 'denied':
        return {'ok': False, 'api': API, 'error': 'no-permission'}
    if found == 'unparsed':
        return {'ok': False, 'api': API, 'error': 'format-unparsed'}
    if found is None:
        return {'ok': False, 'api': API, 'error': 'tmux-failed'}
    listed, skipped = found
    # skipped — необязательное поле в рамках api 1: читатель, который его не
    # знает, просто его не заметит, а знающий покажет, что часть строк
    # tmux разобрать не удалось.
    return {'ok': True, 'api': API, 'sessions': listed, 'skipped': skipped}


def serve(channel):
    """Обслуживает один запрос: строка на вход, строка на выход."""
    line = channel.read_line()
    if not line.strip():
        return False
    try:
        request = json.loads(line)
    except (json.JSONDecodeError, UnicodeDecodeError):
        request = None
    channel.write_line(json.dumps(answer(request), ensure_ascii=False).encode())
    return True


def main():
    try:
        descriptor = os.open(PORT, os.O_RDWR | os.O_NOCTTY)
    except PermissionError:
        print('Нет прав на %s: служба должна работать под UID владельца порта.' % PORT,
              file=sys.stderr)
        raise SystemExit(13)
    except FileNotFoundError:
        print('Порт %s не найден: гость запущен без канала сессий.' % PORT, file=sys.stderr)
        raise SystemExit(14)
    try:
        while True:
            serve(Channel(descriptor))
    finally:
        os.close(descriptor)


if __name__ == '__main__':
    main()
