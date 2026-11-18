import base64
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(os.environ.get('GIT_MIRROR_ROOT', '/mirror'))
CONFIG = Path(os.environ.get('GIT_MIRROR_CONFIG', '/config/git/repository.json'))
UPSTREAM = Path(os.environ.get('GIT_MIRROR_UPSTREAM', '/config/upstream'))
REMOTE = os.environ.get('GIT_MIRROR_REMOTE', '')
CLEAN = ROOT / 'clean.git'
SOURCE = ROOT / 'source.git'
STATE = ROOT / 'state.json'
ORIGIN = 'github.com'
USERNAME = 'x-access-token'
READ_BRANCH = 'main'
WORK_BRANCH = 'work'
LAYOUT = 2
IDENTITY = {
    'GIT_AUTHOR_NAME': 'agent-docker', 'GIT_AUTHOR_EMAIL': 'agent-docker@localhost',
    'GIT_COMMITTER_NAME': 'agent-docker', 'GIT_COMMITTER_EMAIL': 'agent-docker@localhost',
}
SNAPSHOT_DATE = '946684800 +0000'
EXCLUDED_ROOTS = {'.github', '.gitlab'}
EXCLUDED_FILES = {'.gitmodules', '.gitlab-ci.yml', '.mailmap', '.git-blame-ignore-revs'}
MAX_COMMITS = 100
MAX_PUSH_SIZE = '256m'
GITLINK = b'160000'
ZERO = '0' * 40


class Refused(Exception):
    pass


def excluded(path):
    parts = path.split('/')
    return parts[0] in EXCLUDED_ROOTS or path in EXCLUDED_FILES or parts[-1] == 'CODEOWNERS'


def clean_env(**extra):
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0',
               HOME=str(ROOT), TMPDIR=str(ROOT / 'tmp'), **extra)
    return env


def git(repo, *args, env=None, input=None, check=True):
    result = subprocess.run(['git', '-C', str(repo), *args], env=env or clean_env(), input=input,
                            capture_output=True, timeout=600)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, args[0])
    return result


def output(repo, *args, **options):
    return git(repo, *args, **options).stdout.decode().strip()


def load_config():
    config = json.loads(CONFIG.read_text())
    repository, token, write = config.get('repository'), config.get('token'), config.get('write')
    if (not isinstance(repository, str) or repository.endswith('.git') or
            not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}', repository) or
            not isinstance(token, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{10,512}', token) or
            not isinstance(write, bool)):
        raise ValueError('Некорректная конфигурация Git-шлюза.')
    branch = config.get('writeBranch')
    writable = (write and isinstance(branch, str) and len(branch) <= 120 and branch not in ('main', 'master') and
                re.fullmatch(r'(?:[a-zA-Z0-9][a-zA-Z0-9_-]*/)*[a-zA-Z0-9][a-zA-Z0-9_-]*', branch))
    return {'repository': repository, 'token': token, 'branch': branch if writable else None}


def remote_env(config):
    if REMOTE:
        return REMOTE, clean_env()
    credentials = base64.b64encode(f'{USERNAME}:{config["token"]}'.encode()).decode()
    options = [('http.proxy', UPSTREAM.read_text().strip()), ('http.followRedirects', 'false'),
               ('http.extraHeader', f'Authorization: Basic {credentials}'), ('credential.helper', '')]
    env = clean_env(GIT_CONFIG_COUNT=str(len(options)))
    for index, (key, value) in enumerate(options):
        env[f'GIT_CONFIG_KEY_{index}'] = key
        env[f'GIT_CONFIG_VALUE_{index}'] = value
    return f'https://{ORIGIN}/{config["repository"]}.git', env


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / 'lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def save_state(state):
    with tempfile.NamedTemporaryFile('w', dir=ROOT, delete=False) as draft:
        json.dump(state, draft)
    os.replace(draft.name, STATE)


def load_state(config, reset=True):
    key = {'layout': LAYOUT, 'repository': config['repository'], 'writeBranch': config['branch']}
    state = json.loads(STATE.read_text()) if STATE.exists() else None
    if state is None or state.get('key') != key:
        if not reset:
            raise Refused('Настройка Git-шлюза изменилась. Выполните git fetch и повторите push.')
        for path in (CLEAN, SOURCE, STATE):
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        state = {'key': key, 'branches': {}}
    return state


