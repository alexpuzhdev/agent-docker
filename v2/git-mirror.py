import base64
import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
import re
import resource
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol, TypedDict

ROOT = Path(os.environ.get('GIT_MIRROR_ROOT', '/mirror')).resolve()
CONFIG = Path(os.environ.get('GIT_MIRROR_CONFIG', '/config/git/repository.json'))
UPSTREAM = Path(os.environ.get('GIT_MIRROR_UPSTREAM', '/config/upstream'))
REMOTE = os.environ.get('GIT_MIRROR_REMOTE', '')
HOST_DIRECT = os.environ.get('GIT_HOST_DIRECT') == '1'
if HOST_DIRECT:
    from host.git_audit import AuditRules, GitAuditRepository, MirrorAudit
MIRRORS = ROOT / 'mirrors'
LEGACY = (ROOT / 'clean.git', ROOT / 'source.git', ROOT / 'state.json')
CLEAN = SOURCE = STATE = None
PROVIDERS = {
    'github': {'field': 'repository', 'suffixes': ('.git',), 'username': 'x-access-token',
               'token': r'[a-zA-Z0-9_-]{10,512}',
               'repository': r'[a-zA-Z0-9][a-zA-Z0-9-]{0,38}/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}'},
    'gitlab': {'field': 'project', 'suffixes': ('.git', '.atom'), 'username': 'oauth2',
               'token': r'[a-zA-Z0-9_.-]{10,512}',
               'repository': r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}(?:/[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}){1,19}'},
}
GITHUB = 'https://github.com'
ORIGIN_PATTERN = (r'https://(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?'
                  r'(?::(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?(?:/[a-zA-Z0-9_~-][a-zA-Z0-9_.~-]*)*')
READ_BRANCH = 'main'
WORK_BRANCH = 'work'
LAYOUT = 3
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
READ_ALIAS = re.compile(r'[a-z][a-z0-9-]{0,31}')
MAX_FILTERED_BLOB = 2 * 1024 * 1024
PROJECTION_FILE = 'projection.sqlite3'
UPSTREAM_BRANCH = re.compile(r'(?:[a-zA-Z0-9][a-zA-Z0-9_-]*/)*[a-zA-Z0-9][a-zA-Z0-9_-]*')


class Refused(Exception):
    pass


class MirrorConfig(TypedDict):
    provider: str
    origin: str
    repository: str
    token: str
    branch: str | None
    readBranches: dict[str, str]


class BranchTip(TypedDict):
    upstream: str | None
    base: str
    clean: str


class MirrorState(TypedDict, total=False):
    key: dict[str, object]
    branches: dict[str, BranchTip]
    projectionPush: list[tuple[str, str]]


class ProjectionStore:
    """Соответствия объектов исходного репозитория и очищенной копии."""

    def __init__(self, database: sqlite3.Connection) -> None:
        self.database = database
        database.execute('CREATE TABLE IF NOT EXISTS projection (source TEXT PRIMARY KEY, clean TEXT NOT NULL)')
        database.execute('CREATE TABLE IF NOT EXISTS trees (source TEXT PRIMARY KEY, clean TEXT NOT NULL)')

    def known_commit(self, source: str) -> str | None:
        row = self.database.execute('SELECT clean FROM projection WHERE source = ?', (source,)).fetchone()
        return row[0] if row else None

    def known_tree(self, source: str) -> str | None:
        row = self.database.execute('SELECT clean FROM trees WHERE source = ?', (source,)).fetchone()
        return row[0] if row else None

    def remember_tree(self, source: str, clean: str) -> None:
        self.database.execute('INSERT INTO trees VALUES (?, ?)', (source, clean))

    def remember_commits(self, commits: list[tuple[str, str]]) -> None:
        self.database.executemany('INSERT OR REPLACE INTO projection VALUES (?, ?)', commits)


