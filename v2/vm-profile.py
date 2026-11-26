import argparse
import fcntl
import json
import os
import re
import secrets
import tempfile
import uuid
from pathlib import Path


SERIALS = ('system', 'board', 'chassis', 'disk')
SEEDS = ('virtio', 'cdrom')


def generate_identity():
    octets = bytearray(secrets.token_bytes(6))
    octets[0] = (octets[0] & 0xfc) | 0x02
    serials = {name: secrets.token_hex(10).upper() for name in SERIALS}
    return {'uuid': str(uuid.uuid4()), 'mac': octets.hex(':'), 'serials': serials}


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


def replace_profile(path, profile):
    descriptor, draft = tempfile.mkstemp(dir=path.parent, prefix='profile-')
    draft_path = Path(draft)
    try:
        with os.fdopen(descriptor, 'w') as handle:
            json.dump(profile, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(draft_path, path)
    finally:
        draft_path.unlink(missing_ok=True)


def read_profile(path, expected):
    profile = json.loads(path.read_text())
    if not isinstance(profile, dict) or any(profile.get(key) != value for key, value in expected.items()):
        raise ValueError('Профиль VM не соответствует версии, имени, ресурсам или ссылке на прокси.')
    return profile


def read_seed(path, profile, requested):
    # Способ подключения данных cloud-init закрепляется один раз и хранится
    # рядом с остальным виртуальным оборудованием: менять его у готового
    # гостя нельзя. Профиль без этого поля остался от предыдущей версии,
    # поэтому запрошенное значение дописывается на место. Запись идёт под
    # блокировкой и после повторного чтения: при одновременном запуске
    # сохранённым должно оказаться одно значение, иначе запущенная VM
    # получит не то устройство, которое записано в профиле.
    seed = profile.get('seed')
    if seed is None:
        with open(path) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            stored = json.loads(path.read_text())
            seed = stored.get('seed')
            if seed is None:
                replace_profile(path, {**stored, 'seed': requested})
                seed = requested
    if seed not in SEEDS:
        raise ValueError('В профиле VM ожидался известный способ подключения данных cloud-init.')
    return seed


def read_identity(profile):
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
    if 'serials' not in identity:
        return vm_uuid, mac
    serials = identity['serials']
    if (not isinstance(serials, dict) or set(serials) != set(SERIALS) or
            not all(isinstance(serials[name], str) and re.fullmatch(r'[0-9A-F]{20}', serials[name])
                    for name in SERIALS)):
        raise ValueError('В профиле VM ожидались серийные номера из 20 шестнадцатеричных символов.')
    return (vm_uuid, mac, *(serials[name] for name in SERIALS))


def main():
    parser = argparse.ArgumentParser(description='Создать или прочитать сохранённый JSON-профиль VM.')
    parser.add_argument('path', type=Path)
    parser.add_argument('vm_id')
    parser.add_argument('vcpu', type=int)
    parser.add_argument('ram_mb', type=int)
    parser.add_argument('disk_gb', type=int)
    parser.add_argument('--retain-existing-identity', action='store_true',
                        help='Сохранить назначение идентификаторов платформой для существующего диска.')
    parser.add_argument('--seed', choices=SEEDS, required=True,
                        help='Способ подключения данных cloud-init для нового профиля.')
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
            publish_profile(args.path, {**expected, 'identity': identity, 'seed': args.seed})
        profile = read_profile(args.path, expected)
        identity = read_identity(profile)
        seed = read_seed(args.path, profile, args.seed)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f'Не удалось загрузить профиль VM: {error}\n')
    print(seed, *(identity or ()))


if __name__ == '__main__':
    main()