def prepare():
    (ROOT / 'tmp').mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        git(ROOT, 'init', '-q', '--bare', str(SOURCE))
    if not CLEAN.exists():
        git(ROOT, 'init', '-q', '--bare', f'--initial-branch={READ_BRANCH}', str(CLEAN))
        for key, value in (('http.receivepack', 'true'), ('receive.denyDeletes', 'true'),
                           ('receive.denyNonFastForwards', 'true'), ('receive.maxInputSize', MAX_PUSH_SIZE),
                           ('core.hooksPath', str(ROOT / 'hooks'))):
            git(CLEAN, 'config', key, value)
    hooks = ROOT / 'hooks'
    hooks.mkdir(exist_ok=True)
    hook = hooks / 'pre-receive'
    content = f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} receive\n'
    if not hook.exists() or hook.read_text() != content:
        hook.write_text(content)
        hook.chmod(0o755)


def ref(repo, name, env=None):
    result = git(repo, 'rev-parse', '-q', '--verify', name + '^{commit}', env=env, check=False)
    return result.stdout.decode().strip() or None


def upstream_heads(config, branch):
    url, env = remote_env(config)
    patterns = ['HEAD'] + ([f'refs/heads/{branch}'] if branch else [])
    listing = {}
    for line in git(SOURCE, 'ls-remote', url, *patterns, env=env).stdout.decode().splitlines():
        sha, name = line.split('\t')
        listing[name] = sha
    if 'HEAD' not in listing:
        raise RuntimeError('В репозитории нет ветки по умолчанию.')
    return listing['HEAD'], listing.get(f'refs/heads/{branch}') if branch else None


def fetch(config, branch):
    head, write = upstream_heads(config, branch)
    if ref(SOURCE, 'refs/source/head') == head and (not write or ref(SOURCE, 'refs/source/write') == write):
        return head, write
    url, env = remote_env(config)
    refspecs = ['+HEAD:refs/source/head'] + ([f'+refs/heads/{branch}:refs/source/write'] if write else [])
    git(SOURCE, 'fetch', '-q', '--no-tags', '--depth=1', url, *refspecs, env=env)
    head = ref(SOURCE, 'refs/source/head')
    write = ref(SOURCE, 'refs/source/write') if write else None
    return head, write


def sanitized_tree(commit):
    with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as scratch:
        env = clean_env(GIT_INDEX_FILE=f'{scratch}/index', GIT_WORK_TREE=scratch)
        git(SOURCE, 'read-tree', commit, env=env)
        drop = []
        for entry in git(SOURCE, 'ls-files', '-s', '-z', env=env).stdout.split(b'\0'):
            if entry:
                info, name = entry.split(b'\t', 1)
                if info.startswith(GITLINK) or excluded(name.decode('utf-8', 'surrogateescape')):
                    drop.append(name)
        if drop:
            git(SOURCE, 'update-index', '--force-remove', '-z', '--stdin', env=env, input=b'\0'.join(drop) + b'\0')
        return output(SOURCE, 'write-tree', env=env)


def publish(name, tree, parent):
    if parent:
        git(SOURCE, 'fetch', '-q', '--no-tags', str(CLEAN), f'+refs/heads/{name}:refs/clean/{name}')
    env = clean_env(**IDENTITY, GIT_AUTHOR_DATE=SNAPSHOT_DATE, GIT_COMMITTER_DATE=SNAPSHOT_DATE)
    commit = output(SOURCE, 'commit-tree', tree, *(['-p', parent] if parent else []), '-m', 'Снимок репозитория', env=env)
    git(SOURCE, 'update-ref', f'refs/export/{name}', commit)
    git(CLEAN, 'fetch', '-q', '--no-tags', str(SOURCE), f'+refs/export/{name}:refs/heads/{name}')
    return commit


def settled_tip(name, expected):
    for _ in range(50):
        tip = ref(CLEAN, f'refs/heads/{name}')
        if tip == expected:
            break
        time.sleep(0.1)
    return tip


def sync_branch(state, name, upstream, base):
    entry = state['branches'].get(name)
    tip = settled_tip(name, entry['clean']) if entry else ref(CLEAN, f'refs/heads/{name}')
    if entry and entry['upstream'] == upstream and entry['base'] == base and entry['clean'] == tip:
        return
    tree = sanitized_tree(base)
    pending = entry and entry['clean'] != tip and ref(CLEAN, entry['clean'])
    if (pending and (not tip or not git(CLEAN, 'merge-base', '--is-ancestor', tip, pending, check=False).returncode) and
            output(CLEAN, 'rev-parse', pending + '^{tree}') == tree):
        git(CLEAN, 'update-ref', f'refs/heads/{name}', pending)
        state['branches'][name] = {'upstream': upstream, 'base': base, 'clean': pending}
        return
    if tip and output(CLEAN, 'rev-parse', tip + '^{tree}') == tree:
        commit = tip
    else:
        commit = publish(name, tree, tip)
    state['branches'][name] = {'upstream': upstream, 'base': base, 'clean': commit}


