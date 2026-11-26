"""Чтение списка живых сессий гостя через отдельный канал virtio-serial.

Канал существует только ради этого списка. Он не сеть: со стороны контейнера
это unix-сокет /guest/control.sock, со стороны гостя — символьное устройство
/dev/virtio-ports/atrice.control. Ни один байт отсюда не попадает в
пользовательскую serial-консоль: путь к serial.sock в этом модуле не
упоминается, и список можно обновлять, пока в консоли работает агент.

Контракт канала (версия api = 1):
  запрос  — одна строка JSON, перевод строки в конце;
  ответ   — одна строка JSON, перевод строки в конце;
  запросы — только {"op": "hello"} и {"op": "sessions"}, произвольных
            команд в канале нет;
  hello   — {"ok": true, "api": 1, "tmux": "3.5a" | null};
  sessions— {"ok": true, "api": 1, "sessions": [
              {"name": str, "created": int, "attached": bool, "command": str}]};
  отказ   — {"ok": false, "error": "no-tmux" | "unknown-op" | ...}.

Коды возврата различают причины, потому что подсказки для них разные:
  0 — список получен;
  2 — ошибка вызова;
  3 — канала нет: сокета не существует или подключиться не удалось;
  4 — ответ превысил предел размера;
  5 — ответ не прошёл проверку структуры;
  6 — ответа нет совсем: служба не запущена или не смогла открыть порт;
  7 — ответ начался, но не поместился в отведённый срок;
  8 — служба работает, но не имеет доступа к серверу tmux;
  9 — в образе гостя нет tmux.

Отдельно от пустого списка стоит разбор формата: если tmux что-то напечатал,
но ни одна строка не совпала с форматом, служба отвечает format-unparsed, и
это код 5. Пустой список означает именно отсутствие сессий.

Операция diagnose отвечает на другой вопрос: не «какие сессии», а «что именно
сейчас не так». Она показывает состояния по отдельности — старый образ без
канала, нет службы, нет tmux, служба не видит сокет, пустой список, живой
список, занятая консоль — и сама не падает ни на одном из них, потому что
показать их должен список, а не сообщение об ошибке. Код возврата diagnose —
0, если диагноз поставлен; состояние лежит в поле state.

Обычная serial-консоль ни от чего этого не зависит: она работает на любом
образе, и diagnose её не трогает. Занятость консоли определяется по тому же
файлу блокировки, что берёт console.py; ни её сокет, ни её журнал, ни tty
здесь не открываются и не записываются.

Срок единый: он отсчитывается монотонными часами от подключения до конца
ответа. Тайм-аут на каждом recv отдельно давал бы складываться задержкам —
ответ из фрагментов по 80 мс проходил бы при сроке 150 мс.
"""

import fcntl
import json
import os
import re
import socket
import sys
import time

CONTROL_SOCKET = os.environ.get('AGENT_DOCKER_CONTROL_SOCK', '/guest/control.sock')
# Пределы намеренно малы: список сессий одного гостя короткий, а всё, что
# больше, означает неисправный или враждебный ответ, и читать его незачем.
REPLY_LIMIT = int(os.environ.get('AGENT_DOCKER_CONTROL_LIMIT', 65536))
DEADLINE = float(os.environ.get('AGENT_DOCKER_CONTROL_DEADLINE', 3))
SESSION_LIMIT = 256
FIELD_LIMIT = 256
API = 1
CONTROL = re.compile(r'[\x00-\x1f\x7f]')
# Файл блокировки пользовательской консоли — тот же, что берёт console.py.
# Это не консоль: ни её сокет, ни её журнал здесь не упоминаются.
CONSOLE_LOCK = os.environ.get('AGENT_DOCKER_CONSOLE_LOCK', '/guest/console.lock')
# Состояния подключения, которые диагностика показывает по отдельности.
# Подсказки для них разные, и ни одно не подменяет другое: «список пуст» — это
# не «служба не видит сокет», а «старый образ» — не «служба не отвечает».
HINTS = {
    'old-image': 'Образ гостя без канала сессий: живые сессии он показывать не умеет. '
                 'Обычная консоль по c работает — она есть на любом образе.',
    'no-service': 'Канал есть, но гостевая служба не ответила: она не запущена или не '
                  'смогла открыть порт. Причина видна в journalctl -u atrice-sessions.',
    'no-tmux': 'В образе гостя нет tmux: показывать живые сессии нечем. '
               'Обычная консоль по c при этом работает.',
    'no-permission': 'Служба не видит сокет tmux: она работает под другим UID или '
                     'каталог сокета ей закрыт. Живые сессии при этом могут быть.',
    'format-unparsed': 'Вывод tmux не разобран: ни одна строка не совпала с форматом. '
                       'Это не отсутствие сессий — живые сессии могут быть.',
    'slow': 'Ответ канала начался, но не поместился в отведённый срок.',
    'broken': 'Ответ канала не прошёл проверку: содержимому гостя доверять нельзя.',
    'empty': 'Канал работает, живых сессий нет.',
    'live': 'Канал работает, список сессий получен.',
}
# Коды отказов транспорта в состояния. Разбор формата и отказы службы идут не
# по кодам, а по полю error: код 5 общий для них и для испорченного ответа.
CODE_STATES = {3: 'no-service', 4: 'broken', 5: 'broken', 6: 'no-service',
               7: 'slow', 8: 'no-permission', 9: 'no-tmux'}
