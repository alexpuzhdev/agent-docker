import argparse
import json
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path


def generate_identity():
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] & 0xfc) | 0x02
    return {'uuid': str(uuid.uuid4()), 'mac': octets.hex(':')}


def publish_profile(path, profile):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as draft:
        draft_path = Path(draft.name)
        try:
            json.dump(profile, draft, indent=2)
            draft.write('\n')
            draft.flush()
            os.fsync(draft.fileno())
            os.link(draft_path, path)
        except FileExistsError:
            pass
        finally:
            draft_path.unlink()


def read_identity(path, expected):
    profile = json.loads(path.read_text())
    if not isinstance(profile, dict) or any(profile.get(key) != value for key, value in expected.items()):
        raise ValueError('Профиль VM не соответствует версии, имени, ресурсам или ссылке на прокси.')
    identity = profile['identity']
    if identity is None:
        return None
    if not isinstance(identity, dict):
        raise ValueError('Повреждены идентификаторы в профиле VM.')
    vm_uuid = identity['uuid']
    mac = identity['mac']
    if not isinstance(vm_uuid, str) or uuid.UUID(vm_uuid).version != 4:
        raise ValueError('В профиле VM ожидался UUID версии 4.')
    if not isinstance(mac, str) or not re.fullmatch(r'[0-9a-f][26ae](?::[0-9a-f]{2}){5}', mac):
        raise ValueError('В профиле VM ожидался локально назначенный одноадресный MAC.')
    return vm_uuid, mac


def main():
    parser = argparse.ArgumentParser(description='Создать или прочитать сохранённый JSON-профиль VM.')
    parser.add_argument('path', type=Path)
    parser.add_argument('vm_id')
    parser.add_argument('vcpu', type=int)
    parser.add_argument('ram_mb', type=int)
    parser.add_argument('disk_gb', type=int)
    parser.add_argument('--retain-existing-identity', action='store_true',
                        help='Сохранить назначение идентификаторов платформой для существующего диска.')
    args = parser.parse_args()
    if min(args.vcpu, args.ram_mb, args.disk_gb) <= 0:
        parser.error('Ресурсы VM должны быть положительными.')

    expected = {
        'schema_version': 1,
        'vm_id': args.vm_id,
        'resources': {'vcpu': args.vcpu, 'ram_mb': args.ram_mb, 'disk_gb': args.disk_gb},
        'proxy_ref': args.vm_id,
    }
    try:
        if not args.path.exists():
            identity = None if args.retain_existing_identity else generate_identity()
            publish_profile(args.path, {**expected, 'identity': identity})
        identity = read_identity(args.path, expected)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'Не удалось загрузить профиль VM: {error}\n')
    if identity is not None:
        print(*identity)


if __name__ == '__main__':
    main()
