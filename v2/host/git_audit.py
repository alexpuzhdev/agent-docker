import argparse
import fnmatch
import json
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Generator
from contextlib import closing
from dataclasses import dataclass, field
from itertools import islice
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


MAX_BLOB_BYTES = 2 * 1024 * 1024
HEX_ID = re.compile(rb'[a-f0-9]{40,64}')
SECRET_PATTERNS = {
    'access-token': re.compile(
        r'\b(?:glpat-[\w.-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'
        r'|sk-ant-[\w-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16})\b'
    ),
    'private-key': re.compile(r'-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----'),
}

URL_CREDENTIALS = re.compile(r"""https?://[^\s/:"']+:(?P<password>[^\s/"']+?)@""", re.IGNORECASE)
ENV_REFERENCE = re.compile(r'\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})')


@dataclass(frozen=True)
class Revision:
    path: str
    commit: str


class AuditRepository(Protocol):
    def heads(self) -> dict[str, str]:
        ...

    def authors(self) -> list[str]:
        ...

    def versions(self) -> tuple[int, dict[str, list[Revision]]]:
        ...

    def blobs(self, objects: list[str]) -> Generator[tuple[str, bytes | None], None, None]:
        ...


class GitAuditRepository(AuditRepository):
    def __init__(self, path: Path, env: dict[str, str], revisions: str | None = None) -> None:
        self.revisions = revisions or '--all'
        self.command = ['git', '-C', str(path)]
        self.env = env

    def heads(self) -> dict[str, str]:
        data = self.output('for-each-ref', '--format=%(refname:short) %(objectname)', 'refs/heads')
        return dict(line.split(' ', 1) for line in data.decode().splitlines())

    def authors(self) -> list[str]:
        with closing(self.records('log', '--all', '-z', '--format=format:%an <%ae>')) as authors:
            return sorted({author.decode(errors='replace') for author in authors if author})

    def versions(self) -> tuple[int, dict[str, list[Revision]]]:
        args = ('log', self.revisions, '--topo-order', '--reverse', '--format=%x00commit:%H',
                '--raw', '--root', '-m', '-r', '--no-renames', '--no-abbrev', '-z')
        found: dict[str, list[Revision]] = {}
        paths_seen: set[tuple[str, bytes]] = set()
        commits: set[str] = set()
        commit = ''
        with closing(self.records(*args)) as records:
            for record in records:
                record = record.lstrip(b'\n')
                if record.startswith(b'commit:'):
                    commit = record[7:].decode()
                    commits.add(commit)
                elif record.startswith(b':'):
                    _, mode, _, blob, _ = record.split()
                    path = next(records)
                    if mode not in (b'100644', b'100755', b'120000') or not HEX_ID.fullmatch(blob):
                        continue
                    oid = blob.decode()
                    key = (oid, path)
                    if key not in paths_seen:
                        paths_seen.add(key)
                        revision = Revision(path.decode('utf8', errors='backslashreplace'), commit)
                        found.setdefault(oid, []).append(revision)
        return len(commits), found

    def records(self, *args: str) -> Generator[bytes, None, None]:
        with subprocess.Popen([*self.command, *args], stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=self.env) as pipe:
            trailing = b''
            try:
                assert pipe.stdout is not None
                while chunk := pipe.stdout.read1(65536):
                    *records, trailing = (trailing + chunk).split(b'\0')
                    yield from records
                if trailing:
                    yield trailing
            except BaseException:
                pipe.kill()
                raise
            if pipe.wait() != 0:
                raise ValueError('Не удалось прочитать историю Git.')

    def object_headers(self, objects: list[str]) -> Generator[tuple[str, str], None, None]:
        remaining = iter(objects)
        while names := list(islice(remaining, 512)):
            queries = ''.join(oid + '\n' for oid in names).encode()
            summary = self.output('cat-file', '--batch-check', data=queries).decode().splitlines()
            if len(summary) != len(names):
                raise ValueError('Git вернул неполный список объектов.')
            yield from zip(names, summary)

    def blobs(self, objects: list[str]) -> Generator[tuple[str, bytes | None], None, None]:
        with subprocess.Popen(
            [*self.command, 'cat-file', '--batch'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=self.env,
        ) as pipe:
            try:
                assert pipe.stdin is not None and pipe.stdout is not None
                for oid, header in self.object_headers(objects):
                    actual, kind, size = header.split()
                    if actual != oid or kind != 'blob':
                        raise ValueError('Git вернул неожиданный объект.')
                    if int(size) > MAX_BLOB_BYTES:
                        yield oid, None
                        continue
                    pipe.stdin.write((oid + '\n').encode())
                    pipe.stdin.flush()
                    if pipe.stdout.readline(160) != header.encode() + b'\n':
                        raise ValueError('Ответ Git по объекту изменился.')
                    data = pipe.stdout.read(int(size))
                    if len(data) != int(size) or pipe.stdout.read(1) != b'\n':
                        raise ValueError('Git вернул неполный объект.')
                    yield oid, None if b'\0' in data else data
            except BaseException:
                pipe.kill()
                raise
            pipe.stdin.close()
            if pipe.wait() != 0:
                raise ValueError('Не удалось прочитать объекты Git.')

    def output(self, *args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(
            [*self.command, *args], input=data, capture_output=True, timeout=120,
            check=True, env=self.env,
        ).stdout


@dataclass(frozen=True)
class AuditRules:
    domains: tuple[str, ...]
    markers: tuple[str, ...]
    ignored_paths: tuple[str, ...]
    assigned_token: str = field(repr=False)

    @classmethod
    def load(cls, saved: object, settings: object) -> 'AuditRules':
        if not isinstance(saved, dict) or set(saved) - {'domains', 'markers', 'ignored_paths'}:
            raise ValueError('В правилах разрешены domains, markers и ignored_paths.')
        if not isinstance(settings, dict):
            raise ValueError('Некорректная настройка назначения репозитория.')
        values: dict[str, list[str]] = {}
        for key in ('domains', 'markers', 'ignored_paths'):
            items = saved.get(key, [])
            if not isinstance(items, list) or any(
                not isinstance(item, str) or not item.strip() or len(item) > 1024 for item in items
            ):
                raise ValueError(f'Поле {key} должно быть списком непустых строк.')
            values[key] = items
        origin = settings.get('origin', 'https://github.com')
        if isinstance(origin, str) and (host := urlsplit(origin).hostname):
            values['domains'].append(host)
        project = settings.get('project', settings.get('repository'))
        if isinstance(project, str) and '/' in project:
            values['markers'].append(project)
        token = settings.get('token')
        return cls(
            tuple(sorted(set(item.casefold() for item in values['domains']))),
            tuple(sorted(set(item.casefold() for item in values['markers']))),
            tuple(values['ignored_paths']),
            token if isinstance(token, str) and len(token) >= 10 else '',
        )

    def matches(self, text: str) -> list[str]:
        folded = text.casefold()
        result: list[str] = []
        for name, entries in (('domain', self.domains), ('marker', self.markers)):
            if any(entry in folded for entry in entries):
                result.append(name)
        result.extend(name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text))
        if self.assigned_token and self.assigned_token in text:
            result.append('assigned-token')
        for match in URL_CREDENTIALS.finditer(text):
            rule = 'url-credentials-template' if ENV_REFERENCE.fullmatch(match['password']) else 'url-credentials'
            if rule not in result:
                result.append(rule)
        return result

    def safe_label(self, text: str) -> str:
        if self.assigned_token and self.assigned_token in text:
            return '[скрыто]'
        if URL_CREDENTIALS.search(text) or any(pattern.search(text) for pattern in SECRET_PATTERNS.values()):
            return '[скрыто]'
        return ''.join(char if char.isprintable() else '?' for char in text)



class MirrorAudit:
    def __init__(self, git: AuditRepository, rules: AuditRules) -> None:
        self.git = git
        self.rules = rules

    def run(self) -> dict[str, object]:
        commits, versions = self.git.versions()
        findings: list[dict[str, object]] = []
        skipped: list[dict[str, str]] = []
        visible = {
            oid: [item for item in locations if not any(
                fnmatch.fnmatchcase(item.path, mask) for mask in self.rules.ignored_paths
            )]
            for oid, locations in versions.items()
        }
        with closing(self.git.blobs([oid for oid, paths in visible.items() if paths])) as blobs:
            for oid, data in blobs:
                line_matches = []
                if data is not None:
                    for number, line in enumerate(data.decode('utf8', errors='replace').splitlines(), 1):
                        line_matches.extend((number, rule) for rule in self.rules.matches(line))
                for revision in visible[oid]:
                    path = self.rules.safe_label(revision.path)
                    position = {'path': path, 'commit': revision.commit}
                    findings.extend({**position, 'line': 0, 'rule': rule}
                                    for rule in self.rules.matches(revision.path))
                    if data is None:
                        skipped.append(position)
                    findings.extend({**position, 'line': number, 'rule': rule} for number, rule in line_matches)
        heads = {}
        for index, (name, oid) in enumerate(self.git.heads().items(), 1):
            shown = self.rules.safe_label(name)
            heads[f'{shown} ({index})' if shown == '[скрыто]' else shown] = oid
        return {
            'heads': heads,
            'authors': [self.rules.safe_label(author) for author in self.git.authors()],
            'commits': commits,
            'findings': findings,
            'skipped': skipped,
            'ignored': sum(len(paths) - len(visible[oid]) for oid, paths in versions.items()),
        }



class HostAuditCommand:
    def __init__(self, public: Path, settings: Path, rules: Path, report: Path) -> None:
        self.public = public
        self.settings = settings
        self.rules = rules
        self.report = report

    def input(self, candidate: Path | None) -> tuple[str, Path, AuditRules]:
        generation = candidate.parent.name if candidate else (self.public / 'current').read_text().strip()
        if not re.fullmatch(r'view-[a-zA-Z0-9]{10}', generation):
            raise ValueError('Не найдена опубликованная версия зеркала.')
        self.rules.parent.mkdir(parents=True, exist_ok=True)
        if not self.rules.exists():
            with self.rules.open('x', encoding='utf8') as output:
                json.dump({'domains': [], 'markers': [], 'ignored_paths': []}, output, indent=2)
                output.write('\n')
        saved = json.loads(self.rules.read_text())
        rules = AuditRules.load(saved, json.loads(self.settings.read_text()))
        if candidate is not None:
            rules = AuditRules(rules.domains, rules.markers, (), rules.assigned_token)
        return generation, candidate or (self.public / generation / 'clean.git'), rules

    def write(self, generation: str, result: dict[str, object]) -> None:
        result['generation'] = generation
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf8', dir=self.report.parent, delete=False,
        ) as draft:
            json.dump(result, draft, ensure_ascii=False, indent=2)
            draft.write('\n')
        os.replace(draft.name, self.report)
        keys = Counter((row['path'], row['rule']) for row in result['findings'])
        print('Ветки:', ', '.join(result['heads']))
        print(f"Авторов в доступной истории: {len(result['authors'])}.")
        print(f"Проверено коммитов: {result['commits']}. Находок: {len(result['findings'])}. "
              f"Пропущено версий файлов: {len(result['skipped'])}. Исключено правилами: {result['ignored']}.")
        for (path, rule), count in keys.most_common(12):
            print(f'{path}  {rule}  {count}')
        print(f'Полный отчёт: {self.report}')
        print(f'Правила: {self.rules}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Проверить опубликованное Git-зеркало.')
    for argument in ('public', 'settings', 'rules', 'report'):
        parser.add_argument(argument, type=Path)
    parser.add_argument('--candidate', type=Path)
    arguments = parser.parse_args()
    try:
        command = HostAuditCommand(arguments.public, arguments.settings, arguments.rules, arguments.report)
        generation, mirror, rules = command.input(arguments.candidate)
        env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
        env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_LAZY_FETCH='1')
        result = MirrorAudit(GitAuditRepository(mirror, env), rules).run()
        command.write(generation, result)
        if arguments.candidate and (result['skipped'] or any(
            item['rule'] != 'url-credentials-template' for item in result['findings']
        )):
            parser.exit(2, 'В копии остались запрещённые данные или непроверенные файлы. Публикация отклонена.\n')
    except (OSError, ValueError, subprocess.SubprocessError):
        parser.exit(1, 'Не удалось проверить зеркало. Проверьте состояние публикации и формат правил.\n')