class CommitGraph(Protocol):
    @staticmethod
    def upstream_refs(url: str, env: dict[str, str], patterns: list[str]) -> dict[str, str]:
        ...

    def read(self, tips: list[str], known: list[str]) -> Generator[tuple[str, str, list[str]], None, None]:
        ...


class GitCommitGraph(CommitGraph):
    @staticmethod
    def upstream_refs(url: str, env: dict[str, str], patterns: list[str]) -> dict[str, str]:
        listing = {}
        for line in output(SOURCE, 'ls-remote', url, *patterns, env=env).splitlines():
            sha, name = line.split('\t', 1)
            listing[name] = sha
        return listing

    def read(self, tips: list[str], known: list[str]) -> Generator[tuple[str, str, list[str]], None, None]:
        args = ['git', '-C', str(SOURCE), 'log', '--topo-order', '--reverse', '--format=%H %T %P',
                *tips, '--not', *known]
        with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, env=clean_env()) as graph:
            try:
                assert graph.stdout is not None
                for line in graph.stdout:
                    commit, tree, *parents = line.split()
                    yield commit, tree, parents
            except BaseException:
                graph.kill()
                raise
            if graph.wait() != 0:
                raise RuntimeError('Не удалось получить историю разрешённых веток.')


@dataclass(frozen=True)
class ContentPolicy:
    excluded_paths: tuple[str, ...]
    replacements: tuple[tuple[bytes, bytes], ...]
    digest: str

    @classmethod
    def load(cls, path: Path) -> 'ContentPolicy':
        raw = path.read_bytes()
        rules = json.loads(raw)
        if not isinstance(rules, dict) or set(rules) - {'exclude', 'replace'}:
            raise ValueError('Некорректные правила фильтрации.')
        excluded_paths = rules.get('exclude', [])
        replacements = rules.get('replace', [])
        if not isinstance(excluded_paths, list) or any(
            not isinstance(pattern, str) or not pattern or len(pattern) > 1024 for pattern in excluded_paths
        ):
            raise ValueError('Поле exclude должно быть списком непустых шаблонов.')
        if not isinstance(replacements, list):
            raise ValueError('Поле replace должно быть списком замен.')
        pairs: list[tuple[bytes, bytes]] = []
        for rule in replacements:
            if not isinstance(rule, dict) or set(rule) != {'from', 'to'}:
                raise ValueError('Для каждой замены требуются from и to.')
            before, after = rule['from'], rule['to']
            if not isinstance(before, str) or not before or not isinstance(after, str):
                raise ValueError('Укажите непустую строку from и строку to.')
            pairs.append((before.encode(), after.encode()))
        return cls(tuple(excluded_paths), tuple(pairs), hashlib.sha256(raw).hexdigest())

    def excludes(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, pattern) for pattern in self.excluded_paths)

    def redact(self, data: bytes) -> bytes:
        result = data
        for before, after in self.replacements:
            growth = len(after) - len(before)
            if growth > 0 and len(result) + result.count(before) * growth > MAX_FILTERED_BLOB:
                raise Refused('Замена превышает предел размера файла.')
            result = result.replace(before, after)
        return result


class ContentFilter:
    def __init__(self, policy: ContentPolicy) -> None:
        self.policy = policy

    @lru_cache(maxsize=4096)
    def blob(self, oid: str) -> str:
        if not self.policy.replacements or int(output(SOURCE, 'cat-file', '-s', oid)) > MAX_FILTERED_BLOB:
            return oid
        original = git(SOURCE, 'cat-file', 'blob', oid).stdout
        if b'\0' in original:
            return oid
        filtered = self.policy.redact(original)
        if filtered == original:
            return oid
        return output(SOURCE, 'hash-object', '-w', '--stdin', input=filtered)


