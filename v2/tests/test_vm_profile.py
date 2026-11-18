import json
import subprocess
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

GENERATOR = Path(__file__).resolve().parents[1] / 'vm-profile.py'


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
        vm_uuid, mac = first.stdout.split()
        self.assertEqual(uuid.UUID(vm_uuid).version, 4)
        self.assertEqual(int(mac[:2], 16) & 3, 2)
        self.assertNotEqual(vm_uuid, second.stdout.split()[0])
        self.assertNotEqual(mac, second.stdout.split()[1])
        path = self.workspace / 'vm-01.json'
        saved = path.read_bytes()
        profile = json.loads(saved)
        self.assertEqual(profile['identity'], {'uuid': vm_uuid, 'mac': mac})
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

    def test_parallel_creation_uses_one_saved_identity(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            jobs = [executor.submit(self.profile, 'vm-01') for _ in range(2)]
        first, second = [job.result() for job in jobs]
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)
        self.assertEqual(len(list(self.workspace.iterdir())), 1)
