import os
import pty
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

READY_MARKER = b'\x1b]9999;agent-docker-shell-ready\x07'


class ConsoleTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.workspace = Path(temp.name)
        source = Path(__file__).resolve().parents[1] / 'console.py'
        self.console = self.workspace / 'console.py'
        self.console.write_text(source.read_text().replace("Path('/guest')", f'Path({str(self.workspace)!r})', 1))
        self.log = self.workspace / 'console.log'
        self.log.write_bytes(READY_MARKER)

    def start_console(self, operation, resume=''):
        terminal, stdin = pty.openpty()
        self.addCleanup(os.close, terminal)
        try:
            process = subprocess.Popen(
                [sys.executable, str(self.console), operation, '', resume],
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        finally:
            os.close(stdin)
        self.addCleanup(process.__exit__, None, None, None)
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        return process, terminal

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
                self.assertTrue(command.endswith(b" && exec claude --resume 'name with spaces'; exit\r"), command)
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