class HistoryProjection:
    """Общая очищенная история разрешённых веток."""

    @staticmethod
    def configured_branches(value: object) -> dict[str, str]:
        if not isinstance(value, dict):
            raise ValueError('Некорректная конфигурация веток чтения.')
        for alias, branch in value.items():
            if (not isinstance(alias, str) or not READ_ALIAS.fullmatch(alias) or alias in (READ_BRANCH, WORK_BRANCH)
                    or not isinstance(branch, str) or len(branch) > 120 or not UPSTREAM_BRANCH.fullmatch(branch)):
                raise ValueError('Некорректная конфигурация веток чтения.')
        return value

    def __init__(self, config: MirrorConfig, state: MirrorState, store: ProjectionStore,
                 graph: CommitGraph, content_filter: ContentFilter | None = None) -> None:
        self.content_filter = content_filter
        self.config = config
        self.state = state
        self.store = store
        self.graph = graph
        self.salt = hmac.digest(secret(), str(STATE.parent.name).encode(), 'sha256')
        self.identity = clean_env(**IDENTITY, GIT_AUTHOR_DATE=SNAPSHOT_DATE, GIT_COMMITTER_DATE=SNAPSHOT_DATE)

    def remote_tips(self) -> tuple[dict[str, str], str | None]:
        refs = {alias: f'refs/heads/{branch}' for alias, branch in self.config['readBranches'].items()}
        refs[READ_BRANCH] = 'HEAD'
        write_branch = self.config['branch']
        write_ref = f'refs/heads/{write_branch}' if write_branch else None
        url, env = remote_env(self.config)
        requested = list(refs.values()) + ([write_ref] if write_ref else [])
        remote = self.graph.upstream_refs(url, env, requested)
        if not all(name in remote for name in refs.values()):
            raise Refused('Одна из разрешённых веток чтения недоступна.')
        if write_ref and write_ref in remote:
            refs[WORK_BRANCH] = write_ref
        changed = {alias: name for alias, name in refs.items()
                   if ref(SOURCE, f'refs/source/{alias}') != remote[name]}
        if changed:
            refspecs = [f'+{name}:refs/source/{alias}' for alias, name in changed.items()]
            git(SOURCE, 'fetch', '-q', '--atomic', '--no-tags', url, *refspecs, env=env)
        tips = {alias: remote[name] for alias, name in refs.items()}
        if not all(ref(SOURCE, f'refs/source/{alias}') == value for alias, value in tips.items()):
            raise Refused('Ветка изменилась при обновлении. Повторите git fetch.')
        return tips, tips.get(WORK_BRANCH)

    def finish_push(self) -> None:
        pending = self.state.pop('projectionPush', [])
        if not pending:
            return
        expected = pending[-1][1]
        if settled_tip(WORK_BRANCH, expected) == expected:
            git(SOURCE, 'fetch', '-q', '--no-tags', str(CLEAN), '+refs/heads/work:refs/clean/work')
            self.store.remember_commits(pending)

    def populate(self, tips: dict[str, str]) -> None:
        if all(self.store.known_commit(tip) for tip in tips.values()):
            return
        previous = {entry['upstream'] for entry in self.state['branches'].values() if entry['upstream']}
        protected = sorted(value for value in previous if self.store.known_commit(value) and ref(SOURCE, value))
        commits: dict[str, str] = {}
        with closing(self.graph.read(sorted(set(tips.values())), protected)) as graph:
            for raw, tree, parents in graph:
                if raw in commits or self.store.known_commit(raw):
                    continue
                clean_tree = self.store.known_tree(tree)
                if clean_tree is None:
                    clean_tree = sanitized_tree(raw, self.content_filter)
                    self.store.remember_tree(tree, clean_tree)
                translated = [commits.get(parent) or self.store.known_commit(parent) for parent in parents]
                if any(parent is None for parent in translated):
                    raise RuntimeError('История разрешённых веток обновилась не полностью.')
                arguments = [value for parent in translated if parent is not None for value in ('-p', parent)]
                label = hmac.digest(self.salt, raw.encode(), 'sha256').hex()[:20]
                commits[raw] = output(SOURCE, 'commit-tree', clean_tree, *arguments, '-m',
                                      f'Изменение {label}', env=self.identity)
                if len(commits) >= 1024:
                    self.store.remember_commits(list(commits.items()))
                    commits.clear()
        self.store.remember_commits(list(commits.items()))

    def sync(self) -> None:
        tips, write = self.remote_tips()
        self.finish_push()
        self.populate(tips)
        if self.config['branch'] and not write:
            tips[WORK_BRANCH] = tips[READ_BRANCH]
        branches = self.state['branches']
        changed = {}
        for alias, upstream in tips.items():
            clean = self.store.known_commit(upstream)
            if not clean:
                raise RuntimeError('Не удалось построить историю разрешённой ветки.')
            tip = ref(CLEAN, f'refs/heads/{alias}')
            if clean != tip:
                changed[alias] = clean
            origin = write if alias == WORK_BRANCH else upstream
            branches[alias] = {'upstream': origin, 'base': upstream, 'clean': clean}
        if changed:
            updates = [f'update refs/export/{alias} {clean}\n' for alias, clean in changed.items()]
            git(SOURCE, 'update-ref', '--stdin', input=''.join(updates).encode())
            refspecs = [f'+refs/export/{alias}:refs/heads/{alias}' for alias in changed]
            git(CLEAN, 'fetch', '-q', '--atomic', '--no-tags', str(SOURCE), *refspecs)


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


