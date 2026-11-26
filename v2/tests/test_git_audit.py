import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from v2.host.git_audit import AuditRules, GitAuditRepository, MirrorAudit


ROOT = Path(__file__).resolve().parents[2]


class GitAuditTest(unittest.TestCase):
    def test_audits_old_versions_without_printing_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            vm = state / 'vm-05'
            public = vm / 'git-host-public'
            private = vm / 'git-host-private'
            provider = vm / 'git'
            clean = public / 'view-abcdefgh12' / 'clean.git'
            for directory in (clean, private, provider):
                directory.mkdir(parents=True)
            seed = state / 'seed'
            fixture_token = 'glpat-' + 'a' * 24
            assigned_token = 'fixture-' + 'b' * 24
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                       GIT_AUTHOR_NAME='Fixture', GIT_AUTHOR_EMAIL='fixture@example.invalid',
                       GIT_COMMITTER_NAME='Fixture', GIT_COMMITTER_EMAIL='fixture@example.invalid')

            def git(*args: str) -> str:
                process = subprocess.run(['git', *args], env=env, text=True, capture_output=True, check=True)
                return process.stdout.strip()

            git('init', '-q', '--initial-branch=main', str(seed))
            (seed / 'CHANGELOG.md').write_text('Релиз\nhttps://gitlab.fixture.invalid/team/example\n')
            (seed / 'ignore.txt').write_text('https://gitlab.fixture.invalid\n')
            (seed / 'blob').write_bytes(b'\0' + fixture_token.encode())
            (seed / 'config.cfg').write_text(
                fixture_token + '\nhttps://registry.fixture.invalid/simple\n'
                'https://fixed:$REGISTRY_PASS@registry.fixture.invalid\n'
            )
            (seed / (assigned_token + '.txt')).write_text(assigned_token + '\n')
            git('-C', str(seed), 'add', '-A')
            git('-C', str(seed), 'commit', '-qm', 'История')
            old = git('-C', str(seed), 'rev-parse', 'HEAD')
            (seed / 'CHANGELOG.md').unlink()
            (seed / 'config.cfg').write_text('пустая конфигурация\n')
            git('-C', str(seed), 'add', '-A')
            git('-C', str(seed), 'commit', '-qm', 'Текущая версия')
            git('-C', str(seed), 'branch', 'reference')
            git('-C', str(seed), 'branch', assigned_token)
            git('init', '-q', '--bare', '--initial-branch=main', str(clean))
            git('-C', str(clean), 'fetch', '-q', str(seed), 'refs/heads/*:refs/heads/*')
            (public / 'current').write_text('view-abcdefgh12\n')
            (provider / 'repository.json').write_text(json.dumps({
                'origin': 'https://gitlab.fixture.invalid', 'project': 'team/example', 'token': assigned_token,
            }))
            rules = private / 'audit-rules.json'
            rules.write_text(json.dumps({
                'domains': ['registry.fixture.invalid'], 'ignored_paths': ['ignore.txt'],
            }))
            result = subprocess.run(
                ['bash', str(ROOT / 'v2/manage.sh'), 'vm-git-audit'],
                env=dict(env, VM='vm-05', V2_STATE_DIR=str(state)),
                text=True, capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            report = (private / 'audit-report.json').read_text()
            report_data = json.loads(report)
            self.assertEqual(set(report_data['heads']), {'main', 'reference', '[скрыто] (1)'})
            self.assertEqual(report_data['commits'], 2)
            self.assertIn({'path': 'CHANGELOG.md', 'commit': old, 'line': 2, 'rule': 'domain'},
                          report_data['findings'])
            self.assertIn({'path': 'config.cfg', 'commit': old, 'line': 1, 'rule': 'access-token'},
                          report_data['findings'])
            self.assertIn({'path': 'config.cfg', 'commit': old, 'line': 2, 'rule': 'domain'},
                          report_data['findings'])
            self.assertNotIn('ignore.txt', report)
            self.assertIn({'path': '[скрыто]', 'commit': old, 'line': 1, 'rule': 'assigned-token'},
                          report_data['findings'])
            self.assertIn({'path': 'config.cfg', 'commit': old, 'line': 3, 'rule': 'url-credentials-template'},
                          report_data['findings'])
            self.assertNotIn(fixture_token, report + result.stdout + result.stderr)
            self.assertNotIn(assigned_token, report + result.stdout + result.stderr)
            self.assertIn({'path': 'blob', 'commit': old}, report_data['skipped'])
            self.assertEqual(json.loads(rules.read_text())['ignored_paths'], ['ignore.txt'])
            fresh_only = GitAuditRepository(clean, env, old + '..refs/heads/main')
            incoming = MirrorAudit(fresh_only, AuditRules.load({}, {})).run()
            self.assertEqual(incoming['commits'], 1)
            self.assertEqual(incoming['skipped'], [])
            self.assertEqual(incoming['findings'], [])



if __name__ == '__main__':
    unittest.main()
