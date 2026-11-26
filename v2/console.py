"""Пользовательская консоль гостя и подключение к живой сессии tmux.

Отдельно стоит владение блокировкой /guest/console.lock. Пока консоль
работает, блокировка её, и второй вход гость отклоняет — иначе два клиента
писали бы в одну serial-консоль. Но закрытый хостовый терминал консоль не
всегда замечает: docker exec с tty оставляет мастер pty открытым на стороне
демона, никакого события в гостя не приходит, и процесс console.py остаётся
висеть в select, держа блокировку. Освобождал её только оператор вручную.

Поэтому потеря терминала определяется двумя путями, и они разные:
  - устройство pty закрыто (мастер закрыт, узел /dev/pts/N исчез) —
    это видно изнутри: чтение отдаёт EOF или EIO, а узла больше нет;
  - клиент docker exec ушёл, а мастер открыт — изнутри не видно ничего, и
    узел на месте. Такое знает только хост, поэтому его сообщает хост:
    manage.sh по ловушке на HUP, TERM и выходе вызывает --release с токеном
    своей сессии, и завершается ровно тот console.py, который этот же хостовый
    вход и запустил. Чужую работающую консоль --release не трогает: токен не
    совпадёт.
Гость при этом не затрагивается ни в одном из путей: в serial-сокет ничего не
пишется, поэтому запущенный агент и сессии tmux продолжают работать.
"""

import errno
import fcntl
import json
import os
import re
import select
import shlex
import signal
import socket
import sys
import termios
import time
import tty
from pathlib import Path

from terminal_stream import TERMINAL_RESET, Stream

# Аргументы задаёт manage.sh: операция, проект и значение RESUME. У операции
# --release третьим аргументом идёт токен хостовой сессии, а --shell только
# читает состояние оболочки и остальные аргументы не использует.
operation, project, resume = sys.argv[1:]
commands = {
    'claude-auth': ['claude', 'auth', 'login'],
    'claude-logout': ['claude', 'auth', 'logout'],
    'claude': ['claude'] + (['--resume', resume] if resume else []),
}
# Приватные режимы терминала (CSI ? ... h/l): отслеживание мыши, альтернативный
# экран, скобочная вставка. В переигранном хвосте журнала они опасны — включают
# режимы прежнего приложения в терминале хоста, из-за чего экран выглядит
# повреждённым, выделение мышью требует Shift, а при 1003 каждое движение мыши
# гонит байты в serial. В живом потоке их трогать нельзя: ими управляет само
# приложение.
private_mode = re.compile(rb'\x1b\[\?[0-9;]*[hl]')
# Режимы отслеживания мыши. Пока их включает приложение в госте, терминал
# хоста отдаёт ему щелчки и перемещения, и обычное выделение текста мышью
# становится недоступно — на большинстве терминалов остаётся только Shift.
# Это захват мыши приложением, а не залипание хоста: после выхода консоль
# режимы сбрасывает. Режим mouse=off оставляет мышь терминалу: включения
# снимаются из потока, приложение щелчков не получает, выделение работает.
mouse_policy = os.environ.get('AGENT_DOCKER_CONSOLE_MOUSE', 'app')

# Операции, которые работают в самой консоли, а не отправляют команду.
terminal_operations = ('vm-console', 'vm-session-open', 'vm-session-claude')
# Операции, которым имя сессии задаёт RESUME: подключение к живой сессии и
# создание новой управляемой сессии рядом с ней.
session_operations = ('vm-session-open', 'vm-session-claude')

# Каталог состояния гостя. Переменная нужна проверкам: они поднимают консоль
# на подставном serial-сокете, где настоящего /guest нет.
workspace = Path(os.environ.get('AGENT_DOCKER_GUEST_DIR') or '/guest')
ready_marker = b'\x1b]9999;agent-docker-shell-ready\x07'
checkpoint = workspace / 'console.pending'
serial_log = workspace / 'console.log'
console_lock = workspace / 'console.lock'
# Имя сессии tmux по контракту канала: ни точки, ни двоеточия, ни пробелов.
session_name = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,63}$')
# Сокет сессий тот же, что у агентов и гостевой службы. Значение из окружения
# гостя главнее: путь задан в /etc/profile.d/atrice-tmux.sh.
tmux_socket = '${ATRICE_TMUX_SOCKET:-/run/atrice/sessions/atrice}'
# Сколько ждать, пока завершаемый владелец отпустит блокировку.
release_timeout = float(os.environ.get('AGENT_DOCKER_RELEASE_TIMEOUT', 3))
# Как часто проверять, не исчез ли терминал, пока в консоли тихо.
watch_interval = float(os.environ.get('AGENT_DOCKER_WATCH_INTERVAL', 1))


