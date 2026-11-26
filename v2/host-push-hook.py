import json
import os
import re
import resource
import shutil
import socket
import subprocess
import sys
import tempfile


class PushHook:
    MAX_BYTES = 256 * 1024 * 1024

    @classmethod
    def limit_pack(cls) -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (cls.MAX_BYTES, cls.MAX_BYTES))

    @classmethod
    def run(cls) -> None:
        updates = [line.split() for line in sys.stdin.read(1024).splitlines() if line.strip()]
        if len(updates) != 1 or len(updates[0]) != 3 or updates[0][2] != 'refs/heads/work':
            raise ValueError('Запись разрешена только в ветку work.')
        old, new, _ = updates[0]
        if not all(re.fullmatch(r'[a-f0-9]{40}', oid) and int(oid, 16) for oid in (old, new)):
            raise ValueError('Создание и удаление веток не разрешено.')
        with tempfile.TemporaryFile(dir=os.environ['TMPDIR']) as pack:
            result = subprocess.run(
                ['git', 'pack-objects', '--stdout', '--revs'],
                input=f'{new}\n^{old}\n'.encode(), stdout=pack, stderr=subprocess.DEVNULL, timeout=90,
                preexec_fn=cls.limit_pack,
            )
            size = pack.tell()
            if result.returncode or not 0 < size <= cls.MAX_BYTES:
                raise ValueError('Публикация содержит слишком большой или некорректный пакет Git.')
            pack.seek(0)
            request = {'generation': os.environ['AT_PUBLISHED_VIEW'], 'old': old, 'new': new, 'size': size}
            with socket.socket(socket.AF_UNIX) as client:
                client.settimeout(550)
                client.connect(os.environ['AT_PUBLISH_SOCKET'])
                with client.makefile('wb') as stream:
                    stream.write(json.dumps(request).encode() + b'\n')
                    shutil.copyfileobj(pack, stream, 65536)
                client.shutdown(socket.SHUT_WR)
                with client.makefile('rb') as acknowledgement:
                    reply = acknowledgement.readline(8)
            if reply != b'ok\n':
                raise ValueError('Хост не подтвердил публикацию. Проверьте состояние ветки и повторите push.')


if __name__ == '__main__':
    try:
        PushHook.run()
    except ValueError as error:
        print(f'agent-docker: {error}', file=sys.stderr)
        sys.exit(1)
    except (OSError, subprocess.SubprocessError, KeyError):
        print('agent-docker: Публикация на хосте недоступна.', file=sys.stderr)
        sys.exit(1)