def github_host(origin):
    host = origin.removeprefix('https://').split('/', 1)[0].split(':', 1)[0]
    return host == 'github.com' or host.endswith('.github.com')


def load_config() -> MirrorConfig:
    config = json.loads(CONFIG.read_text())
    if not isinstance(config, dict):
        raise ValueError('Некорректная конфигурация Git-шлюза.')
    token, write = config.get('token'), config.get('write')
    provider = config.get('provider', 'github')
    origin = config.get('origin', GITHUB if provider == 'github' else None)
    rules = PROVIDERS.get(provider) if isinstance(provider, str) else None
    repository = config.get(rules['field']) if rules else None
    if (not rules or any(field in config for field in ('repository', 'project') if field != rules['field']) or
            not isinstance(repository, str) or repository.endswith(rules['suffixes']) or
            len(repository) > 1024 or not re.fullmatch(rules['repository'], repository) or
            not isinstance(token, str) or not re.fullmatch(rules['token'], token) or
            not isinstance(write, bool) or not isinstance(origin, str) or not re.fullmatch(ORIGIN_PATTERN, origin) or
            len(origin) > 512 or (provider == 'github') != (origin == GITHUB) or
            (provider != 'github' and github_host(origin))):
        raise ValueError('Некорректная конфигурация Git-шлюза.')
    branch = config.get('writeBranch')
    writable = (write and isinstance(branch, str) and len(branch) <= 120 and branch not in ('main', 'master') and
                UPSTREAM_BRANCH.fullmatch(branch))
    return {'provider': provider, 'origin': origin, 'repository': repository, 'token': token,
            'branch': branch if writable else None,
            'readBranches': HistoryProjection.configured_branches(config.get('readBranches', {}))}


def remote_env(config):
    if REMOTE:
        return REMOTE, clean_env()
    username = PROVIDERS[config['provider']]['username']
    credentials = base64.b64encode(f'{username}:{config["token"]}'.encode()).decode()
    proxy = '' if HOST_DIRECT else UPSTREAM.read_text().strip()
    options = [('http.proxy', proxy), ('http.followRedirects', 'false'),
               ('http.extraHeader', f'Authorization: Basic {credentials}'), ('credential.helper', '')]
    env = clean_env(GIT_CONFIG_COUNT=str(len(options)))
    if HOST_DIRECT:
        env = {key: value for key, value in env.items() if not key.lower().endswith('_proxy')}
    for index, (key, value) in enumerate(options):
        env[f'GIT_CONFIG_KEY_{index}'] = key
        env[f'GIT_CONFIG_VALUE_{index}'] = value
    return f'{config["origin"]}/{config["repository"]}.git', env


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / 'lock', 'a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def secret():
    path = ROOT / 'secret'
    try:
        value = path.read_bytes()
    except FileNotFoundError:
        value = b''
    if len(value) == 32:
        return value
    value = os.urandom(32)
    descriptor, draft = tempfile.mkstemp(dir=ROOT, prefix='secret-')
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(draft, path)
    return value