class Detached(Exception):
    """Клиент ушёл: консоль закрывается, гостя не трогаем."""


def terminal_loss(error):
    """Отказ потерянного терминала, а не настоящая неисправность.

    Исчезнувшее устройство pty отвечает EIO, ENXIO или EBADF на чтение, на
    запись и на любой вызов termios. Такой отказ означает закрытый терминал и
    не должен ни падать трассировкой, ни выглядеть отказом команды.
    """
    lost = (errno.EIO, errno.ENXIO, errno.EBADF)
    if isinstance(error, termios.error):
        return bool(error.args) and error.args[0] in lost
    return isinstance(error, OSError) and error.errno in lost


def safe_tail(data):
    """Хвост журнала, начатый с целой строки.

    Окно хвоста режется по размеру и попадало в середину управляющей
    последовательности: её остаток терминал хоста понимал как мусор, и экран
    при переподключении выглядел повреждённым. Начало переносится на первую
    границу строки, а если её нет — на начало первой целой последовательности.
    """
    line = data.find(b'\n')
    if line >= 0:
        return data[line + 1:]
    escape = data.find(b'\x1b')
    return data[escape:] if escape >= 0 else data


def managed_name():
    """Имя новой управляемой сессии tmux: claude-<16 шестнадцатеричных знаков>."""
    return 'claude-' + os.urandom(8).hex()


def restore_terminal(descriptor):
    """Сбрасывает приватные режимы в своём терминале, не касаясь гостя.

    Приложение в госте включает отслеживание мыши и альтернативный экран, а при
    уходе клиента выключить их уже не успевает: после отсоединения в терминале
    хоста оставались включёнными 1000/1002/1003/1006, выделение мышью требовало
    Shift, а экран выглядел повреждённым.
    """
    try:
        os.write(descriptor, TERMINAL_RESET)
    except OSError:
        # Терминала уже нет: сбрасывать нечего.
        pass


def shell_is_ready():
    """Свободна ли оболочка гостя для отправки команды.

    Признак один и точный: отметка возврата к приглашению, которую печатает
    хук PROMPT_COMMAND из /etc/profile.d/agent-docker-serial.sh при каждом
    настоящем возврате Bash к приглашению. Считается только отметка после
    отсечки последней нашей отправки, поэтому работающий на переднем плане
    процесс готовности не даёт.

    По видимому тексту журнала готовность не определяется: строку, похожую на
    приглашение, печатает и работающая программа («progress # », «amount: 5$ »),
    а отправленный в неё tmux attach ушёл бы ей как нажатия клавиш.
    """
    offset = int(checkpoint.read_text()) if checkpoint.exists() else 0
    with serial_log.open('rb') as log:
        end = log.seek(0, os.SEEK_END)
        log.seek(max(offset, end - 65536))
        return ready_marker in log.read()


def send_input(guest, data):
    checkpoint.write_text(str(serial_log.stat().st_size))
    guest.sendall(data)


def terminal_gone(descriptor):
    """Исчезло ли устройство терминала.

    Закрытый мастер pty удаляет узел /dev/pts/N, и это видно изнутри гостя
    без всякой записи в терминал. Живая консоль — хоть работающая, хоть
    простаивающая — узел сохраняет, поэтому проверка её не отключает.
    """
    try:
        name = os.ttyname(descriptor)
    except OSError:
        return True
    return not os.path.exists(name)


def holder():
    """Кто держит блокировку консоли, по записи в самом файле блокировки.

    Запись необязательна: прежняя сборка консоли её не вела, и тогда про
    владельца известно только то, что он есть.
    """
    try:
        content = console_lock.read_text()[:4096]
    except OSError:
        return {}
    try:
        record = json.loads(content)
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


