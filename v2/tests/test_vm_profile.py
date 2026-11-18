import json
import os
import shutil
import subprocess
import sys
import tempfile
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
        return subprocess.run(
            [sys.executable, str(GENERATOR), str(self.workspace / f'{name}.json'),
             name, cpu, '1024', '12', *options],
            capture_output=True, text=True, timeout=5,
        )

    def test_new_guest_gets_distinct_persistent_uuid_and_mac(self):
        first = self.profile('vm-01')
        second = self.profile('vm-02')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        vm_uuid, mac, *serials = first.stdout.split()
        self.assertEqual(uuid.UUID(vm_uuid).version, 4)
        self.assertEqual(int(mac[:2], 16) & 3, 2)
        self.assertEqual(len(serials), 4)
        for serial in serials:
            self.assertRegex(serial, r'^[0-9A-F]{20}$')
        self.assertEqual(len(set(serials)), 4)
        other = second.stdout.split()
        for own, foreign in zip(first.stdout.split(), other):
            self.assertNotEqual(own, foreign)
        path = self.workspace / 'vm-01.json'
        saved = path.read_bytes()
        profile = json.loads(saved)
        self.assertEqual(profile['identity'], {
            'uuid': vm_uuid, 'mac': mac,
            'serials': dict(zip(('system', 'board', 'chassis', 'disk'), serials)),
        })
        self.assertEqual(profile['proxy_ref'], 'vm-01')
        restarted = self.profile('vm-01', '--retain-existing-identity')
        self.assertEqual(restarted.returncode, 0, restarted.stderr)
        self.assertEqual(restarted.stdout, first.stdout)
        self.assertEqual(path.read_bytes(), saved)

    def test_existing_disk_keeps_platform_defaults(self):
        first = self.profile('vm-01', '--retain-existing-identity')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout, '')
        path = self.workspace / 'vm-01.json'
        self.assertIsNone(json.loads(path.read_bytes())['identity'])
        repeated = self.profile('vm-01')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(repeated.stdout, '')

    def test_mismatched_resources_keep_saved_profile(self):
        created = self.profile('vm-01')
        self.assertEqual(created.returncode, 0, created.stderr)
        path = self.workspace / 'vm-01.json'
        saved = path.read_bytes()
        mismatched = self.profile('vm-01', cpu='4')
        self.assertNotEqual(mismatched.returncode, 0)
        self.assertIn('не соответствует', mismatched.stderr)
        self.assertEqual(path.read_bytes(), saved)

    def saved_profile(self, name, identity):
        path = self.workspace / f'{name}.json'
        path.write_text(json.dumps({
            'schema_version': 1, 'vm_id': name,
            'resources': {'vcpu': 2, 'ram_mb': 1024, 'disk_gb': 12},
            'proxy_ref': name, 'identity': identity,
        }, indent=2) + '\n')
        return path, path.read_bytes()

    def test_profile_without_serials_keeps_existing_hardware(self):
        identity = {'uuid': str(uuid.uuid4()), 'mac': '02:11:22:33:44:55'}
        path, saved = self.saved_profile('vm-01', identity)
        for options in ((), ('--retain-existing-identity',)):
            result = self.profile('vm-01', *options)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.split(), [identity['uuid'], identity['mac']])
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
        self.assertTrue(set(self.option(first, '-device')).isdisjoint(self.option(second, '-device')))

    def test_existing_disk_without_profile_keeps_platform_defaults(self):
        (self.guest('vm-01') / 'disk.qcow2').write_text('')
        arguments = self.start('vm-01')
        self.assertEqual(self.option(arguments, '-uuid'), [])
        self.assertEqual(self.option(arguments, '-smbios'), [])
        self.assertIn(f'file={self.guest("vm-01")}/disk.qcow2,if=virtio,format=qcow2', self.option(arguments, '-drive'))
        self.assertFalse(any('virtio-blk-pci' in value for value in self.option(arguments, '-device')))
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
        saved = profile.read_bytes()
        arguments = self.start('vm-01')
        self.assertEqual(self.option(arguments, '-uuid'), [identity['uuid']])
        self.assertEqual(self.option(arguments, '-smbios'), [])
        self.assertIn(f'file={guest}/disk.qcow2,if=virtio,format=qcow2', self.option(arguments, '-drive'))
        self.assertFalse(any('virtio-blk-pci' in value for value in self.option(arguments, '-device')))
        self.assertEqual(profile.read_bytes(), saved)