def select(config, policy: ContentPolicy | None = None):
    global CLEAN, SOURCE, STATE
    key = {'layout': LAYOUT, 'repository': config['repository'], 'writeBranch': config['branch']}
    if policy is not None:
        key['contentPolicy'] = policy.digest
    if config['readBranches']:
        key['readBranches'] = config['readBranches']
    if config['provider'] != 'github':
        key.update(provider=config['provider'], origin=config['origin'])
    name = hmac.new(secret(), json.dumps(key, sort_keys=True).encode(), hashlib.sha256).hexdigest()
    home = MIRRORS / name
    CLEAN, SOURCE, STATE = home / 'clean.git', home / 'source.git', home / 'state.json'
    return key, home


def remove(path):
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.is_symlink() or path.exists():
        path.unlink()


def retire(home):
    scratch = ROOT / 'tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    stale = [path for path in LEGACY if path.is_symlink() or path.exists()]
    stale += [path for path in ROOT.glob('tmp?*') if path.is_file()]
    if MIRRORS.is_dir():
        stale += [other for other in MIRRORS.iterdir() if other != home]
    for path in stale:
        try:
            os.rename(path, scratch / f'retired-{os.urandom(8).hex()}')
        except OSError:
            remove(path)
    for path in scratch.glob('retired-*'):
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def saved_state(key):
    try:
        state = json.loads(STATE.read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) and state.get('key') == key and CLEAN.is_dir() and SOURCE.is_dir() else None


def save_state(state):
    with tempfile.NamedTemporaryFile('w', dir=STATE.parent, delete=False) as draft:
        json.dump(state, draft)
    os.replace(draft.name, STATE)


def load_state(key):
    state = saved_state(key)
    if state is None:
        for path in (CLEAN, SOURCE, STATE, STATE.parent / PROJECTION_FILE,
                     STATE.parent / f'{PROJECTION_FILE}-journal'):
            remove(path)
        state = {'key': key, 'branches': {}}
    return state


def prepare(keep_history: bool) -> None:
    (ROOT / 'tmp').mkdir(parents=True, exist_ok=True)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if not SOURCE.exists():
        git(ROOT, 'init', '-q', '--bare', str(SOURCE))
        if keep_history:
            # База соответствий хранит ссылки и на коммиты до переписывания веток.
            git(SOURCE, 'config', 'gc.auto', '0')
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
    listing = GitCommitGraph.upstream_refs(url, env, patterns)
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


def sanitized_tree(commit, content_filter: ContentFilter | None = None):
    with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as scratch:
        env = clean_env(GIT_INDEX_FILE=f'{scratch}/index', GIT_WORK_TREE=scratch)
        git(SOURCE, 'read-tree', commit, env=env)
        drop = []
        replacements = []
        for entry in git(SOURCE, 'ls-files', '-s', '-z', env=env).stdout.split(b'\0'):
            if not entry:
                continue
            info, name = entry.split(b'\t', 1)
            path = name.decode('utf-8', 'surrogateescape')
            if info.startswith(GITLINK) or excluded(path) or (content_filter and content_filter.policy.excludes(path)):
                drop.append(name)
                continue
            if content_filter is not None:
                mode, oid, _ = info.split()
                clean = content_filter.blob(oid.decode()).encode()
                if clean != oid:
                    replacements.append(mode + b' ' + clean + b'\t' + name + b'\0')
        if drop:
            git(SOURCE, 'update-index', '--force-remove', '-z', '--stdin', env=env, input=b'\0'.join(drop) + b'\0')
        if replacements:
            git(SOURCE, 'update-index', '-z', '--index-info', env=env, input=b''.join(replacements))
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