def remember_holder(lock, token):
    """Записывает в файл блокировки, кто её держит.

    Запись нужна дважды: человеку — чтобы отказ назвал процесс, и --release —
    чтобы завершить ровно ту консоль, которую запустил этот хостовый вход.
    """
    try:
        terminal = os.ttyname(sys.stdin.fileno())
    except OSError:
        terminal = ''
    record = {'pid': os.getpid(), 'tty': terminal, 'token': token,
              'operation': operation, 'started': int(time.time())}
    try:
        lock.seek(0)
        lock.truncate()
        lock.write(json.dumps(record, ensure_ascii=False))
        lock.flush()
    except OSError:
        # Не записали — консоль всё равно работает: запись только для отчёта.
        pass


def is_console(pid):
    """Похож ли процесс на консоль гостя: защита от совпадения номеров."""
    try:
        with open('/proc/%d/cmdline' % pid, 'rb') as command:
            return b'console.py' in command.read()
    except OSError:
        return False


def lock_is_free():
    """Свободна ли блокировка консоли сейчас."""
    try:
        descriptor = os.open(str(console_lock), os.O_RDONLY)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return True
    finally:
        os.close(descriptor)


def release(token):
    """Завершает консоль, запущенную хостовым входом с этим же токеном.

    Вызывается с хоста по ловушке, когда его клиент docker exec закончился:
    изнутри гостя уход клиента не виден. Условия строгие — блокировка занята,
    запись владельца есть, токен совпадает, процесс действительно console.py.
    Любое несовпадение означает чужую или новую консоль, и тогда не делается
    ничего. Гость не затрагивается: в serial-сокет здесь не пишется.
    """
    if lock_is_free():
        return 0
    record = holder()
    if not token or record.get('token') != token:
        return 0
    pid = record.get('pid')
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid() or not is_console(pid):
        return 0
    for number in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, number)
        except ProcessLookupError:
            break
        except PermissionError:
            print('Консоль %d принадлежит другому пользователю.' % pid, file=sys.stderr)
            return 1
        deadline = time.monotonic() + release_timeout
        while time.monotonic() < deadline:
            if lock_is_free():
                print('Освобождена консоль %d: её хостовый клиент закрыт.' % pid)
                return 0
            time.sleep(0.05)
    return 0 if lock_is_free() else 1


def guest_command(size):
    """Строка, которую консоль отправляет в оболочку гостя."""
    command = ('stty rows %d cols %d; export DISABLE_UPDATES=1; '
               'mkdir -p "$HOME/projects"; cd "$HOME/projects"'
               % (max(size.lines, 24), max(size.columns, 80)))
    if operation == 'vm-session-open':
        # Подключение к живой сессии: сокет один и общий, имя проверено выше.
        return command + ' && exec tmux -S "%s" attach -t %s; exit' % (
            tmux_socket, shlex.quote(resume))
    if operation == 'vm-session-claude':
        # Новая управляемая сессия рядом с уже работающими: тот же общий сокет,
        # новое имя, свой сервер tmux не поднимается. Прежние сессии при этом
        # не затрагиваются — ни attach к ним, ни kill-session здесь нет. Имя
        # занято — tmux откажет сам, и оболочка вернётся к приглашению.
        # Агент в новой сессии запускается без --resume: RESUME здесь — имя
        # самой сессии, а не прошлый разговор, который нужно продолжить.
        return command + ' && exec tmux -S "%s" new-session -s %s -- claude; exit' % (
            tmux_socket, shlex.quote(resume))
    if project:
        command += ' && cd -- ' + shlex.quote(project)
    agent = shlex.join(commands[operation])
    if operation == 'claude':
        # Агент запускается в управляемой сессии на общем сокете, а не прямо в
        # serial-оболочке. Прямой запуск канал не видел вовсе (сессии tmux нет),
        # и после закрытия окна агент оставался на serial без пути возврата:
        # оболочка занята, свежего приглашения нет. В сессии он виден каналом,
        # переживает закрытие окна и доступен повторным входом. В образе без
        # tmux запуск остаётся прямым, иначе агент не запустился бы вовсе.
        return command + (' && if command -v tmux > /dev/null; then '
                          'exec tmux -S "%s" new-session -s %s -- %s; else exec %s; fi; exit'
                          % (tmux_socket, managed_name(), agent, agent))
    return command + ' && exec ' + agent + '; exit'


