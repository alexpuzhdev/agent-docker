import argparse
import json
import os
import re
import resource
import signal
import socketserver
import subprocess
import tempfile
import time
from pathlib import Path


class PushRequest(socketserver.StreamRequestHandler):
    MAX_BYTES = 256 * 1024 * 1024

    def handle(self) -> None:
        try:
            self.wfile.write(b'ok\n' if self.publish() else b'failed\n')
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            try:
                self.wfile.write(b'failed\n')
            except OSError:
                return

    @staticmethod
    def limit_job() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (1024 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (400, 400))

    def read_request(self, size: int, deadline: float) -> bytes:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError('Истёк срок чтения запроса.')
        self.request.settimeout(seconds)
        return self.rfile.read1(size)

    def publish(self) -> bool:
        deadline = time.monotonic() + 30
        line = bytearray()
        for _ in range(1024):
            char = self.read_request(1, deadline)
            if char == b'\n':
                break
            if not char:
                return False
            line += char
        else:
            return False
        header = json.loads(line)
        if not isinstance(header, dict) or set(header) != {'generation', 'old', 'new', 'size'}:
            return False
        generation, old, new, size = (header[key] for key in ('generation', 'old', 'new', 'size'))
        if not isinstance(generation, str) or not re.fullmatch(r'view-[a-zA-Z0-9]{10}', generation):
            return False
        if not all(isinstance(oid, str) and re.fullmatch(r'[a-f0-9]{40}', oid) for oid in (old, new)):
            return False
        if type(size) is not int or not 0 < size <= self.MAX_BYTES:
            return False
        with tempfile.NamedTemporaryFile(dir=self.server.scratch) as pack:
            remaining = size
            while remaining:
                data = self.read_request(min(remaining, 65536), deadline)
                if not data:
                    return False
                pack.write(data)
                remaining -= len(data)
            if self.read_request(1, deadline):
                return False
            pack.flush()
            with subprocess.Popen(
                [*self.server.command, generation, old, new, pack.name],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True,
                preexec_fn=self.limit_job,
            ) as process:
                try:
                    return process.wait(timeout=520) == 0
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    raise


class PushServer(socketserver.UnixStreamServer):
    request_queue_size = 2

    def __init__(self, address: str, scratch: Path, command: list[str]) -> None:
        self.scratch = scratch
        self.command = command
        super().__init__(address, PushRequest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('socket', type=Path)
    parser.add_argument('scratch', type=Path)
    parser.add_argument('manage', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    os.chdir(args.socket.parent)
    args.socket.unlink(missing_ok=True)
    try:
        with PushServer(args.socket.name, args.scratch, ['bash', str(args.manage), 'vm-git-host-receive']) as worker:
            print('Публикация в work включена. Для остановки нажмите Ctrl+C.', flush=True)
            worker.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        args.socket.unlink(missing_ok=True)