def sync_branch(state, name, upstream, base, content_filter: ContentFilter | None = None):
    entry = state['branches'].get(name)
    tip = settled_tip(name, entry['clean']) if entry else ref(CLEAN, f'refs/heads/{name}')
    if entry and entry['upstream'] == upstream and entry['base'] == base and entry['clean'] == tip:
        return
    tree = sanitized_tree(base, content_filter)
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
    with locked():
        config = load_config()
        policy_path = os.environ.get('GIT_HOST_CONTENT_POLICY')
        if policy_path and not HOST_DIRECT:
            raise Refused('Фильтр содержимого доступен только для зеркала хоста.')
        policy = ContentPolicy.load(Path(policy_path)) if policy_path else None
        content_filter = ContentFilter(policy) if policy else None
        key, home = select(config, policy)
        retire(home)
        state = load_state(key)
        keep_history = bool(config['readBranches'])
        prepare(keep_history)
        if keep_history:
            with closing(sqlite3.connect(STATE.parent / PROJECTION_FILE)) as database, database:
                HistoryProjection(config, state, ProjectionStore(database), GitCommitGraph(), content_filter).sync()
            save_state(state)
            print(home.relative_to(ROOT))
            return
        branch = config['branch']
        head, write = fetch(config, branch)
        sync_branch(state, READ_BRANCH, head, head, content_filter)
        if branch:
            entry = state['branches'].get(WORK_BRANCH)
            if write:
                sync_branch(state, WORK_BRANCH, write, write, content_filter)
            elif (not entry or entry['upstream'] is not None or entry['base'] != head or
                  settled_tip(WORK_BRANCH, entry['clean']) != entry['clean']):
                main = state['branches'][READ_BRANCH]['clean']
                git(CLEAN, 'update-ref', f'refs/heads/{WORK_BRANCH}', main)
                git(SOURCE, 'update-ref', 'refs/source/base', head)
                state['branches'][WORK_BRANCH] = {'upstream': None, 'base': head, 'clean': main}
        save_state(state)
        print(home.relative_to(ROOT))


def resolve():
    with locked():
        key, home = select(load_config())
        retire(home)
        if saved_state(key) is None:
            raise Refused('Зеркало текущего назначения ещё не готово.')
        print(home.relative_to(ROOT))