def relay(guest, descriptor):
    """Перекладывает байты между терминалом и гостем до Ctrl+] или ухода клиента."""
    stream = Stream(mouse_policy == 'off')
    while True:
        try:
            readable, _, _ = select.select([guest, descriptor], [], [], watch_interval)
        except InterruptedError:
            continue
        if not readable:
            # В консоли тихо: проверяем, что терминал ещё на месте, и
            # досылаем размер окна, если оболочка успела освободиться.
            if terminal_gone(descriptor):
                raise Detached
            apply_size(guest, descriptor)
            continue
        if guest in readable:
            data = guest.recv(65536)
            if not data:
                return
            try:
                os.write(sys.stdout.fileno(), stream.clean(data))
            except OSError:
                # Писать некуда: терминал закрылся, гостя не трогаем.
                raise Detached
        if descriptor in readable:
            try:
                data = os.read(descriptor, 1024)
            except OSError as error:
                if terminal_loss(error):
                    raise Detached
                raise
            if not data:
                # EOF на вводе: клиент закрыл свою сторону.
                raise Detached
            before, escape, _ = data.partition(b'\x1d')
            if before:
                send_input(guest, before)
            if escape:
                return


def detach(number, frame):
    """HUP и TERM означают закрытый терминал, а не отказ."""
    raise Detached


# Размер окна, который ещё не удалось передать гостю: оболочка была занята.
pending_size = {}


def apply_size(guest, descriptor, size=None):
    """Передаёт размеры окна гостю, но только свободной оболочке.

    Serial-линия размеры окна не передаёт, и единственный способ сообщить их
    гостю — команда stty в его оболочке. Пока в ней работает приложение — в
    том числе клиент tmux живой сессии, — не отправляется ничего: команда
    ушла бы ему как нажатия клавиш. Непереданный размер запоминается и уходит
    сам, как только оболочка вернёт приглашение, поэтому изменение окна во
    время работы приложения не теряется.
    """
    if size is None:
        size = pending_size.get('size')
    if size is None:
        return
    if not shell_is_ready():
        pending_size['size'] = size
        return
    pending_size.pop('size', None)
    try:
        send_input(guest, ('stty rows %d cols %d\r'
                           % (max(size[0], 24), max(size[1], 80))).encode())
    except OSError:
        pass


def resize(guest, descriptor):
    """Обработчик SIGWINCH: новые размеры окна консоли."""
    def handler(number, frame):
        try:
            size = os.get_terminal_size(descriptor)
        except OSError:
            return
        apply_size(guest, descriptor, (size.lines, size.columns))

    return handler


def shell_report():
    """Одна строка JSON о состоянии serial-оболочки гостя.

    Отвечает на вопрос, который снаружи иначе не различить: ждёт ли оболочка
    ввод или в ней работает приложение переднего плана. Признак тот же, что у
    самой консоли, — отметка возврата к приглашению после отсечки последней
    отправки, поэтому ответ совпадает с тем, что консоль решит про отправку.

    Режим читающий: блокировка не берётся, serial-сокет не открывается, tty не
    нужен, журнал не дописывается. Поэтому его можно спрашивать при работающем
    агенте, и он не мешает ни консоли, ни сессиям. Состояния три: ready —
    оболочка свободна, busy — в ней что-то работает, unknown — журнала нет или
    его не прочитать (пустым списком и свободной оболочкой это не подменяется).
    """
    try:
        state = 'ready' if shell_is_ready() else 'busy'
    except OSError:
        state = 'unknown'
    print(json.dumps({'api': 1, 'shell': state}, ensure_ascii=False))
    return 0