ERROR_STATES = {'no-tmux': 'no-tmux', 'no-permission': 'no-permission',
                'format-unparsed': 'format-unparsed'}


class Failure(Exception):
    """Отказ с кодом возврата.

    Причина нужна дважды: человеку — строкой в stderr и кодом возврата,
    диагностике — состоянием в отчёте. Поэтому отказ сначала поднимается
    исключением, а печатает его и завершает процесс только main.
    """

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def fail(code, message):
    raise Failure(code, message)


def expired(received):
    """Срок истёк: пустой ответ и начатый ответ — разные неисправности."""
    if received:
        fail(7, 'Ответ канала сессий не поместился в отведённый срок.')
    fail(6, 'Служба сессий не ответила: она не запущена или не смогла открыть порт.')


def ask(operation):
    """Отправляет один запрос и возвращает разобранный ответ.

    Весь обмен укладывается в один монотонный срок: иначе медленные
    фрагменты складывались бы и ответ принимался спустя кратно больший срок.
    """
    if not os.path.exists(CONTROL_SOCKET):
        fail(3, 'Канал сессий не настроен: гость запущен без него. '
                'Обновите образ и перезапустите гостя.')
    deadline_at = time.monotonic() + DEADLINE
    chunks = bytearray()
    with socket.socket(socket.AF_UNIX) as channel:
        try:
            channel.settimeout(max(deadline_at - time.monotonic(), 0.001))
            channel.connect(CONTROL_SOCKET)
            channel.sendall((json.dumps({'op': operation}) + '\n').encode())
            while b'\n' not in chunks:
                left = deadline_at - time.monotonic()
                if left <= 0:
                    expired(chunks)
                channel.settimeout(left)
                piece = channel.recv(4096)
                if not piece:
                    break
                chunks += piece
                if len(chunks) > REPLY_LIMIT:
                    fail(4, 'Ответ канала сессий превысил предел размера.')
        except socket.timeout:
            expired(chunks)
        except OSError as error:
            fail(3, 'Канал сессий недоступен: %s' % error)
    line, terminator, _ = bytes(chunks).partition(b'\n')
    if not line.strip():
        if not terminator:
            fail(6, 'Служба сессий не ответила: она не запущена или не смогла открыть порт.')
        fail(5, 'Служба сессий ответила пустой строкой.')
    if not terminator:
        fail(5, 'Ответ канала сессий оборвался без перевода строки.')
    try:
        return json.loads(line.decode('utf-8', 'strict'))
    except (UnicodeDecodeError, json.JSONDecodeError):
        fail(5, 'Ответ канала сессий не является строкой JSON.')


def text(value):
    """Приводит строковое поле к безопасному для терминала виду."""
    if not isinstance(value, str) or len(value) > FIELD_LIMIT:
        return None
    return CONTROL.sub(' ', value)


def validate(reply):
    """Проверяет структуру ответа: доверять содержимому гостя нельзя."""
    if not isinstance(reply, dict) or reply.get('api') != API:
        fail(5, 'Ответ канала сессий не соответствует версии контракта %d.' % API)
    if reply.get('ok') is not True:
        error = text(reply.get('error')) or 'неизвестная причина'
        if error == 'no-tmux':
            fail(9, 'В образе гостя нет tmux: живые сессии показать нечем.')
        if error == 'no-permission':
            fail(8, 'Служба сессий не видит сервер tmux: она работает под другим UID.')
        if error == 'format-unparsed':
            fail(5, 'Вывод tmux не разобран: ни одна строка не совпала с форматом. '
                    'Это не отсутствие сессий — живые сессии могут быть.')
        fail(5, 'Канал сессий ответил отказом: %s' % error)
    sessions = reply.get('sessions')
    if not isinstance(sessions, list) or len(sessions) > SESSION_LIMIT:
        fail(5, 'Ответ канала сессий не содержит пригодного списка.')
    checked = []
    for entry in sessions:
        if not isinstance(entry, dict):
            fail(5, 'Запись сессии не является объектом.')
        name = text(entry.get('name'))
        command = text(entry.get('command'))
        created = entry.get('created')
        attached = entry.get('attached')
        if not name or command is None:
            fail(5, 'У записи сессии нет имени или команды.')
        if not isinstance(created, int) or isinstance(created, bool) or created < 0:
            fail(5, 'Время создания сессии не является целым числом.')
        if not isinstance(attached, bool):
            fail(5, 'Признак подключения сессии не является логическим.')
        checked.append({'name': name, 'created': created,
                        'attached': attached, 'command': command})
    return checked