def export(config, state, old, new, *, incoming: dict[str, str] | None = None,
           content_filter: ContentFilter | None = None):
    branch = config['branch']
    hook = incoming if incoming is not None else dict(os.environ)
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
    head, current = upstream_heads(config, branch)
    if incoming is None and current != entry['upstream']:
        raise Refused('Ветка изменилась. Выполните git pull --rebase и повторите push.')
    if current is None and head != entry['base']:
        raise Refused('Основная ветка обновилась. Выполните git pull --rebase и повторите push.')
    tip = entry['base']
    projected = []
    protected_paths: set[bytes] = set()
    if content_filter is not None:
        visible_tree = sanitized_tree(tip, content_filter)
        protected_paths = set(git(
            SOURCE, 'diff-tree', '--name-only', '-r', '--no-renames', '-z', tip + '^{tree}', visible_tree,
        ).stdout.split(b'\0'))
    identity = clean_env(**IDENTITY, GIT_AUTHOR_DATE=SNAPSHOT_DATE, GIT_COMMITTER_DATE=SNAPSHOT_DATE) \
        if incoming is not None else clean_env(**IDENTITY)
    for commit in commits:
        parent = output(CLEAN, 'rev-parse', commit + '^', env=hook)
        changes = git(CLEAN, 'diff-tree', '-r', '--no-renames', '-z', parent, commit, env=hook).stdout.split(b'\0')
        for info, name in zip(changes[0::2], changes[1::2]):
            modes = info.lstrip(b':').split(b' ')[:2]
            path = name.decode('utf8', errors='surrogateescape')
            filtered = name in protected_paths or (content_filter is not None and content_filter.policy.excludes(path))
            if GITLINK in modes or excluded(path):
                raise Refused('Изменение служебных файлов и подмодулей репозитория не разрешено.')
            if filtered:
                raise Refused('Изменение отфильтрованных файлов не разрешено.')
        patch = git(CLEAN, 'diff-tree', '-p', '--binary', '--full-index', '--no-renames', '--no-ext-diff',
                    '--no-textconv', parent, commit, env=hook).stdout
        message = git(CLEAN, 'log', '-1', '--format=%B', commit, env=hook).stdout
        with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as scratch:
            index = clean_env(GIT_INDEX_FILE=f'{scratch}/index', GIT_WORK_TREE=scratch)
            git(SOURCE, 'read-tree', tip, env=index)
            if patch and git(SOURCE, 'apply', '--cached', '--binary', env=index, input=patch, check=False).returncode:
                raise Refused('Изменения конфликтуют с веткой. Выполните git pull --rebase и повторите push.')
            tree = output(SOURCE, 'write-tree', env=index)
        tip = output(SOURCE, 'commit-tree', tree, '-p', tip, '-F', '-', env=identity, input=message)
        projected.append((tip, commit))
    already_sent = incoming is not None and current == tip
    if not already_sent:
        if current != entry['upstream']:
            raise Refused('Ветка изменилась. Выполните git pull --rebase и повторите push.')
        lease = f'--force-with-lease=refs/heads/{branch}:{current or ""}'
        if git(SOURCE, 'push', '-q', url, f'{tip}:refs/heads/{branch}', lease, env=env, check=False).returncode:
            raise Refused('Репозиторий не принял изменения. Выполните git pull --rebase и повторите push.')
    raw_ref = 'refs/source/work' if config['readBranches'] else 'refs/source/write'
    git(SOURCE, 'update-ref', raw_ref, tip)
    state['branches'][WORK_BRANCH] = {'upstream': tip, 'base': tip, 'clean': new}
    if config['readBranches']:
        state['projectionPush'] = projected


def receive():
    updates = [line.split() for line in sys.stdin.read().splitlines() if line.strip()]
    with locked():
        config = load_config()
        key, _ = select(config)
        state = saved_state(key)
        if state is None or Path(os.environ.get('GIT_DIR', '.')).resolve() != CLEAN.resolve():
            raise Refused('Назначение репозитория изменилось. Выполните git fetch и повторите push.')
        branch = config['branch']
        if not branch or len(updates) != 1 or updates[0][2] != f'refs/heads/{WORK_BRANCH}':
            raise Refused(f'Запись разрешена только в ветку {WORK_BRANCH}, по одной ветке за push.')
        old, new = updates[0][:2]
        if new == ZERO or old == ZERO:
            raise Refused('Создание и удаление веток не разрешено.')
        export(config, state, old, new)
        save_state(state)