def main():
    if operation == '--release':
        return release(resume)
    if operation == '--shell':
        return shell_report()
    if operation not in commands and operation not in terminal_operations:
        raise SystemExit('Неизвестная операция консоли: %s' % operation)
    if not sys.stdin.isatty():
        raise SystemExit('Запуск гостевой консоли требует интерактивного терминала.')
    if project and (project.startswith('/') or '..' in project.split('/')):
        raise SystemExit('PROJECT должен быть путём относительно ~/projects.')
    if operation in session_operations and not session_name.match(resume or ''):
        raise SystemExit('Имя сессии не подходит: ожидались буквы, цифры и дефис.')
    with console_lock.open('a+') as lock, socket.socket(socket.AF_UNIX) as guest:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            record = holder()
            pid = record.get('pid')
            message = 'Консоль этого гостя уже открыта.'
            if isinstance(pid, int):
                message = 'Консоль этого гостя уже открыта (процесс %d).' % pid
            raise SystemExit(message)
        remember_holder(lock, os.environ.get('AGENT_DOCKER_CONSOLE_TOKEN', ''))
        if operation != 'vm-console' and not shell_is_ready():
            if operation == 'vm-session-open':
                # Оболочку занял живой клиент tmux — скорее всего, эта же
                # сессия, открытая в консоли гостя. Отправлять в неё attach
                # нельзя: байты ушли бы работающему клиенту как нажатия.
                raise SystemExit('Оболочка гостя занята: сессия уже открыта в его консоли. '
                                 'Вернитесь в неё обычной консолью.')
            if operation == 'vm-session-claude':
                # Та же защита для новой сессии: команда создания ушла бы
                # работающему в оболочке процессу как нажатия клавиш. Сессия
                # не создаётся вовсе, поэтому лишней записи в списке не
                # появляется, а прежняя сессия продолжает работать.
                raise SystemExit('Оболочка гостя занята: в его консоли работает сессия. '
                                 'Отсоединитесь от неё внутри tmux или выйдите из агента, '
                                 'затем повторите; посмотреть консоль можно обычным входом.')
            raise SystemExit('Автозапуск ждёт новый вход в Bash. Подключитесь через make vm-console и выйдите из Claude или Bash.')
        guest.connect(str(workspace / 'serial.sock'))
        descriptor = sys.stdin.fileno()
        signal.signal(signal.SIGHUP, detach)
        signal.signal(signal.SIGTERM, detach)
        signal.signal(signal.SIGWINCH, resize(guest, descriptor))
        try:
            saved = termios.tcgetattr(descriptor)
        except (termios.error, OSError) as error:
            # Терминал исчез, пока консоль открывалась: это не отказ команды,
            # и гостю ничего не отправлено.
            if terminal_loss(error):
                return 0
            raise
        try:
            tty.setraw(descriptor)
            if mouse_policy == 'off':
                # Мышь остаётся терминалу: включения из потока снимаются, а
                # уже включённый режим прежнего приложения сбрасывается сразу.
                restore_terminal(sys.stdout.fileno())
            if operation == 'vm-console' and serial_log.exists():
                # Возврат к работающей сессии: хвост журнала показывает, что
                # происходило в госте, пока консоль была отключена. Журнала
                # может не быть у гостя, который ещё ни разу не загружался.
                # Приватные режимы из хвоста снимаются: иначе вход включал
                # в терминале хоста мышь и альтернативный экран прежнего
                # приложения. Живой поток фильтрации не подлежит — режимами
                # распоряжается само приложение.
                with serial_log.open('rb') as log:
                    end = log.seek(0, os.SEEK_END)
                    log.seek(max(0, end - 4096))
                    os.write(sys.stdout.fileno(),
                             private_mode.sub(b'', safe_tail(log.read())))
            if operation != 'vm-console':
                send_input(guest, (guest_command(os.get_terminal_size(descriptor)) + '\r').encode())
            print('\r\nДля выхода из консоли нажмите Ctrl+].\r', flush=True)
            relay(guest, descriptor)
        except Detached:
            # Клиент ушёл без Ctrl+]: блокировка освободится вместе с нами, а
            # гость продолжает работать — ему ничего не отправлено.
            return 0
        except (termios.error, OSError) as error:
            # Терминал мог исчезнуть и посреди работы, на любом вызове.
            if terminal_loss(error):
                return 0
            raise
        finally:
            restore_terminal(sys.stdout.fileno())
            try:
                termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
            except (termios.error, OSError):
                # Терминала уже нет: восстанавливать режим не на чем.
                pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