def console_state():
    """Занята ли пользовательская serial-консоль гостя.

    Берётся тот же файл блокировки, что и у console.py, и только он: ни сокет
    консоли, ни её журнал, ни tty здесь не открываются и не записываются.
    Файл не создаётся — его отсутствие означает, что консоль не открывали ни
    разу. Блокировка берётся неблокирующе и отпускается сразу; окно, в котором
    одновременный запуск консоли увидел бы её занятой, — доли миллисекунды, и
    консоль в этом случае просто скажет повторить.
    """
    try:
        descriptor = os.open(CONSOLE_LOCK, os.O_RDONLY)
    except FileNotFoundError:
        return 'free'
    except OSError:
        return 'unknown'
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return 'busy'
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return 'free'
    finally:
        os.close(descriptor)


def report(state, collected, message=''):
    """Завершает отчёт диагностики: состояние, подсказка и подробность.

    Подсказка берётся по состоянию, а не из сообщения отказа: сообщение
    объясняет сбой разово, а подсказка говорит, что делать, и у каждого
    состояния она своя. Сообщение сохраняется отдельным полем detail, чтобы
    точная причина не терялась.
    """
    collected['state'] = state
    collected['hint'] = HINTS.get(state, 'Состояние канала неизвестно.')
    if message:
        collected['detail'] = message
    return collected


def diagnose():
    """Одна проверка подключения: каждое состояние показывается отдельно.

    Диагностика не падает ни на одном из них — её дело сказать, что именно не
    так, а не отказаться отвечать. Отдельно от канала стоит консоль: она
    работает на любом образе, и её занятость — тоже часть отчёта.
    """
    collected = {'api': API, 'tmux': None, 'socket': None, 'sessions': [],
                 'console': console_state()}
    # Канала нет совсем: гость запущен с образа, который его не умеет.
    if not os.path.exists(CONTROL_SOCKET):
        return report('old-image', collected)
    try:
        greeting = ask('hello')
    except Failure as failure:
        return report(CODE_STATES.get(failure.code, 'broken'), collected,
                      failure.message)
    if not isinstance(greeting, dict) or greeting.get('api') != API:
        return report('broken', collected)
    # Поле socket необязательное: у службы прежней сборки его нет.
    collected['socket'] = text(greeting.get('socket'))
    if greeting.get('ok') is not True:
        return report(ERROR_STATES.get(text(greeting.get('error')), 'broken'),
                      collected)
    collected['tmux'] = text(greeting.get('tmux'))
    if collected['tmux'] is None:
        return report('no-tmux', collected)
    try:
        reply = ask('sessions')
        if isinstance(reply, dict) and reply.get('ok') is not True:
            return report(ERROR_STATES.get(text(reply.get('error')), 'broken'),
                          collected)
        collected['sessions'] = validate(reply)
    except Failure as failure:
        return report(CODE_STATES.get(failure.code, 'broken'), collected,
                      failure.message)
    skipped = reply.get('skipped')
    if isinstance(skipped, int) and not isinstance(skipped, bool) and skipped > 0:
        collected['skipped'] = skipped
    return report('live' if collected['sessions'] else 'empty', collected)


def main():
    operation = sys.argv[1] if len(sys.argv) > 1 else 'sessions'
    if operation not in ('sessions', 'hello', 'diagnose'):
        fail(2, 'Канал принимает только sessions, hello и diagnose.')
    if operation == 'diagnose':
        # Диагноз поставлен — это успех, каким бы ни было состояние: показать
        # его должен список на хосте, а не код возврата.
        print(json.dumps(diagnose(), ensure_ascii=False))
        return
    reply = ask(operation)
    if operation == 'hello':
        if not isinstance(reply, dict):
            fail(5, 'Ответ канала сессий должен быть объектом JSON.')
        validate({'api': reply.get('api'), 'ok': reply.get('ok'),
                  'error': reply.get('error'), 'sessions': []})
        tmux = text(reply.get('tmux')) if reply.get('tmux') is not None else None
        print(json.dumps({'api': API, 'tmux': tmux}, ensure_ascii=False))
        return
    listed = validate(reply)
    result = {'api': API, 'sessions': listed}
    # Необязательное поле: сколько строк tmux разобрать не удалось. Его может
    # не быть у службы прежней сборки, и тогда его просто нет в выводе.
    skipped = reply.get('skipped')
    if isinstance(skipped, int) and not isinstance(skipped, bool) and skipped > 0:
        result['skipped'] = skipped
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Failure as failure:
        print(failure.message, file=sys.stderr)
        raise SystemExit(failure.code)