class HostPush:
    @staticmethod
    def receive(old: str, new: str, pack: Path) -> None:
        if not HOST_DIRECT or not all(re.fullmatch(r'[a-f0-9]{40}', oid) and oid != ZERO for oid in (old, new)):
            raise Refused('Некорректный запрос публикации.')
        with locked():
            config = load_config()
            policy_path = os.environ.get('GIT_HOST_CONTENT_POLICY')
            policy = ContentPolicy.load(Path(policy_path)) if policy_path else None
            key, _ = select(config, policy)
            state = saved_state(key)
            if state is None or not config['branch']:
                raise Refused('Зеркало ожидает настройки ветки записи и синхронизации.')
            HostPush.verify_target(config)
            entry = state['branches'].get(WORK_BRANCH)
            if entry and entry['clean'] == new and ref(CLEAN, 'refs/heads/work') == new:
                _, remote = upstream_heads(config, config['branch'])
                if remote == entry['upstream']:
                    return
            with tempfile.TemporaryDirectory(dir=ROOT / 'tmp') as temporary:
                repo = Path(temporary) / 'incoming.git'
                git(ROOT, 'init', '-q', '--bare', '--template=', str(repo))
                alternates = repo / 'objects/info/alternates'
                alternates.write_text(str(CLEAN / 'objects') + '\n')
                with pack.open('rb') as data:
                    result = subprocess.run(
                        ['git', '-C', str(repo), 'index-pack', '--stdin', '--strict', '--threads=1'],
                        stdin=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        timeout=120, env=clean_env(), preexec_fn=HostPush.limit_import,
                    )
                if result.returncode:
                    raise Refused('Не удалось проверить полученные объекты.')
                git(repo, 'update-ref', 'refs/heads/incoming', new)
                HostPush.audit(repo, old, new)
                hook_env = clean_env(GIT_OBJECT_DIRECTORY=str(repo / 'objects'))
                content_filter = ContentFilter(policy) if policy else None
                export(config, state, old, new, incoming=hook_env, content_filter=content_filter)
                git(CLEAN, 'fetch', '-q', '--no-tags', '--no-write-fetch-head',
                    str(repo), 'refs/heads/incoming:refs/heads/work')
                save_state(state)

    @staticmethod
    def verify_target(config: MirrorConfig) -> None:
        url, env = remote_env(config)
        symbolic = output(SOURCE, 'ls-remote', '--symref', url, 'HEAD', env=env)
        match = re.search(r'^ref: (refs/heads/[^\t\n]+)\tHEAD$', symbolic, re.MULTILINE)
        if not match or match[1] == f"refs/heads/{config['branch']}":
            raise Refused('Публикация в основную ветку запрещена или основная ветка недоступна.')

    @staticmethod
    def limit_import() -> None:
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))

    @staticmethod
    def audit(repo: Path, old: str, new: str) -> None:
        rule_path = os.environ.get('GIT_HOST_AUDIT_RULES')
        saved = json.loads(Path(rule_path).read_text()) if rule_path else {}
        rules = AuditRules.load(saved, json.loads(CONFIG.read_text()))
        strict = AuditRules(rules.domains, rules.markers, (), rules.assigned_token)
        commits = output(repo, 'rev-list', '--max-count=' + str(MAX_COMMITS + 1), f'{old}..{new}').split()
        if not 1 <= len(commits) <= MAX_COMMITS:
            raise Refused(f'Отправьте от 1 до {MAX_COMMITS} коммитов.')
        for commit in commits:
            if int(output(repo, 'cat-file', '-s', commit)) > MAX_FILTERED_BLOB:
                raise Refused('Метаданные одного из коммитов слишком велики.')
            metadata = output(repo, 'cat-file', 'commit', commit)
            if strict.matches(metadata):
                raise Refused('Метаданные одного из коммитов не прошли проверку публикации.')
        result = MirrorAudit(GitAuditRepository(repo, clean_env(), f'{old}..{new}'), strict).run()
        if result['skipped'] or any(item['rule'] != 'url-credentials-template' for item in result['findings']):
            raise Refused('Полученные изменения не прошли проверку публикации.')


def main():
    commands = {'sync': sync, 'resolve': resolve, 'receive': receive}
    try:
        if len(sys.argv) == 2 and sys.argv[1] == 'host-check-write':
            if not load_config()['branch']:
                raise Refused('Для публикации укажите разрешённую ветку записи.')
        elif len(sys.argv) == 5 and sys.argv[1] == 'host-receive':
            HostPush.receive(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
        elif len(sys.argv) == 2 and sys.argv[1] in commands:
            commands[sys.argv[1]]()
        else:
            sys.exit('Использование: git-mirror.py sync|resolve|receive|host-receive')
    except Refused as refusal:
        print(f'agent-docker: {refusal}', file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('agent-docker: Git-шлюз не смог обработать запрос.', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
