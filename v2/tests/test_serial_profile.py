"""Отметка возврата к приглашению в гостевом профиле.

Профиль проверяется настоящим интерактивным bash на отдельном pty. Условия
области действия (интерактивная оболочка, пользователь agent, терминал
/dev/ttyS0) выполняются подставными id и tty в PATH, поэтому сам профиль в
проверке не переписывается.
"""
import os
import pty
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

READY_MARKER = '\x1b]9999;agent-docker-shell-ready\x07'
PROFILE = Path(__file__).resolve().parents[1] / 'serial-profile.sh'


class SerialProfileTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.binaries = Path(temp.name) / 'bin'
        self.binaries.mkdir()
        self.stub('id', 'agent')
        self.stub('tty', '/dev/ttyS0')

    def stub(self, name, answer):
        """Подставная команда условия области действия профиля."""
        path = self.binaries / name
        path.write_text('#!/bin/sh\nprintf \'%s\\n\' %s\n' % ('%s', answer))
        path.chmod(0o755)

    def shell(self, script, user='agent', terminal='/dev/ttyS0'):
        """Прогоняет сценарий в интерактивном bash на pty и отдаёт его вывод."""
        self.stub('id', user)
        self.stub('tty', terminal)
        terminal_descriptor, child = pty.openpty()
        process = subprocess.Popen(
            ['bash', '--norc', '--noprofile', '-i'],
            stdin=subprocess.PIPE, stdout=child, stderr=child, text=True,
            env=dict(os.environ, PATH='%s:%s' % (self.binaries, os.environ['PATH']),
                     PROFILE=str(PROFILE)))
        os.close(child)
        process.stdin.write(script + '\nexit 0\n')
        process.stdin.close()
        self.assertEqual(process.wait(timeout=20), 0)
        collected = bytearray()
        try:
            while True:
                chunk = os.read(terminal_descriptor, 4096)
                if not chunk:
                    break
                collected += chunk
        except OSError:
            pass
        os.close(terminal_descriptor)
        return collected.decode(errors='replace')

    def test_marker_comes_back_on_every_prompt(self):
        """Отметка печатается при каждом возврате к приглашению."""
        output = self.shell('. "$PROFILE"\ntrue\nfalse\ntrue')
        # Приглашений после входа в профиль не меньше, чем команд.
        self.assertGreaterEqual(output.count(READY_MARKER), 4, repr(output))

    def test_user_prompt_command_and_status_survive(self):
        """Пользовательский PROMPT_COMMAND и статус команды сохраняются."""
        output = self.shell(
            'PROMPT_COMMAND=\'printf "user=%s;" $?\'\n'
            'PS1="[ps1=\\$?]"\n'
            '. "$PROFILE"\n'
            'false\n'
            '(exit 7)')
        self.assertIn('user=1;', output)
        self.assertIn('[ps1=1]', output)
        self.assertIn('user=7;', output)
        self.assertIn('[ps1=7]', output)
        self.assertIn(READY_MARKER, output)

    def test_hook_is_not_duplicated(self):
        """Повторный вход в профиль не добавляет второй хук."""
        output = self.shell(
            '. "$PROFILE"\n. "$PROFILE"\n. "$PROFILE"\n'
            'printf "hooks=%s\\n" "${PROMPT_COMMAND}"')
        hooks = [line for line in output.splitlines() if line.startswith('hooks=')]
        self.assertEqual(hooks, ['hooks=agent_docker_shell_ready'], repr(output))

    def test_marker_is_invisible_and_alone(self):
        """В видимом выводе от отметки ничего не появляется."""
        output = self.shell('. "$PROFILE"\nprintf \'слово\\n\'')
        self.assertIn('слово', output)
        visible = output.replace(READY_MARKER, '')
        self.assertNotIn('9999', visible)
        self.assertNotIn('agent-docker-shell-ready', visible)

    def test_scope_stays_on_the_agent_serial_console(self):
        """Вне консоли agent на /dev/ttyS0 хук не ставится."""
        for user, terminal in (('root', '/dev/ttyS0'), ('agent', '/dev/pts/3')):
            output = self.shell('. "$PROFILE"\nprintf "hooks=%s\\n" "${PROMPT_COMMAND-нет}"',
                                user=user, terminal=terminal)
            self.assertIn('hooks=нет', output)
            self.assertNotIn(READY_MARKER, output)

    def test_non_interactive_shell_keeps_profile_quiet(self):
        """Неинтерактивная оболочка профиль не трогает."""
        run = subprocess.run(
            ['bash', '-c', '. "$1"; printf "hooks=%s\\n" "${PROMPT_COMMAND-нет}"', '-', str(PROFILE)],
            capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH='%s:%s' % (self.binaries, os.environ['PATH'])))
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(run.stdout.strip(), 'hooks=нет')


if __name__ == '__main__':
    unittest.main()
