import json
import socket

with socket.socket(socket.AF_UNIX) as guest:
    guest.settimeout(8)
    guest.connect('/guest/qmp.sock')
    with guest.makefile('rwb') as channel:
        if 'QMP' not in json.loads(channel.readline()):
            raise SystemExit('Некорректный ответ QMP.')
        for name in ('qmp_capabilities', 'system_powerdown'):
            channel.write(json.dumps({'execute': name}).encode() + b'\n')
            channel.flush()
            while True:
                result = json.loads(channel.readline())
                if 'error' in result:
                    raise SystemExit('QEMU отклонил команду ' + name)
                if 'return' in result:
                    break
