import base64
import fcntl
import json
import runpy
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

MIRROR = Path(__file__).resolve().parents[1] / 'git-mirror.py'
OWNER = {'GIT_AUTHOR_NAME': 'Ivan Petrov', 'GIT_AUTHOR_EMAIL': 'ivan.petrov@corp.example',
         'GIT_COMMITTER_NAME': 'Ivan Petrov', 'GIT_COMMITTER_EMAIL': 'ivan.petrov@corp.example'}
GUEST = {'GIT_AUTHOR_NAME': 'Guest Agent', 'GIT_AUTHOR_EMAIL': 'guest@vm.invalid',
         'GIT_COMMITTER_NAME': 'Guest Agent', 'GIT_COMMITTER_EMAIL': 'guest@vm.invalid'}
OTHER = {'GIT_AUTHOR_NAME': 'Petr Sidorov', 'GIT_AUTHOR_EMAIL': 'petr@other.example',
         'GIT_COMMITTER_NAME': 'Petr Sidorov', 'GIT_COMMITTER_EMAIL': 'petr@other.example'}
TOKEN = 'fixture_mirror_token_0004'
GITLAB_TOKEN = 'glpat-fixture.mirror_token-0006'
GITLAB_URL = 'https://gitlab.fixture-7c1e.invalid/scm'


class GitMirrorTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.upstream = self.root / 'upstream.git'
        self.mirror = self.root / 'mirror'
        self.config = self.root / 'repository.json'
        self.env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        self.env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
                        GIT_MIRROR_ROOT=str(self.mirror), GIT_MIRROR_CONFIG=str(self.config),
                        GIT_MIRROR_REMOTE=self.upstream.as_uri())
        self.git(self.root, 'init', '-q', '--bare', '--initial-branch=main', str(self.upstream))
        self.owner = self.root / 'owner'
        self.git(self.root, 'clone', '-q', str(self.upstream), str(self.owner))
        self.write(self.owner, {
            'README.md': 'Проект\n', 'src/app.py': 'print(1)\n', '.github/workflows/ci.yml': 'on: push\n',
            'docs/CODEOWNERS': '* @ivan-petrov\n', '.mailmap': 'Ivan <ivan.petrov@corp.example>\n',
            '.gitmodules': '[submodule "x"]\n\turl = https://corp.example/x.git\n', '.gitlab-ci.yml': 'job: {}\n',
        })
        (self.owner / 'data.bin').write_bytes(bytes(range(256)) * 4)
        self.git(self.owner, 'add', '-A')
        self.git(self.owner, 'update-index', '--add', '--cacheinfo', '160000,' + 'a' * 40 + ',vendor/lib')
        self.git(self.owner, 'commit', '-q', '-m', 'Merge pull request #7 from ivan-petrov/secret-feature', env=OWNER)
        self.git(self.owner, 'push', '-q', 'origin', 'main')
        self.configure(write=False)

    def git(self, cwd, *args, env=None, check=True, input=None):
        result = subprocess.run(['git', '-C', str(cwd), *args], env={**self.env, **(env or {})},
                                capture_output=True, text=True, timeout=60, input=input)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def write(self, repo, files):
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

    def commit(self, repo, message, identity):
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', message, env=identity)
        return self.git(repo, 'rev-parse', 'HEAD').stdout.strip()

    def configure(self, write, branch='', repository='owner/project', provider=None, origin=None, token=TOKEN):
        field = 'project' if provider == 'gitlab' else 'repository'
        config = {field: repository, 'token': token, 'write': write, 'writeBranch': branch}
        if provider:
            config['provider'] = provider
        if origin:
            config['origin'] = origin
        self.config.write_text(json.dumps(config))

    def configure_gitlab(self, write, branch='', repository='group/sub/team/project'):
        self.configure(write, branch, repository, 'gitlab', GITLAB_URL, GITLAB_TOKEN)

    def sync(self, check=True):
        result = subprocess.run([sys.executable, str(MIRROR), 'sync'], env=self.env,
                                capture_output=True, text=True, timeout=60)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def resolve(self):
        return subprocess.run([sys.executable, str(MIRROR), 'resolve'], env=self.env,
                              capture_output=True, text=True, timeout=60)

    def clean(self):
        result = self.resolve()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r'^mirrors/[0-9a-f]{64}\n$')
        return self.mirror / result.stdout.strip() / 'clean.git'

    def guest_clone(self, branch='main'):
        guest = self.root / f'guest-{len(list(self.root.glob("guest-*")))}'
        self.git(self.root, 'clone', '-q', '--branch', branch, str(self.clean()), str(guest))
        return guest

    def push(self, guest, refspec, check=True):
        result = self.git(guest, 'push', 'origin', refspec, check=False)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def all_objects(self, repo):
        return subprocess.run(['git', '-C', str(repo), 'cat-file', '--batch-all-objects', '--batch'],
                              env=self.env, capture_output=True, timeout=60).stdout

    def upstream_log(self, branch):
        return self.git(self.upstream, 'log', '--format=%an <%ae>|%s', branch).stdout.splitlines()

    def test_snapshot_hides_history_and_metadata(self):
        self.sync()
        guest = self.guest_clone()
        self.assertEqual(self.git(guest, 'log', '--format=%an <%ae> %ad|%s', '--date=unix').stdout.splitlines(),
                         ['agent-docker <agent-docker@localhost> 946684800|Снимок репозитория'])
        files = set(self.git(guest, 'ls-files').stdout.splitlines())
        self.assertEqual(files, {'README.md', 'src/app.py', 'data.bin'})
        self.assertIn('160000 commit ' + 'a' * 40 + '\tvendor/lib',
                      self.git(self.upstream, 'ls-tree', '-r', 'main').stdout)
        self.assertNotIn(bytes.fromhex('a' * 40), self.all_objects(self.clean()))
        self.assertEqual(self.git(guest, 'ls-remote', 'origin').stdout.count('\n'), 2)
        blob = self.all_objects(self.clean())
        upstream_head = self.git(self.upstream, 'rev-parse', 'main').stdout.strip()
        for secret in (b'ivan', b'corp.example', b'pull request', upstream_head.encode(), TOKEN.encode()):
            self.assertNotIn(secret, blob.lower() if secret.islower() else blob)
        self.assertEqual((self.clean() / 'HEAD').read_text(), 'ref: refs/heads/main\n')

    def test_snapshot_is_stable_and_follows_upstream(self):
        self.sync()
        first = self.git(self.clean(), 'rev-parse', 'main').stdout.strip()
        self.sync()
        self.assertEqual(self.git(self.clean(), 'rev-parse', 'main').stdout.strip(), first)
        self.write(self.owner, {'src/app.py': 'print(2)\n', '.github/workflows/ci.yml': 'on: pull_request\n'})
        self.commit(self.owner, 'Hotfix by Ivan', OWNER)
        self.git(self.owner, 'push', '-q', 'origin', 'main')
        self.sync()
        guest = self.guest_clone()
        log = self.git(guest, 'log', '--format=%an|%P|%s').stdout.splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(log[0].startswith(f'agent-docker|{first}|'))
        self.assertEqual((guest / 'src/app.py').read_text(), 'print(2)\n')
        self.write(self.owner, {'.github/workflows/ci.yml': 'on: workflow_dispatch\n'})
        self.commit(self.owner, 'Only hidden files', OWNER)
        self.git(self.owner, 'push', '-q', 'origin', 'main')
        self.sync()
        self.git(guest, 'fetch', '-q')
        self.assertEqual(self.git(guest, 'rev-list', '--count', 'origin/main').stdout.strip(), '2')

    def test_read_only_mirror_rejects_push(self):
        self.sync()
        guest = self.guest_clone()
        self.write(guest, {'src/app.py': 'print(3)\n'})
        self.commit(guest, 'change', GUEST)
        for refspec in ('HEAD:main', 'HEAD:refs/heads/other'):
            result = self.push(guest, refspec, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('agent-docker: Запись разрешена только в ветку work', result.stderr)
        self.assertEqual(len(self.upstream_log('main')), 1)

    def test_push_is_exported_with_service_identity(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        self.write(guest, {'src/app.py': 'print("agent")\n'})
        self.commit(guest, 'Изменён вывод', GUEST)
        (guest / 'image.bin').write_bytes(bytes(reversed(range(256))) * 8)
        (guest / 'data.bin').write_bytes(b'\0' * 1024)
        pushed = self.commit(guest, 'Добавлены данные', GUEST)
        result = self.push(guest, 'HEAD:work')
        self.assertNotIn('ivan', result.stderr.lower())
        upstream_main = self.git(self.upstream, 'rev-parse', 'main').stdout.strip()
        self.assertEqual(self.upstream_log('agent/work')[:2], [
            'agent-docker <agent-docker@localhost>|Добавлены данные',
            'agent-docker <agent-docker@localhost>|Изменён вывод',
        ])
        self.assertEqual(self.git(self.upstream, 'rev-parse', 'agent/work~2').stdout.strip(), upstream_main)
        tree = set(self.git(self.upstream, 'ls-tree', '-r', '--name-only', 'agent/work').stdout.splitlines())
        self.assertTrue({'.github/workflows/ci.yml', 'docs/CODEOWNERS', '.mailmap', 'image.bin'} <= tree)
        self.assertEqual(self.git(self.upstream, 'show', 'agent/work:src/app.py').stdout, 'print("agent")\n')
        exported = subprocess.run(['git', '-C', str(self.upstream), 'show', 'agent/work:data.bin'],
                                  env=self.env, capture_output=True).stdout
        self.assertEqual(exported, b'\0' * 1024)
        self.sync()
        self.assertEqual(self.git(self.clean(), 'rev-parse', 'work').stdout.strip(), pushed)
        self.assertEqual(self.git(self.clean(), 'rev-parse', 'main').stdout.strip(),
                         self.git(guest, 'rev-parse', 'origin/main').stdout.strip())
        self.write(guest, {'README.md': 'Проект агента\n'})
        self.commit(guest, 'Обновлён README', GUEST)
        self.push(guest, 'HEAD:work')
        self.assertEqual(len(self.upstream_log('agent/work')), 4)
        self.assertEqual(sorted(line.split('\t')[1] for line in self.git(guest, 'ls-remote', 'origin').stdout.splitlines()),
                         ['HEAD', 'refs/heads/main', 'refs/heads/work'])
        clean = self.clean()
        served = self.all_objects(clean) + self.git(clean, 'for-each-ref').stdout.encode()
        self.assertNotIn(b'agent/work', served)
        self.assertNotIn(b'guest@vm.invalid', self.all_objects(self.upstream))

    def test_push_rejections_keep_upstream_unchanged(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        base = self.git(guest, 'rev-parse', 'HEAD').stdout.strip()
        cases = [
            ({'.github/workflows/evil.yml': 'on: push\n'}, 'служебных файлов'),
            ({'lib/CODEOWNERS': '* @guest\n'}, 'служебных файлов'),
            ({'.mailmap': 'x\n'}, 'служебных файлов'),
        ]
        for files, expected in cases:
            with self.subTest(files=files):
                self.git(guest, 'reset', '-q', '--hard', base)
                self.write(guest, files)
                self.commit(guest, 'попытка', GUEST)
                result = self.push(guest, 'HEAD:work', check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)
        self.git(guest, 'reset', '-q', '--hard', base)
        self.git(guest, 'checkout', '-q', '-b', 'side')
        self.write(guest, {'side.txt': 'side\n'})
        self.commit(guest, 'side', GUEST)
        self.git(guest, 'checkout', '-q', 'work')
        self.write(guest, {'main.txt': 'main\n'})
        self.commit(guest, 'main', GUEST)
        self.git(guest, 'merge', '-q', '--no-ff', '-m', 'merge', 'side', env=GUEST)
        result = self.push(guest, 'HEAD:work', check=False)
        self.assertIn('Коммиты слияния не принимаются', result.stderr)
        self.git(guest, 'reset', '-q', '--hard', base)
        self.write(guest, {'other.txt': 'other\n'})
        self.commit(guest, 'other', GUEST)
        for refspec, expected in ((':work', 'Создание и удаление веток не разрешено'), ('HEAD:main', 'только в ветку work')):
            result = self.push(guest, refspec, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(expected, result.stderr)
        self.git(guest, 'reset', '-q', '--hard', base)
        self.git(guest, 'update-index', '--add', '--cacheinfo', '160000,' + 'b' * 40 + ',vendor/guest')
        self.git(guest, 'commit', '-q', '-m', 'gitlink', env=GUEST)
        result = self.push(guest, 'HEAD:work', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('подмодулей', result.stderr)
        self.assertEqual(self.git(self.upstream, 'ls-remote', '--heads', '.').stdout.count('\n'), 1)
        self.assertEqual(self.git(self.clean(), 'config', 'receive.maxInputSize').stdout.strip(), '256m')

    def test_lost_ref_update_recovers_on_next_sync(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        before = self.git(guest, 'rev-parse', 'HEAD').stdout.strip()
        self.write(guest, {'src/app.py': 'print("lost")\n'})
        self.commit(guest, 'lost one', GUEST)
        self.write(guest, {'src/app.py': 'print("lost twice")\n'})
        pushed = self.commit(guest, 'lost ref', GUEST)
        self.push(guest, 'HEAD:work')
        self.git(self.clean(), 'update-ref', 'refs/heads/work', before)
        self.sync()
        self.assertEqual(self.git(self.clean(), 'rev-parse', 'work').stdout.strip(), pushed)
        self.git(guest, 'pull', '-q', '--rebase', env=GUEST)
        self.assertEqual(self.git(guest, 'rev-parse', 'HEAD').stdout.strip(), pushed)
        self.write(guest, {'next.txt': 'next\n'})
        self.commit(guest, 'after recovery', GUEST)
        self.push(guest, 'HEAD:work')
        self.assertEqual([line.split('|')[1] for line in self.upstream_log('agent/work')],
                         ['after recovery', 'lost ref', 'lost one', 'Merge pull request #7 from ivan-petrov/secret-feature'])

    def test_rebuilt_snapshot_after_lost_objects(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        before = self.git(guest, 'rev-parse', 'HEAD').stdout.strip()
        self.write(guest, {'src/app.py': 'print("gone")\n'})
        self.commit(guest, 'gone', GUEST)
        self.push(guest, 'HEAD:work')
        state_path = (self.clean().parent / 'state.json')
        state = json.loads(state_path.read_text())
        state['branches']['work']['clean'] = 'f' * 40
        state_path.write_text(json.dumps(state))
        self.git(self.clean(), 'update-ref', 'refs/heads/work', before)
        self.sync()
        tip = self.git(self.clean(), 'log', '-1', '--format=%an|%P', 'work').stdout.strip()
        self.assertEqual(tip, f'agent-docker|{before}')
        self.assertEqual(self.git(self.clean(), 'show', 'work:src/app.py').stdout, 'print("gone")\n')

    def test_upstream_change_requires_rebase(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        self.write(guest, {'src/app.py': 'print("first")\n'})
        self.commit(guest, 'first', GUEST)
        self.push(guest, 'HEAD:work')
        self.git(self.owner, 'fetch', '-q', 'origin')
        self.git(self.owner, 'checkout', '-q', '-b', 'agent/work', 'origin/agent/work')
        self.write(self.owner, {'README.md': 'Правка владельца\n'})
        self.commit(self.owner, 'Ivan edits README', OWNER)
        self.git(self.owner, 'push', '-q', 'origin', 'agent/work')
        self.write(guest, {'notes.txt': 'agent notes\n'})
        self.commit(guest, 'second', GUEST)
        result = self.push(guest, 'HEAD:work', check=False)
        self.assertIn('git pull --rebase', result.stderr)
        self.sync()
        self.git(guest, 'pull', '-q', '--rebase', env=GUEST)
        self.assertEqual((guest / 'README.md').read_text(), 'Правка владельца\n')
        self.assertNotIn('Ivan', self.git(guest, 'log', '--format=%an %s').stdout)
        self.push(guest, 'HEAD:work')
        self.assertEqual(self.upstream_log('agent/work')[0], 'agent-docker <agent-docker@localhost>|second')
        self.assertEqual(self.git(self.upstream, 'show', 'agent/work:notes.txt').stdout, 'agent notes\n')

    def other_upstream(self):
        other = self.root / 'other.git'
        self.git(self.root, 'init', '-q', '--bare', '--initial-branch=main', str(other))
        work = self.root / 'other-owner'
        self.git(self.root, 'clone', '-q', str(other), str(work))
        self.write(work, {'other.txt': 'другой проект\n'})
        self.commit(work, 'Initial import by Petr', OTHER)
        self.git(work, 'push', '-q', 'origin', 'main')
        return other

    def upload_pack(self, clean, want):
        line = f'want {want}\n'
        body = f'{len(line) + 4:04x}{line}00000009done\n'.encode()
        env = {**self.env, 'GIT_PROJECT_ROOT': str(self.mirror), 'GIT_HTTP_EXPORT_ALL': '1',
               'PATH_INFO': '/' + str(clean.relative_to(self.mirror)) + '/git-upload-pack',
               'REQUEST_METHOD': 'POST', 'CONTENT_TYPE': 'application/x-git-upload-pack-request',
               'CONTENT_LENGTH': str(len(body)), 'REMOTE_ADDR': '127.0.0.1'}
        return subprocess.run(['git', 'http-backend'], env=env, input=body, capture_output=True, timeout=60).stdout

    def served(self, clean):
        return self.all_objects(clean) + self.git(clean, 'for-each-ref').stdout.encode()

    def advance_main(self, content):
        self.git(self.owner, 'checkout', '-q', 'main')
        self.git(self.owner, 'pull', '-q', 'origin', 'main')
        self.write(self.owner, {'README.md': content})
        self.commit(self.owner, 'Ivan updates README', OWNER)
        self.git(self.owner, 'push', '-q', 'origin', 'main')
        return self.git(self.upstream, 'rev-parse', 'main').stdout.strip()

    def test_new_write_branch_starts_from_current_main(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        first = self.git(self.clean(), 'rev-parse', 'work').stdout.strip()
        head = self.advance_main('Новая версия\n')
        self.sync()
        clean = self.clean()
        work = self.git(clean, 'rev-parse', 'work').stdout.strip()
        self.assertEqual(work, self.git(clean, 'rev-parse', 'main').stdout.strip())
        self.assertNotEqual(work, first)
        self.assertEqual(self.git(clean, 'merge-base', '--is-ancestor', first, work, check=False).returncode, 0)
        guest = self.guest_clone('work')
        self.assertEqual((guest / 'README.md').read_text(), 'Новая версия\n')
        self.write(guest, {'notes.txt': 'agent\n'})
        self.commit(guest, 'first push', GUEST)
        self.push(guest, 'HEAD:work')
        self.assertEqual(self.git(self.upstream, 'rev-parse', 'agent/work~1').stdout.strip(), head)
        self.assertEqual(self.git(self.upstream, 'show', 'agent/work:README.md').stdout, 'Новая версия\n')

    def test_stale_base_is_refused_until_clone_is_updated(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        self.write(guest, {'notes.txt': 'agent work\n'})
        work = self.commit(guest, 'agent work', GUEST)
        head = self.advance_main('Обновлено владельцем\n')
        result = self.push(guest, 'HEAD:work', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Основная ветка обновилась. Выполните git pull --rebase', result.stderr)
        self.assertEqual(self.git(self.upstream, 'ls-remote', '--heads', '.').stdout.count('\n'), 1)
        self.assertEqual(self.git(guest, 'rev-parse', 'HEAD').stdout.strip(), work)
        self.sync()
        self.git(guest, 'pull', '-q', '--rebase', env=GUEST)
        self.assertEqual((guest / 'notes.txt').read_text(), 'agent work\n')
        self.assertEqual((guest / 'README.md').read_text(), 'Обновлено владельцем\n')
        self.push(guest, 'HEAD:work')
        self.assertEqual(self.upstream_log('agent/work')[0], 'agent-docker <agent-docker@localhost>|agent work')
        self.assertEqual(self.git(self.upstream, 'rev-parse', 'agent/work~1').stdout.strip(), head)
        self.assertEqual(self.git(self.upstream, 'show', 'agent/work:notes.txt').stdout, 'agent work\n')

    def test_gitlab_subgroup_clone_fetch_push(self):
        self.configure_gitlab(write=True, branch='agent/task-1')
        self.sync()
        clean = self.clean()
        self.assertEqual(self.git(clean, 'for-each-ref', '--format=%(refname)').stdout.split(),
                         ['refs/heads/main', 'refs/heads/work'])
        guest = self.guest_clone('work')
        self.assertEqual(set(self.git(guest, 'ls-files').stdout.split()), {'README.md', 'src/app.py', 'data.bin'})
        self.assertEqual(self.git(guest, 'log', '--format=%an <%ae>').stdout, 'agent-docker <agent-docker@localhost>\n')
        self.write(guest, {'src/app.py': 'print("gitlab")\n'})
        self.commit(guest, 'GitLab change', GUEST)
        self.push(guest, 'HEAD:work')
        self.assertEqual(self.upstream_log('agent/task-1')[0], 'agent-docker <agent-docker@localhost>|GitLab change')
        self.assertEqual(self.git(self.upstream, 'show', 'agent/task-1:src/app.py').stdout, 'print("gitlab")\n')
        result = self.push(guest, 'HEAD:main', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Запись разрешена только в ветку work', result.stderr)
        self.git(guest, 'fetch', '-q', 'origin')
        served = self.served(self.clean())
        for secret in (GITLAB_URL.encode(), b'gitlab.fixture-7c1e', GITLAB_TOKEN.encode(), b'group/sub/team',
                       b'agent/task-1', b'ivan'):
            self.assertNotIn(secret, served.lower() if secret.islower() else served)
        state = (self.clean().parent / 'state.json').read_text()
        self.assertNotIn(GITLAB_TOKEN, state)

    def test_provider_change_rebuilds_mirror_without_serving_old(self):
        self.configure(write=False)
        self.sync()
        github = self.clean()
        self.configure_gitlab(write=False, repository='owner/project')
        result = self.resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertFalse(github.parent.exists())
        self.sync()
        gitlab = self.clean()
        self.assertNotEqual(github, gitlab)
        key = json.loads((gitlab.parent / 'state.json').read_text())['key']
        self.assertEqual((key['provider'], key['origin']), ('gitlab', GITLAB_URL))
        self.configure_gitlab(write=False, repository='owner/project')
        self.config.write_text(self.config.read_text().replace(GITLAB_URL, 'https://gitlab.other-7c1e.invalid'))
        self.assertNotEqual(self.resolve().returncode, 0)
        self.assertFalse(gitlab.parent.exists())
        self.configure(write=False)
        self.sync()
        self.assertEqual(json.loads((self.clean().parent / 'state.json').read_text())['key'],
                         {'layout': 3, 'repository': 'owner/project', 'writeBranch': None})

    def test_remote_url_and_credentials_per_provider(self):
        upstream = self.root / 'upstream-proxy'
        upstream.write_text('http://proxy-user:proxy-pass@proxy.fixture.invalid:3128\n')
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_MIRROR_')}
        env.update(GIT_MIRROR_ROOT=str(self.mirror), GIT_MIRROR_UPSTREAM=str(upstream))
        previous = dict(os.environ)
        os.environ.clear()
        os.environ.update(env)
        try:
            module = runpy.run_path(str(MIRROR))
        finally:
            os.environ.clear()
            os.environ.update(previous)
        cases = [
            ({'provider': 'github', 'origin': 'https://github.com', 'repository': 'owner/project', 'token': TOKEN},
             'https://github.com/owner/project.git', 'x-access-token:' + TOKEN),
            ({'provider': 'gitlab', 'origin': GITLAB_URL, 'repository': 'group/sub/team/project', 'token': GITLAB_TOKEN},
             GITLAB_URL + '/group/sub/team/project.git', 'oauth2:' + GITLAB_TOKEN),
        ]
        for config, url, credentials in cases:
            with self.subTest(provider=config['provider']):
                remote, git_env = module['remote_env'](config)
                self.assertEqual(remote, url)
                options = {git_env[f'GIT_CONFIG_KEY_{index}']: git_env[f'GIT_CONFIG_VALUE_{index}']
                           for index in range(int(git_env['GIT_CONFIG_COUNT']))}
                self.assertEqual(options['http.proxy'], 'http://proxy-user:proxy-pass@proxy.fixture.invalid:3128')
                self.assertEqual(options['http.followRedirects'], 'false')
                self.assertEqual(options['credential.helper'], '')
                header = options['http.extraHeader']
                self.assertEqual(base64.b64decode(header.removeprefix('Authorization: Basic ')).decode(), credentials)
                self.assertNotIn(config['token'], remote)

    def test_invalid_provider_configuration_is_refused(self):
        valid = {'provider': 'gitlab', 'origin': GITLAB_URL, 'project': 'group/project', 'token': GITLAB_TOKEN,
                 'write': False, 'writeBranch': ''}
        accepted = [
            valid, {**valid, 'project': '/'.join(['g'] * 20)}, {**valid, 'project': 'g/' + 'p' * 255},
            {**valid, 'origin': 'https://gitlab.fixture.invalid:65535/a.b/c_d'}, {**valid, 'origin': 'https://gitlab'},
            {**valid, 'origin': 'https://scm-01.fixture.invalid'},
            {'repository': 'owner/project.atom', 'token': TOKEN, 'write': False, 'writeBranch': ''},
            {'provider': 'github', 'origin': 'https://github.com', 'repository': 'owner/project', 'token': TOKEN,
             'write': False, 'writeBranch': ''},
        ]
        for config in accepted:
            with self.subTest(accepted=config):
                self.config.write_text(json.dumps(config))
                self.assertEqual(self.sync(check=False).returncode, 0)
        cases = [
            {**valid, 'provider': None}, {**valid, 'provider': ['gitlab']}, {**valid, 'origin': None},
            {**valid, 'repository': 'group/project'}, {key: value for key, value in valid.items() if key != 'project'},
            {**valid, 'origin': 'https://user@gitlab.fixture.invalid'}, {**valid, 'origin': GITLAB_URL + '/..'},
            {**valid, 'origin': GITLAB_URL + '/./x'}, {**valid, 'origin': 'https://GitLab.fixture.invalid'},
            {**valid, 'origin': 'https://10.0.0.5'}, {**valid, 'origin': 'https://10.0.0.5:8443/scm'},
            {**valid, 'origin': 'https://github.com:443'}, {**valid, 'origin': 'https://www.github.com'},
            {**valid, 'origin': 'https://github.com/scm'}, {**valid, 'origin': 'https://gitlab.fixture.invalid:65536'},
            {**valid, 'origin': 'https://gitlab.fixture.invalid:0'}, ['not', 'an', 'object'], 'text', None,
            {**valid, 'project': 'group/project.atom'},
            {'provider': 'github', 'project': 'owner/project', 'token': TOKEN, 'write': False, 'writeBranch': ''},
            {'repository': 'owner/project', 'project': 'owner/project', 'token': TOKEN, 'write': False, 'writeBranch': ''},
            {**valid, 'provider': 'bitbucket'}, {**valid, 'origin': 'http://gitlab.fixture.invalid'},
            {**valid, 'origin': 'https://user:pass@gitlab.fixture.invalid'},
            {**valid, 'origin': 'https://gitlab.fixture.invalid/'}, {**valid, 'origin': 'https://gitlab.fixture.invalid?x=1'},
            {**valid, 'origin': 'https://github.com'}, {key: value for key, value in valid.items() if key != 'origin'},
            {**valid, 'project': 'project'}, {**valid, 'project': 'group/project.git'},
            {**valid, 'project': 'group/-project'}, {**valid, 'project': 'group/../project'},
            {**valid, 'project': '/'.join(['g'] * 21)}, {**valid, 'project': 'g/' + 'p' * 256},
            {**valid, 'token': 'short'},
            {'provider': 'github', 'origin': GITLAB_URL, 'repository': 'owner/project', 'token': TOKEN,
             'write': False, 'writeBranch': ''},
        ]
        for config in cases:
            with self.subTest(config=config):
                self.config.write_text(json.dumps(config))
                result = self.sync(check=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stderr, 'agent-docker: Git-шлюз не смог обработать запрос.\n')

    def test_unreachable_gitlab_does_not_reveal_address_or_token(self):
        self.configure_gitlab(write=True, branch='agent/task-1')
        upstream = self.root / 'upstream-proxy'
        upstream.write_text('http://proxy-user-7c1e:proxy-pass-7c1e@127.0.0.1:9\n')
        self.env.pop('GIT_MIRROR_REMOTE')
        self.env['GIT_MIRROR_UPSTREAM'] = str(upstream)
        for command in ('sync', 'resolve'):
            result = subprocess.run([sys.executable, str(MIRROR), command], env=self.env,
                                    capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            output = result.stdout + result.stderr
            for secret in ('gitlab.fixture-7c1e', GITLAB_TOKEN, 'proxy-pass-7c1e', 'group/sub/team', 'agent/task-1'):
                self.assertNotIn(secret, output)

    def test_configuration_change_rebuilds_mirror(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        old = self.clean()
        self.assertEqual(self.git(old, 'branch', '--format=%(refname)').stdout.split(),
                         ['refs/heads/main', 'refs/heads/work'])
        self.configure(write=False, repository='owner/other')
        self.sync()
        new = self.clean()
        self.assertNotEqual(old, new)
        self.assertFalse(old.parent.exists())
        self.assertEqual(self.git(new, 'branch', '--format=%(refname)').stdout.split(), ['refs/heads/main'])
        self.assertEqual(json.loads((new.parent / 'state.json').read_text())['key'],
                         {'layout': 3, 'repository': 'owner/other', 'writeBranch': None})

    def test_assignment_change_without_sync_serves_no_old_mirror(self):
        self.sync()
        old = self.clean()
        head = self.git(old, 'rev-parse', 'main').stdout.strip()
        self.assertIn(b'PACK', self.upload_pack(old, head))
        self.env['GIT_MIRROR_REMOTE'] = self.other_upstream().as_uri()
        self.configure(write=False, repository='owner/other')
        result = self.resolve()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertFalse(old.parent.exists())
        self.assertEqual(sorted(path.name for path in (self.mirror / 'mirrors').iterdir()), [])
        self.assertNotIn(b'PACK', self.upload_pack(old, head))
        self.sync()
        guest = self.guest_clone()
        self.assertEqual(set(self.git(guest, 'ls-files').stdout.split()), {'other.txt'})
        served = self.served(self.clean())
        for secret in ('Проект'.encode(), b'print(1)', b'Petr', b'other.example'):
            self.assertNotIn(secret, served)

    def test_push_into_previous_assignment_is_refused(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        guest = self.guest_clone('work')
        self.write(guest, {'src/app.py': 'print("stale")\n'})
        self.commit(guest, 'stale', GUEST)
        self.configure(write=True, branch='agent/other')
        result = self.push(guest, 'HEAD:work', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Назначение репозитория изменилось', result.stderr)
        self.assertEqual(self.git(self.upstream, 'ls-remote', '--heads', '.').stdout.count('\n'), 1)

    def test_hook_refuses_repository_of_other_assignment(self):
        self.configure(write=True, branch='agent/work')
        self.sync()
        self.assertEqual((self.mirror / 'secret').stat().st_mode & 0o777, 0o600)
        before = self.clean()
        (self.mirror / 'secret').write_bytes(b'')
        self.sync()
        self.assertEqual(len((self.mirror / 'secret').read_bytes()), 32)
        self.assertEqual((self.mirror / 'secret').stat().st_mode & 0o777, 0o600)
        self.assertNotEqual(self.clean(), before)
        self.assertFalse(before.parent.exists())
        foreign = self.root / 'foreign.git'
        self.git(self.root, 'init', '-q', '--bare', str(foreign))
        line = f'{"1" * 40} {"2" * 40} refs/heads/work\n'
        for git_dir, cwd in ((str(foreign), self.root), ('.', foreign)):
            result = subprocess.run([sys.executable, str(MIRROR), 'receive'], env={**self.env, 'GIT_DIR': git_dir},
                                    cwd=cwd, input=line, capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Назначение репозитория изменилось', result.stderr)
        self.assertEqual(self.git(self.upstream, 'ls-remote', '--heads', '.').stdout.count('\n'), 1)

    def test_waiting_sync_uses_assignment_read_under_lock(self):
        self.sync()
        old = self.clean()
        env = {**self.env, 'GIT_MIRROR_REMOTE': self.other_upstream().as_uri()}
        with open(self.mirror / 'lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            waiting = subprocess.Popen([sys.executable, str(MIRROR), 'sync'], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 30
            while f' {waiting.pid} ' not in ''.join(line for line in Path('/proc/locks').read_text().splitlines(True)
                                                    if '->' in line):
                self.assertLess(time.monotonic(), deadline, 'sync не встал в очередь на блокировку')
                time.sleep(0.05)
            self.configure(write=False, repository='owner/other')
        stdout, stderr = waiting.communicate(timeout=60)
        self.assertEqual(waiting.returncode, 0, stderr)
        self.assertEqual(self.mirror / stdout.strip() / 'clean.git', self.clean())
        self.assertFalse(old.parent.exists())
        self.assertEqual(json.loads((self.clean().parent / 'state.json').read_text())['key']['repository'],
                         'owner/other')

    def test_legacy_layout_with_real_branch_name_is_removed(self):
        legacy = self.mirror / 'clean.git'
        self.mirror.mkdir()
        self.git(self.root, 'init', '-q', '--bare', str(legacy))
        self.git(self.owner, 'push', '-q', str(legacy), 'main:refs/heads/main', 'main:refs/heads/agent/work')
        (self.mirror / 'source.git').mkdir()
        (self.mirror / 'state.json').write_text(json.dumps({
            'key': {'layout': 2, 'repository': 'owner/project', 'writeBranch': 'agent/work'}, 'branches': {},
        }))
        self.write(self.owner, {'src/app.py': 'print("merged")\n'})
        self.commit(self.owner, "Merge branch 'agent/work' into main", OWNER)
        self.git(self.owner, 'push', '-q', 'origin', 'main')
        self.configure(write=True, branch='agent/work')
        self.sync()
        for path in ('clean.git', 'source.git', 'state.json'):
            self.assertFalse((self.mirror / path).exists(), path)
        clean = self.clean()
        self.assertEqual(self.git(clean, 'for-each-ref', '--format=%(refname)').stdout.split(),
                         ['refs/heads/main', 'refs/heads/work'])
        self.assertNotIn(b'agent/work', self.served(clean))
        guest = self.guest_clone('work')
        self.assertEqual((guest / 'src/app.py').read_text(), 'print("merged")\n')

    def test_failures_are_reported_without_details(self):
        self.env['GIT_MIRROR_REMOTE'] = (self.root / 'missing.git').as_uri()
        result = self.sync(check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stderr, 'agent-docker: Git-шлюз не смог обработать запрос.\n')
        self.config.write_text('{"token": "' + TOKEN + '"')
        result = self.sync(check=False)
        self.assertNotIn(TOKEN, result.stderr)


if __name__ == '__main__':
    unittest.main()
