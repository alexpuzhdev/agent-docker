import errno
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import tempfile
import termios
import time
import unittest


ROOT = Path(__file__).resolve().parents[2]
ENTER_SCREEN = b"\x1b[?1049h"
LEAVE_SCREEN = b"\x1b[?1049l"


@unittest.skipUnless(shutil.which("go") and shutil.which("stty"), "нужны go и stty")
class AtriceScreenTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.root = Path(directory.name)
        cls.binary = cls.root / "atrice"
        subprocess.run(
            ["go", "build", "-buildvcs=false", "-o", str(cls.binary), "./cli"],
            cwd=ROOT, check=True, capture_output=True, timeout=90,
        )
        (cls.root / "v2").mkdir()
        (cls.root / "v2/manage.sh").write_text(
            'case "$1" in\n'
            '  vm-setup) mkdir -p ".v2-state/$VM"; printf "2 4096 12\\n" > ".v2-state/$VM/resources";\n'
            '    touch ".v2-state/$VM/upstream"; printf "FIXTURE_SETUP %s\\n" "$VM";;\n'
            '  vm-proxy|claude-auth) printf "FIXTURE_ROUTE %s %s\\n" "$1" "$VM";;\n'
            '  vm-git-audit) printf "FIXTURE_AUDIT %s\\n" "$VM";;\n'
            '  vm-git-status) printf "FIXTURE_GIT_STATUS\\n";;\n'
            '  vm-git) printf "FIXTURE_TOKEN_PROMPT\\n"; IFS= read -rs token </dev/tty;\n'
            '    test "$token" = fixture_token_hidden || exit 1;\n'
            '    printf "FIXTURE_GIT_SAVED %s %s %s %s\\n" "$REPO" "$GIT_PROVIDER" "$GIT_READ_BRANCHES" "$GIT_WRITE_BRANCH";;\n'
            '  *) printf "ACTION_ON_PRIMARY\\n";;\n'
            'esac\n'
        )
        (cls.root / ".v2-state/vm-09").mkdir(parents=True)
        fake = cls.root / "docker"
        fake.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$1" >> "$ATRICE_TEST_DOCKER_LOG"\n'
            'if test -n "$ATRICE_TEST_WAIT_FILE"; then\n'
            '  while test ! -f "$ATRICE_TEST_WAIT_FILE"; do sleep .05; done\n'
            'fi\n'
        )
        fake.chmod(0o755)

    def read_until(self, descriptor: int, wanted: bytes) -> bytes:
        deadline = time.monotonic() + 6
        output = bytearray()
        while wanted not in output:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([descriptor], [], [], remaining)[0]:
                self.fail(f"Нет последовательности {wanted!r}: {bytes(output)[-1000:]!r}")
            try:
                output.extend(os.read(descriptor, 65536))
            except OSError as error:
                if error.errno == errno.EIO:
                    self.fail(f"Терминал завершился до {wanted!r}: {bytes(output)[-1000:]!r}")
                raise
        return bytes(output)

    def open_menu(self, wait_file: Path | None = None) -> int:
        pid, descriptor = pty.fork()
        if pid == 0:
            env = dict(os.environ, PATH=f"{self.root}:{os.environ['PATH']}", TERM="xterm-256color")
            env.pop("V2_STATE_DIR", None)
            env.update(NO_COLOR="1", ATRICE_TEST_DOCKER_LOG=str(self.root / "docker-calls"))
            if wait_file is not None:
                env["ATRICE_TEST_WAIT_FILE"] = str(wait_file)
            os.execve(self.binary, [str(self.binary), "--root", str(self.root)], env)
        self.addCleanup(self.close_menu, pid, descriptor)
        return descriptor

    @staticmethod
    def close_menu(pid: int, descriptor: int) -> None:
        os.close(descriptor)
        os.waitpid(pid, 0)

    def run_menu(self, quit_key: bytes, open_action: bool) -> None:
        descriptor = self.open_menu()
        first = self.read_until(descriptor, b"vm-09")
        self.assertIn(ENTER_SCREEN, first)
        if open_action:
            os.write(descriptor, b"l")
            action = self.read_until(descriptor, "Нажмите Enter".encode())
            self.assertLess(action.index(LEAVE_SCREEN), action.index(b"ACTION_ON_PRIMARY"))
            os.write(descriptor, b"\r")
            self.assertIn(ENTER_SCREEN, self.read_until(descriptor, b"vm-09"))
        os.write(descriptor, quit_key)
        self.assertIn(LEAVE_SCREEN, self.read_until(descriptor, LEAVE_SCREEN))

    def test_menu_returns_to_primary_screen(self) -> None:
        self.run_menu(b"q", True)

    def test_cursor_movement_does_not_requery_docker(self) -> None:
        second = self.root / ".v2-state/vm-10"
        second.mkdir()
        self.addCleanup(second.rmdir)
        calls = self.root / "docker-calls"
        descriptor = self.open_menu()
        self.read_until(descriptor, "не настроена".encode())
        before = calls.read_text()
        os.write(descriptor, b"\x1b[B")
        self.read_until(descriptor, b"> vm-10")
        os.write(descriptor, b"\x1b[A")
        self.read_until(descriptor, b"> vm-09")
        self.assertEqual(calls.read_text(), before)
        third = self.root / ".v2-state/vm-11"
        third.mkdir()
        self.addCleanup(third.rmdir)
        os.write(descriptor, b"r")
        self.read_until(descriptor, b"vm-11")
        self.assertEqual(calls.read_text(), before + "ps\n")
        os.write(descriptor, b"q")
        self.read_until(descriptor, LEAVE_SCREEN)

    def test_menu_can_exit_while_docker_is_waiting(self) -> None:
        release = self.root / "docker-release"
        descriptor = self.open_menu(release)
        try:
            self.read_until(descriptor, b"> vm-09")
            os.write(descriptor, b"q")
            self.read_until(descriptor, LEAVE_SCREEN)
        finally:
            release.touch()

    def test_end_of_input_restores_screen(self) -> None:
        self.run_menu(b"\x04", False)

    def test_git_menu_audits_published_host_mirror(self) -> None:
        publication = self.root / ".v2-state/vm-09/git-host-public"
        publication.mkdir()
        self.addCleanup(publication.rmdir)
        marker = publication / "current"
        marker.write_text("view-abcdefgh12\n")
        self.addCleanup(marker.unlink)
        descriptor = self.open_menu()
        self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"g")
        status = self.read_until(descriptor, "Проверить, что видно агенту в Git".encode())
        self.assertIn(b"FIXTURE_GIT_STATUS", status)
        self.assertNotIn(LEAVE_SCREEN, status)
        os.write(descriptor, b"\r")
        result = self.read_until(descriptor, "Нажмите Enter".encode())
        self.assertIn(b"FIXTURE_AUDIT vm-09", result)
        os.write(descriptor, b"\r")
        self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"q")
        self.read_until(descriptor, LEAVE_SCREEN)

    def test_git_retry_stays_on_a_clean_alternate_screen(self) -> None:
        descriptor = self.open_menu()
        self.read_until(descriptor, b"vm-09")
        for attempt in range(2):
            os.write(descriptor, b"g")
            status = self.read_until(descriptor, "Репозиторий для новой настройки".encode())
            self.assertNotIn(LEAVE_SCREEN, status)
            self.assertLess(status.rindex(b"\x1b[2J\x1b[H"), status.index(b"FIXTURE_GIT_STATUS"))
            self.assertNotIn(b"invalid value", status)
            if attempt == 0:
                for value, prompt in (
                    (b"owner/project", "Провайдер"),
                    (b"github", "Ветки чтения"),
                    (b"branch-without-alias", "Ветка записи"),
                ):
                    os.write(descriptor, value + b"\r")
                    self.read_until(descriptor, prompt.encode())
                os.write(descriptor, b"\r")
                error = self.read_until(descriptor, "Нажмите Enter".encode())
                self.assertIn(b"invalid value", error)
                self.assertNotIn(LEAVE_SCREEN, error)
            os.write(descriptor, b"\r")
            if attempt == 1:
                self.read_until(descriptor, "Нажмите Enter".encode())
                os.write(descriptor, b"\r")
            menu = self.read_until(descriptor, b"vm-09")
            self.assertNotIn(ENTER_SCREEN, menu)
        os.write(descriptor, b"q")
        self.read_until(descriptor, LEAVE_SCREEN)

    def test_git_configuration_routes_to_host_and_keeps_token_hidden(self) -> None:
        descriptor = self.open_menu()
        self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"g")
        status = self.read_until(descriptor, "Репозиторий для новой настройки".encode())
        self.assertIn(b"FIXTURE_GIT_STATUS", status)
        for value, next_prompt in (
            (b"group/project", "Провайдер"),
            (b"gitlab", "HTTPS-адрес GitLab"),
            (b"https://gitlab.fixture.invalid", "Ветки чтения"),
            (b"reference=release/test", "Ветка записи"),
        ):
            os.write(descriptor, value + b"\r")
            self.read_until(descriptor, next_prompt.encode())
        os.write(descriptor, b"agent/task\r")
        self.read_until(descriptor, b"FIXTURE_TOKEN_PROMPT")
        deadline = time.monotonic() + 2
        while termios.tcgetattr(descriptor)[3] & termios.ECHO:
            if time.monotonic() >= deadline:
                self.fail("Ввод токена остался видимым")
            time.sleep(0.01)
        os.write(descriptor, b"fixture_token_hidden\r")
        result = self.read_until(descriptor, "Нажмите Enter".encode())
        self.assertIn(b"FIXTURE_GIT_SAVED group/project gitlab reference=release/test agent/task", result)
        self.assertNotIn(b"fixture_token_hidden", result)
        os.write(descriptor, b"\r")
        self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"q")
        self.read_until(descriptor, LEAVE_SCREEN)

    def test_vm_menu_repairs_setup_and_opens_claude_auth(self) -> None:
        state = self.root / ".v2-state/vm-09"
        for name in ("resources", "upstream", "alias"):
            self.addCleanup((state / name).unlink, missing_ok=True)
        descriptor = self.open_menu()
        self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"e")
        result = self.read_until(descriptor, "Нажмите Enter".encode())
        self.assertIn(b"FIXTURE_SETUP vm-09", result)
        os.write(descriptor, b"\r")
        self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"m")
        self.read_until(descriptor, "Имя для списка".encode())
        os.write(descriptor, "Проверка Git\r".encode())
        self.read_until(descriptor, "Нажмите Enter".encode())
        os.write(descriptor, b"\r")
        self.read_until(descriptor, "vm-09 (Проверка Git)".encode())
        for key, target in ((b"e", b"vm-proxy"), (b"i", b"claude-auth")):
            os.write(descriptor, key)
            result = self.read_until(descriptor, "Нажмите Enter".encode())
            self.assertIn(b"FIXTURE_ROUTE " + target + b" vm-09", result)
            os.write(descriptor, b"\r")
            self.read_until(descriptor, b"vm-09")
        os.write(descriptor, b"q")
        self.read_until(descriptor, LEAVE_SCREEN)

    @unittest.skipUnless(shutil.which("node"), "нужен node для проверки адреса прокси")
    def test_proxy_setup_accepts_copy_pasted_credentials(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        project = Path(directory.name)
        (project / "v2").mkdir()
        for name in ("manage.sh", "upstream.cjs"):
            shutil.copyfile(ROOT / "v2" / name, project / "v2" / name)
        shutil.copyfile(ROOT / ".env.example", project / ".env.example")
        fake = project / "docker"
        fake.write_text(
            '#!/bin/bash\n'
            'if [[ $* == *check-upstream.cjs* ]]; then\n'
            '  for arg in "$@"; do\n'
            '    if [[ $arg == type=bind,src=*,dst=/upstream,readonly ]]; then\n'
            '      file=${arg#type=bind,src=}\n'
            '      file=${file%,dst=/upstream,readonly}\n'
            '      node -e \'require(process.argv[1]).readUpstream(process.argv[2])\' '
            '"$FIXTURE_PROJECT/v2/upstream.cjs" "$file" || exit 1\n'
            '      test "${REJECT_PROXY:-}" != 1; exit $?\n'
            '    fi\n'
            '  done\n'
            '  exit 1\n'
            'fi\n'
        )
        fake.chmod(0o755)
        state = project / "state"
        supplied = b"fixture-user:fixture-password@proxy.fixture.invalid:3128"

        for reject in ("1", "0"):
            pid, descriptor = pty.fork()
            if pid == 0:
                env = dict(os.environ, PATH=f"{project}:{os.environ['PATH']}", VM="vm-23",
                           V2_STATE_DIR=str(state), FIXTURE_PROJECT=str(project), REJECT_PROXY=reject)
                os.execvpe("bash", ["bash", str(project / "v2/manage.sh"), "vm-proxy"], env)
            try:
                self.read_until(descriptor, "HTTP-прокси".encode())
                deadline = time.monotonic() + 2
                while termios.tcgetattr(descriptor)[3] & termios.ECHO:
                    if time.monotonic() >= deadline:
                        self.fail("Ввод прокси остался видимым")
                    time.sleep(0.01)
                os.write(descriptor, supplied + b"\r")
                _, status = os.waitpid(pid, 0)
                self.assertEqual(os.waitstatus_to_exitcode(status), int(reject))
            finally:
                os.close(descriptor)
            guest = state / "vm-23"
            if reject == "1":
                self.assertFalse(guest.exists())
            else:
                self.assertEqual((guest / "upstream").read_bytes(), b"http://" + supplied + b"\n")