def sync():
    config = load_config()
    with locked():
        state = load_state(config)
        prepare()
        branch = config['branch']
        head, write = fetch(config, branch)
        sync_branch(state, READ_BRANCH, head, head)
        if branch:
            entry = state['branches'].get(WORK_BRANCH)
            if write:
                sync_branch(state, WORK_BRANCH, write, write)
            elif not entry or entry['upstream'] is not None or settled_tip(WORK_BRANCH, entry['clean']) != entry['clean']:
                main = state['branches'][READ_BRANCH]['clean']
                git(CLEAN, 'update-ref', f'refs/heads/{WORK_BRANCH}', main)
                git(SOURCE, 'update-ref', 'refs/source/base', head)
                state['branches'][WORK_BRANCH] = {'upstream': None, 'base': head, 'clean': main}
        save_state(state)


def export(config, state, old, new):
    branch = config['branch']
    hook = dict(os.environ)
    entry = state['branches'].get(WORK_BRANCH)
    if not entry or entry['clean'] != old:
        raise Refused('Ветка изменилась. Выполните git pull --rebase и повторите push.')
    if git(CLEAN, 'merge-base', '--is-ancestor', old, new, env=hook, check=False).returncode:
        raise Refused('Разрешено только продолжение ветки без перезаписи истории.')
    commits = output(CLEAN, 'rev-list', '--reverse', f'{old}..{new}', env=hook).split()
    if not commits or len(commits) > MAX_COMMITS:
        raise Refused(f'Отправьте от 1 до {MAX_COMMITS} коммитов.')
    if output(CLEAN, 'rev-list', '--min-parents=2', f'{old}..{new}', env=hook):
        raise Refused('Коммиты слияния не принимаются. Используйте git pull --rebase.')
    url, env = remote_env(config)
    current = upstream_heads(config, branch)[1]
    if current != entry['upstream']:
        raise Refused('Ветка изменилась. Выполните git pull --rebase и повторите push.')
    tip = entry['base']
    for commit in commits:
        parent = output(CLEAN, 'rev-parse', commit + '^', env=hook)
        changes = git(CLEAN, 'diff-tree', '-r', '--no-renames', '-z', parent, commit, env=hook).stdout.split(b'\0')
        for info, name in zip(changes[0::2], changes[1::2]):
            modes = info.lstrip(b':').split(b' ')[:2]
            if GITLINK in modes or excluded(name.decode('utf-8', 'surrogateescape')):
                raise Refused('Изменение служебных файлов и подмодулей репозитория не разрешено.')
        patch = git(CLEAN, 'diff-tree', '-p', '--binary', '--full-index', '--no-renames', '--no-ext-diff',
                    '--no-textconv', parent, commit, env=hook).stdout
        message = git(CLEAN, 'log', '-1', '--format=%B', commit, env=hook).stdout
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as scratch:
            index = clean_env(GIT_INDEX_FILE=f'{scratch}/index', GIT_WORK_TREE=scratch)
            git(SOURCE, 'read-tree', tip, env=index)
            if patch and git(SOURCE, 'apply', '--cached', '--binary', env=index, input=patch, check=False).returncode:
                raise Refused('Изменения конфликтуют с веткой. Выполните git pull --rebase и повторите push.')
            tree = output(SOURCE, 'write-tree', env=index)
        tip = output(SOURCE, 'commit-tree', tree, '-p', tip, '-F', '-', env=clean_env(**IDENTITY), input=message)
    lease = f'--force-with-lease=refs/heads/{branch}:{current or ""}'
    if git(SOURCE, 'push', '-q', url, f'{tip}:refs/heads/{branch}', lease, env=env, check=False).returncode:
        raise Refused('Репозиторий не принял изменения. Выполните git pull --rebase и повторите push.')
    git(SOURCE, 'update-ref', 'refs/source/write', tip)
    state['branches'][WORK_BRANCH] = {'upstream': tip, 'base': tip, 'clean': new}


def receive():
    updates = [line.split() for line in sys.stdin.read().splitlines() if line.strip()]
    config = load_config()
    with locked():
        state = load_state(config, reset=False)
        branch = config['branch']
        if not branch or len(updates) != 1 or updates[0][2] != f'refs/heads/{WORK_BRANCH}':
            raise Refused(f'Запись разрешена только в ветку {WORK_BRANCH}, по одной ветке за push.')
        old, new = updates[0][:2]
        if new == ZERO or old == ZERO:
            raise Refused('Создание и удаление веток не разрешено.')
        export(config, state, old, new)
        save_state(state)


def main():
    commands = {'sync': sync, 'receive': receive}
    if len(sys.argv) != 2 or sys.argv[1] not in commands:
        sys.exit('Использование: git-mirror.py sync|receive')
    command = commands[sys.argv[1]]
    try:
        command()
    except Refused as refusal:
        print(f'agent-docker: {refusal}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('agent-docker: Git-шлюз не смог обработать запрос.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
