import fcntl
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GENERATOR = Path(__file__).resolve().parents[1] / 'vm-profile.py'
GUEST = GENERATOR.parent / 'guest.sh'


class VmProfileTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)

    def profile(self, name, *options, cpu='2'):
        # Способ подключения данных cloud-init обязателен, поэтому проверки,
        # которым он безразличен, запрашивают virtio.
        if '--seed' not in options:
            options = (*options, '--seed', 'virtio')
        return self.generate(str(self.workspace / f'{name}.json'), name, cpu, '1024', '12', *options)

    def generate(self, *arguments, timeout=5):
        return subprocess.run(
            [sys.executable, str(GENERATOR), *arguments], capture_output=True, text=True, timeout=timeout,
        )

    def saved(self, path):
        profile = json.loads(path.read_text())
        return profile.pop('seed', None), profile

    def test_new_guest_gets_distinct_persistent_uuid_and_mac(self):
        first = self.profile('vm-01')
        second = self.profile('vm-02')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        seed, vm_uuid, mac, *serials = first.stdout.split()
        self.assertEqual(seed, 'virtio')
        self.assertEqual(uuid.UUID(vm_uuid).version, 4)
        self.assertEqual(int(mac[:2], 16) & 3, 2)
        self.assertEqual(len(serials), 4)
        for serial in serials:
            self.assertRegex(serial, r'^[0-9A-F]{20}$')
        self.assertEqual(len(set(serials)), 4)
        other = second.stdout.split()[1:]
        for own, foreign in zip(first.stdout.split()[1:], other):
            self.assertNotEqual(own, foreign)
        path = self.workspace / 'vm-01.json'
        saved = path.read_bytes()
        profile = json.loads(saved)
        self.assertEqual(profile['identity'], {
            'uuid': vm_uuid, 'mac': mac,
            'serials': dict(zip(('system', 'board', 'chassis', 'disk'), serials)),
        })
        self.assertEqual(profile['proxy_ref'], 'vm-01')
        self.assertEqual(profile['seed'], 'virtio')
        restarted = self.profile('vm-01', '--retain-existing-identity')
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout, first.stdout)
        self.assertEqual(path.read_bytes(), saved)

    def test_existing_disk_keeps_platform_defaults(self):
        first = self.profile('vm-01', '--retain-existing-identity', '--seed', 'cdrom')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, 'cdrom\n')
        path = self.workspace / 'vm-01.json'
        self.assertIsNone(json.loads(path.read_bytes())['identity'])
        # Сохранённый способ подключения важнее запрошенного: иначе
        # устройство у готового гостя сменилось бы на следующем запуске.
        repeated = self.profile('vm-01')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, 'cdrom\n')
        self.assertEqual(json.loads(path.read_bytes())['seed'], 'cdrom')

    def test_mismatched_resources_keep_saved_profile(self):
        created = self.profile('vm-01')
        self.assertEqual(created.returncode, 0, created.stderr)
        path = self.workspace / 'vm-01.json'
        saved = path.read_bytes()
        mismatched = self.profile('vm-01', cpu='4')
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertIn('не соответствует', mismatched.stderr)
        self.assertEqual(path.read_bytes(), saved)

    def saved_profile(self, name, identity, seed=None):
        path = self.workspace / f'{name}.json'
        profile = {
            'schema_version': 1, 'vm_id': name,
            'resources': {'vcpu': 2, 'ram_mb': 1024, 'disk_gb': 12},
            'proxy_ref': name, 'identity': identity,
        }
        if seed is not None:
            profile['seed'] = seed
        path.write_text(json.dumps(profile, indent=2) + '\n')
        return path, path.read_bytes()

    def test_profile_without_serials_keeps_existing_hardware(self):
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        path, saved = self.saved_profile('vm-01', identity, seed='cdrom')
        for options in ((), ('--retain-existing-identity',)):
            result = self.profile('vm-01', *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split(), ['cdrom', identity['uuid'], identity['mac']])
        self.assertEqual(path.read_bytes(), saved)

    def test_profile_without_seed_records_requested_transport(self):
        # Профиль прошлой версии поля seed не содержит. Запрошенный способ
        # дописывается на место, остальное остаётся как было.
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        path, saved = self.saved_profile('vm-01', identity)
        first = self.profile('vm-01', '--seed', 'cdrom')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.split(), ['cdrom', identity['uuid'], identity['mac']])
        self.assertEqual(self.saved(path), ('cdrom', json.loads(saved)))
        repeated = self.profile('vm-01', '--seed', 'virtio')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, first.stdout)
        self.assertEqual(json.loads(path.read_text())['seed'], 'cdrom')

    def test_unknown_seed_keeps_saved_profile(self):
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        for seed in ('sata', '', 'VIRTIO', True, ['virtio']):
            with self.subTest(seed=seed):
                path, saved = self.saved_profile('vm-01', identity, seed=seed)
                result = self.profile('vm-01')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('способ подключения', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(path.read_bytes(), saved)

    def test_invalid_serials_keep_saved_profile(self):
        base = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        good = {'system': 'A' * 20, 'board': 'B' * 20, 'chassis': 'C' * 20, 'disk': 'D' * 20}
        for serials in ({**good, 'disk': 'd' * 20}, {**good, 'disk': 'D,ID=X' + 'D' * 14},
                        {**good, 'disk': 'D' * 21}, {key: good[key] for key in ('system', 'board', 'chassis')},
                        {**good, 'extra': 'E' * 20}, ['A' * 20]):
            with self.subTest(serials=serials):
                path, saved = self.saved_profile('vm-01', {**base, 'serials': serials})
                result = self.profile('vm-01')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('серийные номера', result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertEqual(path.read_bytes(), saved)

    def test_seed_is_required(self):
        # Запрошенный способ закрепляется в профиле навсегда, поэтому
        # значение по умолчанию недопустимо: его легко применить не к тому
        # гостю, и у созданного ранее гостя устройство исчезло бы.
        path = self.workspace / 'vm-01.json'
        result = self.generate(str(path), 'vm-01', '2', '1024', '12')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('--seed', result.stderr)
        self.assertFalse(path.exists())

    def test_migration_waits_for_profile_lock(self):
        # Чужая блокировка должна останавливать дополнение профиля, иначе
        # одновременные запуски запишут разные значения.
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        path, saved = self.saved_profile('vm-01', identity)
        with open(path) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(subprocess.TimeoutExpired):
                self.generate(str(path), 'vm-01', '2', '1024', '12', '--seed', 'cdrom', timeout=2)
        self.assertEqual(path.read_bytes(), saved)
        result = self.profile('vm-01', '--seed', 'cdrom')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(path.read_text())['seed'], 'cdrom')

    def test_migration_rereads_profile_under_lock(self):
        # Пока дополнение профиля ждёт блокировку, значение может появиться
        # из другого запуска. Записывать своё поверх него нельзя: VM уже
        # запущена с тем устройством, которое записано в профиле.
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        path, _ = self.saved_profile('vm-01', identity)
        with open(path) as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            child = subprocess.Popen(
                [sys.executable, str(GENERATOR), str(path), 'vm-01', '2', '1024', '12', '--seed', 'virtio'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(child.kill)
            time.sleep(0.5)
            self.saved_profile('vm-01', identity, seed='cdrom')
            fcntl.flock(handle, fcntl.LOCK_UN)
        stdout, stderr = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertEqual(stdout.split(), ['cdrom', identity['uuid'], identity['mac']])
        self.assertEqual(json.loads(path.read_text())['seed'], 'cdrom')

    def test_parallel_migration_records_one_transport(self):
        # Профиль прошлой версии дополняется под блокировкой: при
        # одновременном запуске сохранённое значение должно совпасть с тем,
        # что получили оба вызова.
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        path, _ = self.saved_profile('vm-01', identity)
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(self.profile, 'vm-01', '--seed', seed) for seed in ('cdrom', 'virtio')]
        first, second = [job.result() for job in jobs]
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(json.loads(path.read_text())['seed'], first.stdout.split()[0])
        self.assertEqual(len(list(self.workspace.iterdir())), 1)

    def test_parallel_creation_uses_one_saved_identity(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(self.profile, 'vm-01') for _ in range(2)]
        first, second = [job.result() for job in jobs]
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(len(list(self.workspace.iterdir())), 1)


class GuestQemuArgumentsTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        for name in ('opt', 'images', 'bin'):
            (self.root / name).mkdir()
        shutil.copy(GENERATOR, self.root / 'opt' / 'vm-profile.py')
        (self.root / 'opt' / 'seed.py').write_text('')
        (self.root / 'images' / '.ready').write_text('')
        stubs = {
            'qemu-img': 'touch "${@: -2:1}"\n',
            'genisoimage': 'exit 0\n',
            'qemu-system-x86_64': 'printf \'%s\\n\' "$@" > "$QEMU_ARGS"\n',
        }
        for name, body in stubs.items():
            stub = self.root / 'bin' / name
            stub.write_text('#!/bin/bash\n' + body)
            stub.chmod(0o755)

    def guest(self, name):
        guest = self.root / name
        guest.mkdir(exist_ok=True)
        return guest

    def start(self, name):
        guest = self.guest(name)
        script = GUEST.read_text().replace('/opt/agent-docker/', f'{self.root}/opt/')
        script = script.replace('/images/', f'{self.root}/images/').replace('/guest/', f'{guest}/')
        (guest / 'guest.sh').write_text(script)
        arguments = guest / 'qemu.args'
        result = subprocess.run(
            ['bash', str(guest / 'guest.sh')], capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PATH': f'{self.root}/bin:' + os.environ['PATH'], 'QEMU_ARGS': str(arguments),
                 'VM_NAME': name, 'VCPU': '2', 'RAM_MB': '1024', 'DISK_GB': '12'},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return arguments.read_text().splitlines()

    def option(self, arguments, name):
        return [arguments[index + 1] for index, value in enumerate(arguments) if value == name]

    def test_new_guest_gets_persistent_smbios_and_disk_serials(self):
        first = self.start('vm-01')
        identity = json.loads((self.guest('vm-01') / 'profile.json').read_text())['identity']
        serials = identity['serials']
        self.assertEqual(self.option(first, '-uuid'), [identity['uuid']])
        self.assertEqual(self.option(first, '-smbios'), [
            f'type=1,serial={serials["system"]}', f'type=2,serial={serials["board"]}',
            f'type=3,serial={serials["chassis"]}',
        ])
        self.assertIn(f'file={self.guest("vm-01")}/disk.qcow2,if=none,id=disk0,format=qcow2', self.option(first, '-drive'))
        self.assertIn(f'virtio-blk-pci,drive=disk0,serial={serials["disk"]}', self.option(first, '-device'))
        self.assertFalse(any('serial=' in value for value in self.option(first, '-drive')))
        self.assertTrue(any(f'mac={identity["mac"]}' in value for value in self.option(first, '-device')))
        self.assertEqual(self.start('vm-01'), first)
        second = self.start('vm-02')
        self.assertTrue(set(self.option(first, '-smbios')).isdisjoint(self.option(second, '-smbios')))
        identifying = lambda arguments: {value for value in self.option(arguments, '-device')
                                          if 'serial=' in value or 'mac=' in value}
        self.assertTrue(identifying(first))
        self.assertTrue(identifying(first).isdisjoint(identifying(second)))

    def test_guest_disables_nested_virtualization_and_limits_syscalls(self):
        arguments = self.start('vm-01')
        self.assertEqual(self.option(arguments, '-cpu'), ['host,-vmx,-svm'])
        sandbox = self.option(arguments, '-sandbox')
        self.assertEqual(len(sandbox), 1, arguments)
        for restriction in ('on', 'obsolete=deny', 'elevateprivileges=deny',
                            'spawn=deny', 'resourcecontrol=deny'):
            self.assertIn(restriction, sandbox[0].split(','))

    def device_position(self, arguments, needle):
        """Позиция устройства среди аргументов: имена vda и vdb в госте
        раздаются в порядке создания устройств, а не в порядке -drive."""
        found = [index for index, value in enumerate(arguments)
                 if needle in value and index and arguments[index - 1] == '-device']
        self.assertEqual(len(found), 1, arguments)
        return found[0]

    def test_new_guest_gets_seed_as_virtio_block_without_cdrom(self):
        arguments = self.start('vm-01')
        drives = self.option(arguments, '-drive')
        seed = [value for value in drives if 'seed.iso' in value]
        self.assertEqual(len(seed), 1, drives)
        self.assertIn('if=none,id=seed', seed[0])
        self.assertIn('readonly=on', seed[0])
        self.assertFalse(any('cdrom' in value or 'media=' in value for value in drives), drives)
        # Системный диск должен создаваться раньше seed, иначе root=/dev/vda1
        # укажет на данные cloud-init. Слот seed закреплён явно.
        seed_device = self.device_position(arguments, 'drive=seed')
        self.assertIn('addr=0x1e', arguments[seed_device])
        self.assertLess(self.device_position(arguments, 'drive=disk0'), seed_device)
        # Выбор фиксируется в профиле при создании: повторный запуск ничего
        # не меняет, и файлов рядом с профилем для этого не нужно.
        profile = json.loads((self.guest('vm-01') / 'profile.json').read_text())
        self.assertEqual(profile['seed'], 'virtio')
        self.assertFalse((self.guest('vm-01') / '.seed-virtio').exists())
        self.assertEqual(self.start('vm-01'), arguments)

    def test_existing_guest_keeps_cdrom_seed(self):
        # Скрипт подготовки внутри созданного ранее гостя ищет устройство
        # так, как умел при создании, поэтому CD-ROM у него должен остаться.
        guest = self.guest('vm-01')
        (guest / 'disk.qcow2').write_text('')
        arguments = self.start('vm-01')
        drives = self.option(arguments, '-drive')
        self.assertIn(f'file={guest}/seed.iso,media=cdrom,readonly=on', drives)
        self.assertFalse(any('id=seed' in value for value in drives), drives)
        self.assertFalse(any('drive=seed' in value for value in self.option(arguments, '-device')))
        self.assertEqual(json.loads((guest / 'profile.json').read_text())['seed'], 'cdrom')
        self.assertEqual(self.start('vm-01'), arguments)

    def test_seed_transport_survives_lost_marker(self):
        # Решение о модели устройства живёт в профиле: восстановление тома
        # без скрытых файлов не должно переводить гостя обратно на CD-ROM.
        guest = self.guest('vm-01')
        arguments = self.start('vm-01')
        (guest / 'disk.qcow2').write_text('')
        (guest / '.seed-virtio').unlink(missing_ok=True)
        self.assertEqual(self.start('vm-01'), arguments)
        self.assertTrue(any('drive=seed' in value for value in self.option(arguments, '-device')))

    def test_guest_created_with_marker_keeps_virtio_seed(self):
        # Гость, созданный промежуточной версией, хранил выбор маркером.
        # Значение переносится в профиль и дальше берётся оттуда.
        guest = self.guest('vm-01')
        (guest / 'disk.qcow2').write_text('')
        (guest / '.seed-virtio').write_text('')
        arguments = self.start('vm-01')
        self.assertEqual(json.loads((guest / 'profile.json').read_text())['seed'], 'virtio')
        self.assertTrue(any('drive=seed' in value for value in self.option(arguments, '-device')))
        (guest / '.seed-virtio').unlink()
        self.assertEqual(self.start('vm-01'), arguments)

    def test_existing_disk_without_profile_keeps_platform_defaults(self):
        (self.guest('vm-01') / 'disk.qcow2').write_text('')
        arguments = self.start('vm-01')
        self.assertEqual(self.option(arguments, '-uuid'), [])
        self.assertEqual(self.option(arguments, '-smbios'), [])
        self.assertIn(f'file={self.guest("vm-01")}/disk.qcow2,if=virtio,format=qcow2', self.option(arguments, '-drive'))
        self.assertFalse(any('drive=disk0' in value for value in self.option(arguments, '-device')))
        self.assertFalse(any('mac=' in value for value in self.option(arguments, '-device')))

    def test_profile_without_serials_keeps_existing_hardware(self):
        guest = self.guest('vm-01')
        (guest / 'disk.qcow2').write_text('')
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        profile = guest / 'profile.json'
        profile.write_text(json.dumps({
            'schema_version': 1, 'vm_id': 'vm-01',
            'resources': {'vcpu': 2, 'ram_mb': 1024, 'disk_gb': 12},
            'proxy_ref': 'vm-01', 'identity': identity,
        }) + '\n')
        saved = json.loads(profile.read_text())
        arguments = self.start('vm-01')
        self.assertEqual(self.option(arguments, '-uuid'), [identity['uuid']])
        self.assertEqual(self.option(arguments, '-smbios'), [])
        self.assertIn(f'file={guest}/disk.qcow2,if=virtio,format=qcow2', self.option(arguments, '-drive'))
        self.assertFalse(any('drive=disk0' in value for value in self.option(arguments, '-device')))
        # Профиль прошлой версии дополняется только способом подключения:
        # оборудование существующего гостя остаётся прежним.
        self.assertEqual(json.loads(profile.read_text()), {**saved, 'seed': 'cdrom'})
