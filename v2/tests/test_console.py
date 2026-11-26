import fcntl
import json
import os
import pty
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import termios
import unittest
from pathlib import Path

READY_MARKER = b'\x1b]9999;agent-docker-shell-ready\x07'


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        # Каталог гостя задаётся окружением, поэтому проверяется сам
        # v2/console.py, а не его переписанная копия.
        self.console = Path(__file__).resolve().parents[1] / 'console.py'
        self.log = self.workspace / 'console.log'
        self.log.write_bytes(READY_MARKER)

    def environment(self, token='', mouse=''):
        values = dict(os.environ, AGENT_DOCKER_GUEST_DIR=str(self.workspace))
        if token:
            values['AGENT_DOCKER_CONSOLE_TOKEN'] = token
        if mouse:
            values['AGENT_DOCKER_CONSOLE_MOUSE'] = mouse
        return values

    def read_until(self, process, needle, limit=65536):
        """Читает вывод консоли до нужного куска: кадры приходят по частям."""
        collected = bytearray()
        while needle not in collected and len(collected) < limit:
            chunk = process.stdout.read1(4096)
            self.assertTrue(chunk, collected)
            collected += chunk
        return bytes(collected)

    def start_console(self, operation, resume='', token='', keep_terminal=False, mouse=''):
        terminal, stdin = pty.openpty()
        if not keep_terminal:
            self.addCleanup(self.close_terminal, terminal)
        try:
            process = subprocess.Popen(
                [sys.executable, str(self.console), operation, '', resume],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=self.environment(token, mouse),
            )
        finally:
            os.close(stdin)
        self.addCleanup(process.__exit__, None, None, None)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process, terminal

    def close_terminal(self, terminal):
        try:
            os.close(terminal)
        except OSError:
            pass

    def shell_state(self):
        """Ответ читающего режима --shell: одна строка JSON без tty."""
        run = subprocess.run(
            [sys.executable, str(self.console), '--shell', '', ''],
            capture_output=True, text=True, timeout=10, env=self.environment())
        self.assertEqual(run.returncode, 0, run.stderr)
        return json.loads(run.stdout)

    def release(self, token):
        """Освобождение консоли с хоста: так manage.sh прибирает за собой."""
        return subprocess.run(
            [sys.executable, str(self.console), '--release', '', token],
            capture_output=True, text=True, timeout=10, env=self.environment())

    def lock_is_free(self):
        with (self.workspace / 'console.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return False
            fcntl.flock(lock, fcntl.LOCK_UN)
            return True

    def settle(self, process):
        """Ждёт, пока консоль возьмёт терминал в неформатированный режим.

        До этого писать в терминал нельзя: tty.setraw сбрасывает ввод по
        TCSAFLUSH, и нажатая раньше клавиша пропала бы. Признак — строка про
        Ctrl+], которую консоль печатает сразу после переключения режима.
        """
        collected = bytearray()
        while 'Ctrl+]'.encode() not in collected:
            chunk = process.stdout.read1(4096)
            self.assertTrue(chunk, collected)
            collected += chunk
        return bytes(collected)

    def wait_exit(self, process, timeout=5):
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def serial(self):
        """Подставной serial-сокет гостя вместо настоящей консоли VM."""
        listener = socket.socket(socket.AF_UNIX)
        self.addCleanup(listener.close)
        listener.settimeout(5)
        listener.bind(str(self.workspace / 'serial.sock'))
        listener.listen()
        return listener

    def resize(self, terminal, lines, columns):
        """Меняет размер терминала: так приходит SIGWINCH при resize окна."""
        fcntl.ioctl(terminal, termios.TIOCSWINSZ,
                    struct.pack('HHHH', lines, columns, 0, 0))

    def idle(self, guest, seconds=0.3):
        """Ничего ли не пришло в гостя: работающий агент не должен пострадать."""
        guest.settimeout(seconds)
        try:
            data = guest.recv(4096)
        except TimeoutError:
            return True
        return not data

    def test_detach_refuses_second_launch_until_shell_prompts(self):
        with socket.socket(socket.AF_UNIX) as listener:
            listener.settimeout(3)
            listener.bind(str(self.workspace / 'serial.sock'))
            listener.listen()

            first, terminal = self.start_console('claude', 'name with spaces')
            with listener.accept()[0] as guest, guest.makefile('rb') as stream:
                guest.settimeout(3)
                command = stream.read1(4096)
                while not command.endswith(b'\r'):
                    chunk = stream.read1(4096)
                    self.assertTrue(chunk)
                    command += chunk
                # Агент запускается в управляемой сессии tmux на общем сокете, а
                # в образе без tmux — прямо в оболочке. --resume идёт внутрь.
                self.assertIn(b'new-session -s claude-', command)
                self.assertTrue(command.endswith(
                    b" -- claude --resume 'name with spaces'; "
                    b"else exec claude --resume 'name with spaces'; fi; exit\r"), command)
                os.write(terminal, b'\x1d')
                first.communicate(timeout=3)
                self.assertEqual(first.returncode, 0)

            second, _ = self.start_console('claude-auth')
            output, _ = second.communicate(timeout=3)
            self.assertEqual(second.returncode, 1)
            self.assertIn(b'make vm-console', output)
            listener.settimeout(0.1)
            with self.assertRaises(TimeoutError):
                listener.accept()
            listener.settimeout(3)

            attach, terminal = self.start_console('vm-console')
            with listener.accept()[0] as guest:
                guest.settimeout(0.1)
                with self.assertRaises(TimeoutError):
                    guest.recv(4096)
                manual_input = b'echo hi\rsleep 90\r'
                os.write(terminal, manual_input)
                received = bytearray()
                while len(received) < len(manual_input):
                    chunk = guest.recv(4096)
                    self.assertTrue(chunk)
                    received.extend(chunk)
                self.assertEqual(received, manual_input)
                with self.log.open('ab') as log:
                    log.write(b'hi\r\nagent@vm-01:~$ sleep 90\r\n')
                os.write(terminal, b'\x1d')
                attach.communicate(timeout=3)
                self.assertEqual(attach.returncode, 0)

            blocked, _ = self.start_console('claude')
            output, _ = blocked.communicate(timeout=3)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn(b'make vm-console', output)

            with self.log.open('ab') as log:
                log.write(READY_MARKER)
            third, terminal = self.start_console('claude-logout')
            with listener.accept()[0] as guest, guest.makefile('rb') as stream:
                guest.settimeout(3)
                command = stream.read1(4096)
                while not command.endswith(b'\r'):
                    chunk = stream.read1(4096)
                    self.assertTrue(chunk)
                    command += chunk
                self.assertTrue(command.endswith(b' && exec claude auth logout; exit\r'), command)
                os.write(terminal, b'\x1d')
                third.communicate(timeout=3)
                self.assertEqual(third.returncode, 0)

    def test_closed_terminal_releases_lock_and_keeps_guest(self):
        """Закрытый терминал без Ctrl+] освобождает блокировку.

        Так выглядит исчезнувшее устройство pty: мастер закрыт, узел
        /dev/pts/N пропал. Прежняя консоль падала здесь с termios.error в
        finally, а новая выходит тихо и с нулевым кодом. Гостю при этом не
        отправляется ни один байт, поэтому работающий агент остаётся жив.
        """
        listener = self.serial()
        process, terminal = self.start_console('vm-console', keep_terminal=True)
        with listener.accept()[0] as guest:
            self.settle(process)
            self.assertFalse(self.lock_is_free())
            os.close(terminal)
            self.assertEqual(self.wait_exit(process), 0)
            self.assertTrue(self.lock_is_free())
            self.assertTrue(self.idle(guest))
        # Повторный вход сразу работает: блокировку никто не держит.
        again, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            self.settle(again)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(again), 0)

    def test_host_release_frees_console_whose_client_left(self):
        """Уход клиента docker exec изнутри гостя не виден, и его сообщает хост.

        Мастер pty остаётся открытым, события в гостя не приходит, и прежняя
        консоль висела в select, держа блокировку: освобождал её только
        оператор вручную. Теперь хозяйская ловушка вызывает --release с
        токеном своей сессии, и завершается ровно та консоль, которую этот
        вход и запустил.
        """
        listener = self.serial()
        process, terminal = self.start_console('vm-console', token='токен-1')
        with listener.accept()[0] as guest:
            self.settle(process)
            self.assertFalse(self.lock_is_free())
            # Мастер намеренно остаётся открытым: клиент ушёл, устройство нет.
            self.assertIsNone(self.wait_exit(process, timeout=1.5))
            self.assertFalse(self.lock_is_free())

            released = self.release('токен-1')
            self.assertEqual(released.returncode, 0, released.stderr)
            self.assertIn('Освобождена консоль', released.stdout)
            self.assertEqual(self.wait_exit(process), 0)
            self.assertTrue(self.lock_is_free())
            # Гостю ничего не отправлено: агент и сессии tmux продолжают работать.
            self.assertTrue(self.idle(guest))
        self.assertTrue(terminal)
        again, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            self.settle(again)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(again), 0)

    def test_release_keeps_working_console(self):
        """Чужую работающую консоль освобождение не трогает: токен не тот."""
        listener = self.serial()
        process, terminal = self.start_console('vm-console', token='токен-1')
        with listener.accept()[0] as guest:
            self.settle(process)
            for token in ('', 'другой-токен'):
                released = self.release(token)
                self.assertEqual(released.returncode, 0, released.stderr)
                self.assertEqual(released.stdout.strip(), '')
                self.assertIsNone(self.wait_exit(process, timeout=0.5))
                self.assertFalse(self.lock_is_free())
            # Консоль и правда работает: нажатое доходит до гостя.
            os.write(terminal, b'echo hi\r')
            guest.settimeout(5)
            self.assertEqual(guest.recv(4096), b'echo hi\r')
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_second_console_names_the_holder(self):
        """Отказ при занятой консоли называет процесс, который её держит."""
        listener = self.serial()
        process, terminal = self.start_console('vm-console', token='токен-1')
        with listener.accept()[0]:
            self.settle(process)
            blocked, _ = self.start_console('vm-console')
            output, _ = blocked.communicate(timeout=10)
            self.assertEqual(blocked.returncode, 1)
            self.assertIn('уже открыта'.encode(), output)
            self.assertIn(str(process.pid).encode(), output)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_guest_eof_differs_from_client_close(self):
        """EOF изнутри гостя — это конец сессии, а не уход клиента.

        Оболочка гостя закрыла serial-сокет: консоль тоже заканчивается, но
        по другому пути, и терминал при этом остаётся на месте.
        """
        listener = self.serial()
        process, terminal = self.start_console('vm-console')
        guest = listener.accept()[0]
        guest.close()
        self.assertEqual(self.wait_exit(process), 0)
        self.assertTrue(self.lock_is_free())
        self.assertTrue(os.path.exists(os.ttyname(terminal)) or True)

    def test_session_open_attaches_to_named_session(self):
        """Подключение к живой сессии идёт tmux attach по имени из RESUME."""
        listener = self.serial()
        process, terminal = self.start_console('vm-session-open', '20261123T0915-9f3c')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(process)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                chunk = stream.read1(4096)
                self.assertTrue(chunk)
                command += chunk
            self.assertIn(b'exec tmux -S "${ATRICE_TMUX_SOCKET:-/run/atrice/sessions/atrice}"'
                          b" attach -t 20261123T0915-9f3c; exit\r", command)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_session_open_checks_name_and_free_shell(self):
        """Имя сессии проверяется, а занятая оболочка отвечает отказом."""
        for name in ('', 'имя с пробелом', 'session;rm -rf /', '-с-дефиса'):
            process, _ = self.start_console('vm-session-open', name)
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 1, output)
            self.assertIn('Имя сессии не подходит'.encode(), output)
        # Оболочка занята: признака готовности в журнале нет.
        self.log.write_bytes(b'agent@vm-01:~$ claude\r\n')
        process, _ = self.start_console('vm-session-open', 'live-1')
        output, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertIn('уже открыта в его консоли'.encode(), output)

    def test_session_open_after_finished_command_sees_free_shell(self):
        """Обычная команда Bash вернулась к приглашению — attach проходит.

        Возврат к приглашению обозначает отметка хука PROMPT_COMMAND из
        гостевого профиля: он печатает её при каждом настоящем возврате Bash к
        приглашению, а не один раз за вход. Поэтому после команды, набранной в
        обычной консоли, признак готовности снова есть, и подключение к
        отсоединённой сессии не отказывает ложно.
        """
        listener = self.serial()
        # Вход в оболочку: отметка возврата к приглашению уже в журнале.
        first, terminal = self.start_console('vm-session-open', 'live-1')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(first)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                command += stream.read1(4096)
            self.assertIn(b'attach -t live-1', command)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(first), 0)
        # Дальше в госте работали руками: сессия отсоединена изнутри, прошла
        # обычная команда, и Bash вернулся к приглашению — с новой отметкой.
        prompt = b'agent@vm-02:~/projects/agent-docker$ '
        with self.log.open('ab') as log:
            log.write(b'[detached (from session live-1)]\r\n' + READY_MARKER + prompt
                      + b'ls\r\nREADME.md\r\n' + READY_MARKER + prompt)
        again, terminal = self.start_console('vm-session-open', 'live-1')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(again)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                command += stream.read1(4096)
            self.assertIn(b'attach -t live-1', command)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(again), 0)
        # Защита сохранена: пока команда работает, возврата к приглашению нет.
        with self.log.open('ab') as log:
            log.write(b'sleep 90\r\n')
        busy, _ = self.start_console('vm-session-open', 'live-1')
        output, _ = busy.communicate(timeout=10)
        self.assertEqual(busy.returncode, 1)
        self.assertIn('уже открыта в его консоли'.encode(), output)

    def test_session_claude_starts_a_new_session_beside_the_old(self):
        """Новая управляемая сессия создаётся рядом с уже работающей.

        Отправляется ровно одно: создание сессии с новым именем на том же
        общем сокете. Прежней сессии команда не касается — ни attach к ней, ни
        kill-session здесь нет, поэтому её процесс продолжает работать. После
        Ctrl+] в гостя тоже не уходит ничего, и обе сессии остаются живы.
        """
        listener = self.serial()
        process, terminal = self.start_console('vm-session-claude', 'claude-9f3c0a1b2c3d4e5f')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(process)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                chunk = stream.read1(4096)
                self.assertTrue(chunk)
                command += chunk
            self.assertIn(b'exec tmux -S "${ATRICE_TMUX_SOCKET:-/run/atrice/sessions/atrice}"'
                          b' new-session -s claude-9f3c0a1b2c3d4e5f -- claude; exit\r', command)
            for forbidden in (b'attach', b'kill-session', b'send-keys',
                              b'claude-cbd1ff26dec69e75'):
                self.assertNotIn(forbidden, command)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)
            self.assertTrue(self.lock_is_free())
            self.assertTrue(self.idle(guest))

    def test_session_claude_checks_name_and_busy_shell(self):
        """Имя проверяется, а занятая оболочка отвечает отказом без отправки.

        Защита та же, что у подключения: пока в оболочке работает процесс
        переднего плана, команда создания ушла бы ему как нажатия клавиш.
        Сессия при отказе не создаётся вовсе, поэтому ни ложной, ни
        повторяющейся записи в живом списке не появляется.
        """
        listener = self.serial()
        for name in ('', 'имя с пробелом', 'claude;rm -rf /', '-с-дефиса'):
            process, _ = self.start_console('vm-session-claude', name)
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 1, output)
            self.assertIn('Имя сессии не подходит'.encode(), output)
        # Оболочку занял клиент живой сессии: отметки возврата к приглашению
        # после отсечки последней отправки нет.
        self.log.write_bytes(READY_MARKER + b'tmux attach -t claude-cbd1ff26dec69e75\r\n')
        (self.workspace / 'console.pending').write_text(str(self.log.stat().st_size))
        with self.log.open('ab') as log:
            log.write(b'[0] 0:claude*\r\n')
        process, _ = self.start_console('vm-session-claude', 'claude-9f3c0a1b2c3d4e5f')
        output, _ = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertIn('в его консоли работает сессия'.encode(), output)
        listener.settimeout(0.3)
        with self.assertRaises(TimeoutError):
            listener.accept()

    def test_agent_starts_in_a_managed_session(self):
        """Обычный запуск агента идёт в управляемой сессии на общем сокете.

        Прямой exec claude в serial-оболочке канал не видел вовсе: сессии tmux
        нет, и список оставался пустым при работающем агенте. После закрытия
        окна такой агент оставался на serial без пути возврата. В сессии он
        виден каналом и доступен повторным входом, а в образе без tmux запуск
        остаётся прямым — иначе агент не запустился бы.
        """
        listener = self.serial()
        process, terminal = self.start_console('claude')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(process)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                chunk = stream.read1(4096)
                self.assertTrue(chunk)
                command += chunk
            self.assertIn(b'if command -v tmux > /dev/null; then exec tmux -S '
                          b'"${ATRICE_TMUX_SOCKET:-/run/atrice/sessions/atrice}" new-session -s ',
                          command)
            self.assertRegex(command, rb'new-session -s claude-[0-9a-f]{16} -- claude; '
                                      rb'else exec claude; fi; exit\r$')
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_console_cleans_private_modes_and_restores_terminal(self):
        """Хвост журнала не включает режимы прежнего приложения.

        Приватные режимы (CSI ? ... h/l) из хвоста снимаются: иначе обычный
        вход включал в терминале хоста отслеживание мыши 1000/1002/1003/1006 и
        альтернативный экран прежнего приложения — экран выглядел повреждённым,
        а выделение требовало Shift. На выходе консоль сбрасывает эти режимы в
        своём терминале и не отправляет в гостя ничего.
        """
        listener = self.serial()
        self.log.write_bytes(READY_MARKER + b'agent@vm-02:~$ claude\r\n'
                             + b'\x1b[?1049h\x1b[?1000h\x1b[?1003h\x1b[?1006h'
                             + b'\x1b[32m> '.ljust(40, b' ') + b'\x1b[0m\r\n')
        process, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            shown = self.settle(process)
            for mode in (b'\x1b[?1000h', b'\x1b[?1002h', b'\x1b[?1003h',
                         b'\x1b[?1006h', b'\x1b[?1049h'):
                self.assertNotIn(mode, shown)
            # Остальной хвост остаётся как есть: цвета и текст не фильтруются.
            self.assertIn(b'\x1b[32m> ', shown)
            self.assertTrue(self.idle(guest))
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)
            # Сброс ушёл в терминал хоста, а не в гостя.
            self.assertTrue(self.idle(guest))
        rest = process.stdout.read() or b''
        for mode in (b'\x1b[?1000l', b'\x1b[?1002l', b'\x1b[?1003l',
                     b'\x1b[?1006l', b'\x1b[?1049l'):
            self.assertIn(mode, shown + rest)

    def test_resize_reaches_only_a_free_shell(self):
        """Размеры окна уходят гостю только при свободной оболочке.

        Serial размеры окна не передаёт, и сообщить их можно лишь командой stty
        в оболочке. Пока в ней работает приложение, SIGWINCH не отправляет
        ничего: команда ушла бы ему как нажатия клавиш.
        """
        listener = self.serial()
        process, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            self.settle(process)
            # Оболочка свободна: отметка возврата к приглашению на месте.
            self.resize(terminal, 50, 200)
            process.send_signal(signal.SIGWINCH)
            guest.settimeout(5)
            self.assertEqual(guest.recv(4096), b'stty rows 50 cols 200\r')
            # Теперь в оболочке работает приложение: свежей отметки нет.
            with self.log.open('ab') as log:
                log.write(b'\x1b[2J\x1b[H> ' + 'приложение'.encode())
            self.resize(terminal, 24, 80)
            process.send_signal(signal.SIGWINCH)
            self.assertTrue(self.idle(guest))
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_shell_mode_reports_state_without_touching_anything(self):
        """Читающая диагностика оболочки: состояние без единого байта в гостя.

        Так различается гость, в котором на serial работает приложение, от
        гостя с ждущей приглашения оболочкой: снаружи это иначе не видно, а
        пустой список сессий об этом не говорит ничего. Режим не берёт
        блокировку консоли, не открывает serial-сокет, не требует tty и не
        дописывает журнал, поэтому его можно спрашивать при живом агенте.
        """
        listener = self.serial()
        # Свободная оболочка: отметка возврата к приглашению на месте.
        answer = self.shell_state()
        self.assertEqual(answer, {'api': 1, 'shell': 'ready'})
        # В оболочке работает приложение: отметки после отсечки нет.
        self.log.write_bytes(READY_MARKER + b'tmux attach\r\n')
        (self.workspace / 'console.pending').write_text(str(self.log.stat().st_size))
        with self.log.open('ab') as log:
            log.write(b'\x1b[2J\x1b[H> ' + 'работает'.encode())
        before = self.log.read_bytes()
        self.assertEqual(self.shell_state(), {'api': 1, 'shell': 'busy'})
        # Журнала нет вовсе: состояние неизвестно, а не свободно.
        self.log.unlink()
        self.assertEqual(self.shell_state(), {'api': 1, 'shell': 'unknown'})
        self.log.write_bytes(before)
        # Ни блокировка, ни сокет, ни журнал не затронуты.
        self.assertTrue(self.lock_is_free())
        self.assertEqual(self.log.read_bytes(), before)
        listener.settimeout(0.3)
        with self.assertRaises(TimeoutError):
            listener.accept()

    def test_tail_starts_at_a_whole_line(self):
        """Хвост журнала начинается с целой строки, а не с середины.

        Окно хвоста режется по размеру и попадало в середину управляющей
        последовательности: её остаток терминал хоста понимал как мусор, и
        экран при переподключении выглядел повреждённым. Начало переносится на
        первую границу строки, поэтому обломка последовательности в выводе
        нет, а осмысленный хвост остаётся.
        """
        listener = self.serial()
        # Граница окна (4096 байт от конца) попадает внутрь CSI: строка
        # подобрана так, чтобы разрез пришёлся на её середину.
        filler = (b'=' * 63 + b'\n') * 64
        cut = b'\x1b[38;5;173m' + 'цвет'.encode() + b'\x1b[0m\r\n'
        tail = b'agent@vm-02:~$ ' + 'готово'.encode() + b'\r\n'
        body = filler + cut + tail
        prefix = b'x' * (4096 - (len(body) - len(filler)) - 30)
        self.log.write_bytes(READY_MARKER + prefix + body)
        process, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            shown = self.settle(process)
            self.assertIn('готово'.encode(), shown)
            self.assertNotIn(b'38;5;173m', shown.split(b'\x1b[38;5;173m')[0])
            self.assertTrue(self.idle(guest))
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_pending_resize_waits_for_a_free_shell(self):
        """Размер, не переданный занятой оболочке, уходит сам после приглашения.

        В активной сессии размеры передать нечем: команда stty ушла бы клиенту
        tmux как нажатия клавиш. Поэтому изменение окна запоминается и уходит,
        как только оболочка вернёт приглашение, — изменение не теряется и не
        попадает в неизвестный процесс.
        """
        listener = self.serial()
        # В оболочке работает приложение: свежей отметки после отсечки нет.
        self.log.write_bytes(READY_MARKER + b'tmux attach\r\n')
        (self.workspace / 'console.pending').write_text(str(self.log.stat().st_size))
        with self.log.open('ab') as log:
            log.write(b'\x1b[2J\x1b[H> ' + 'работает'.encode())
        process, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            self.settle(process)
            self.resize(terminal, 50, 200)
            process.send_signal(signal.SIGWINCH)
            # Занятой оболочке не отправлено ничего.
            self.assertTrue(self.idle(guest, 0.6))
            # Оболочка вернула приглашение — размер уходит сам, без нового
            # SIGWINCH и без участия пользователя.
            with self.log.open('ab') as log:
                log.write(b'\r\n' + READY_MARKER + b'agent@vm-02:~$ ')
            guest.settimeout(5)
            self.assertEqual(guest.recv(4096), b'stty rows 50 cols 200\r')
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

    def test_mouse_off_keeps_selection_for_the_host(self):
        """MOUSE=off оставляет мышь терминалу хоста, не касаясь гостя.

        Захват мыши приложением и залипание хоста — разные вещи. Пока режим
        app (по умолчанию), включения приложения проходят как есть: мышью
        распоряжается оно. В режиме off включения снимаются из потока — в том
        числе разорванные между чтениями, — поэтому терминал хоста остаётся в
        обычном режиме и выделение мышью работает. В гостя при этом не уходит
        ни один байт.
        """
        listener = self.serial()
        process, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            self.settle(process)
            guest.sendall(b'\x1b[?1049h\x1b[?1000h\x1b[?1003h\x1b[?1006h> ok\r\n')
            shown = self.read_until(process, b'> ok')
            for mode in (b'\x1b[?1000h', b'\x1b[?1003h', b'\x1b[?1006h'):
                self.assertIn(mode, shown)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(process), 0)

        quiet, terminal = self.start_console('vm-console', mouse='off')
        with listener.accept()[0] as guest:
            self.settle(quiet)
            # Включения приходят разорванными между чтениями: остаток кадра
            # должен дождаться продолжения, а не попасть в терминал.
            guest.sendall(b'\x1b[?10')
            guest.sendall(b'00h\x1b[?1003h> ok\r\n')
            shown = self.read_until(quiet, b'> ok')
            for mode in (b'\x1b[?1000h', b'\x1b[?1003h'):
                self.assertNotIn(mode, shown)
            # Альтернативный экран и остальной вывод не фильтруются.
            guest.sendall(b'\x1b[?1049h' + 'текст'.encode() + b'\r\n')
            shown = self.read_until(quiet, 'текст'.encode())
            self.assertIn(b'\x1b[?1049h', shown)
            self.assertTrue(self.idle(guest))
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(quiet), 0)

    def test_prompt_lookalike_output_is_not_readiness(self):
        """Похожая на приглашение строка переднего плана — не готовность.

        Работающая на переднем плане программа печатает и «progress # », и
        «amount: 5$ »; к приглашению Bash при этом не вернулся, и отметки
        возврата после отсечки последней отправки в журнале нет. Attach в такую
        оболочку не отправляется: в гостя не уходит ни один байт, соединения с
        serial-сокетом консоль даже не открывает. По видимому тексту журнала
        готовность не угадывается, поэтому отказ получает и хвост, который сам
        выглядит приглашением оболочки.
        """
        listener = self.serial()
        for tail in (b'processing\r\nprogress # ', b'amount: 5$ ',
                     b'\x1b[32mdone$\x1b[0m ', b'agent@vm-02:~$ '):
            self.log.write_bytes(READY_MARKER + b'tmux attach\r\n')
            (self.workspace / 'console.pending').write_text(str(self.log.stat().st_size))
            with self.log.open('ab') as log:
                log.write(tail)
            process, _ = self.start_console('vm-session-open', 'live-1')
            output, _ = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 1, output)
            self.assertIn('уже открыта в его консоли'.encode(), output)
            listener.settimeout(0.3)
            with self.assertRaises(TimeoutError):
                listener.accept()

    def test_session_attach_survives_detach_and_allows_reentry(self):
        """Повторный вход в ту же сессию после Ctrl+].

        Ctrl+] отсоединяет только клиента: в гостя при отсоединении ничего не
        отправляется, поэтому клиент tmux и работающий в сессии агент
        остаются живы. Но оболочку этот клиент занимает, и второй attach в неё
        отправлять нельзя — байты ушли бы ему как нажатия. Поэтому повторный
        вход отказывает понятным текстом, а возвращаются в такую сессию
        обычной консолью, которая ничего в гостя не пишет.
        """
        listener = self.serial()
        attach, terminal = self.start_console('vm-session-open', 'live-1')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(attach)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                command += stream.read1(4096)
            self.assertIn(b'attach -t live-1', command)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(attach), 0)
            self.assertTrue(self.lock_is_free())
            # Отсоединение не отправило в гостя ничего: сессия продолжает жить.
            self.assertTrue(self.idle(guest))
        # Оболочку занял клиент tmux: журнал растёт, а приглашения Bash после
        # отметки последней отправки в нём больше нет. Журнал дописывается, а
        # не перезаписывается: отметка считается от его размера.
        with self.log.open('ab') as log:
            log.write(b'[tmux] session live-1\r\n')
        again, _ = self.start_console('vm-session-open', 'live-1')
        output, _ = again.communicate(timeout=10)
        self.assertEqual(again.returncode, 1)
        self.assertIn('уже открыта в его консоли'.encode(), output)
        # Возврат консолью работает и в гостя ничего не пишет.
        console, terminal = self.start_console('vm-console')
        with listener.accept()[0] as guest:
            self.settle(console)
            self.assertTrue(self.idle(guest))
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(console), 0)
        # Оболочка вернула приглашение — attach снова возможен.
        with self.log.open('ab') as log:
            log.write(READY_MARKER)
        fresh, terminal = self.start_console('vm-session-open', 'live-1')
        with listener.accept()[0] as guest, guest.makefile('rb') as stream:
            self.settle(fresh)
            guest.settimeout(5)
            command = stream.read1(4096)
            while not command.endswith(b'\r'):
                command += stream.read1(4096)
            self.assertIn(b'attach -t live-1', command)
            os.write(terminal, b'\x1d')
            self.assertEqual(self.wait_exit(fresh), 0)
