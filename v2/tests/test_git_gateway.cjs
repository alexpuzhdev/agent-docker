const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { promisify } = require('node:util');

const run = promisify(execFile);
const token = 'fixture_token_only';
const owner = ['-c', 'user.name=Ivan Petrov', '-c', 'user.email=ivan.petrov@corp.example'];
const guest = ['-c', 'user.name=Guest Agent', '-c', 'user.email=guest@vm.invalid'];

function listen(server) {
    return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

test('guest sees an anonymous snapshot and pushes only to the assigned branch', async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-git-test-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const config = path.join(root, 'repository.json');
    const upstream = path.join(root, 'upstream.git');
    const seed = path.join(root, 'seed');
    const copy = path.join(root, 'clone');
    process.env.GIT_CONFIG_NOSYSTEM = '1';
    process.env.GIT_CONFIG_GLOBAL = '/dev/null';
    process.env.GIT_MIRROR_ROOT = path.join(root, 'mirror');
    process.env.GIT_MIRROR_CONFIG = config;
    process.env.GIT_MIRROR_REMOTE = 'file://' + upstream;
    const git = (...args) => run('git', args);

    await git('init', '--bare', '--initial-branch=main', upstream);
    await git('init', '--initial-branch=main', seed);
    await fs.mkdir(path.join(seed, '.github'));
    await fs.writeFile(path.join(seed, '.github', 'CODEOWNERS'), '* @ivan-petrov\n');
    await fs.writeFile(path.join(seed, 'app.txt'), 'owner\n');
    await git('-C', seed, 'add', '-A');
    await git('-C', seed, ...owner, 'commit', '-m', 'Merge pull request #7 from ivan-petrov/feature');
    await git('-C', seed, 'push', upstream, 'main');

    const { createGitHandler } = require('../git-gateway.cjs');
    const gateway = http.createServer(createGitHandler(config, process.env.GIT_MIRROR_ROOT));
    t.after(() => gateway.close());
    const base = 'http://127.0.0.1:' + await listen(gateway);
    const remote = base + '/project.git';
    const updateConfig = (write, writeBranch) => fs.writeFile(config, JSON.stringify({
        repository: 'owner/source', write, token, writeBranch,
    }));

    assert.equal((await fetch(remote + '/info/refs?service=git-upload-pack')).status, 404);
    await updateConfig(false);
    assert.equal((await fetch(base + '/alien.git/info/refs?service=git-upload-pack')).status, 404);
    await git('clone', remote, copy);
    const log = await git('-C', copy, 'log', '--format=%an <%ae>|%s');
    assert.equal(log.stdout, 'agent-docker <agent-docker@localhost>|Снимок репозитория\n');
    assert.deepEqual((await git('-C', copy, 'ls-files')).stdout.split('\n').filter(Boolean), ['app.txt']);
    const advertisement = await fetch(remote + '/info/refs?service=git-upload-pack');
    assert.equal(advertisement.status, 200);
    const allowed = new Set(['cache-control', 'connection', 'content-type', 'date', 'keep-alive', 'transfer-encoding']);
    for (const name of advertisement.headers.keys()) assert.ok(allowed.has(name), name);
    const listing = await advertisement.text();
    assert.ok(!listing.includes('github') && !listing.includes('pull') && !listing.includes(token));

    await fs.writeFile(path.join(copy, 'app.txt'), 'guest\n');
    await git('-C', copy, ...guest, 'commit', '-am', 'Правка агента');
    assert.equal((await fetch(remote + '/info/refs?service=git-receive-pack')).status, 403);
    await assert.rejects(git('-C', copy, 'push', 'origin', 'HEAD:refs/heads/work'), /403/);
    const receivePack = remote + '/git-receive-pack';
    assert.equal((await fetch(receivePack, {
        method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: '0000',
    })).status, 403);

    await updateConfig(true, 'agent/work');
    assert.equal((await fetch(receivePack, {
        method: 'POST', headers: { 'Content-Type': 'text/plain' }, body: '0000',
    })).status, 415);
    assert.equal((await fetch(receivePack, {
        method: 'POST', body: '0000',
        headers: { 'Content-Type': 'application/x-git-receive-pack-request', 'Content-Encoding': 'gzip' },
    })).status, 415);
    await git('-C', copy, 'fetch', 'origin');
    await git('-C', copy, ...guest, 'rebase', 'origin/work');
    await assert.rejects(git('-C', copy, 'push', 'origin', 'HEAD:main'), /только в ветку work/);
    await git('-C', copy, 'push', 'origin', 'HEAD:refs/heads/work');
    assert.ok(!(await git('-C', copy, 'ls-remote', 'origin')).stdout.includes('agent/work'));
    const exported = await git('-C', upstream, 'log', '--format=%an <%ae>|%s', 'agent/work');
    assert.equal(exported.stdout.split('\n')[0], 'agent-docker <agent-docker@localhost>|Правка агента');
    assert.equal((await git('-C', upstream, 'show', 'agent/work:.github/CODEOWNERS')).stdout, '* @ivan-petrov\n');
    assert.equal((await git('-C', upstream, 'show', 'agent/work:app.txt')).stdout, 'guest\n');
    assert.equal((await git('-C', upstream, 'log', '--format=%ae', '--all')).stdout.includes('guest@vm.invalid'), false);
    await assert.rejects(git('-C', copy, 'push', 'origin', ':refs/heads/work'));

    await git('-C', seed, 'checkout', '-qb', 'hidden/reference-source');
    await fs.writeFile(path.join(seed, 'reference.txt'), 'выбранное изменение\n');
    await git('-C', seed, 'add', 'reference.txt');
    await git('-C', seed, ...owner, 'commit', '-m', 'Private provider message');
    await git('-C', seed, 'push', upstream, 'hidden/reference-source');
    await fs.writeFile(config, JSON.stringify({
        repository: 'owner/source', write: true, token, writeBranch: 'agent/work',
        readBranches: { reference: 'hidden/reference-source' },
    }));
    const projected = path.join(root, 'projected');
    await git('clone', '--branch', 'work', remote, projected);
    await git('-C', projected, 'merge-base', 'origin/work', 'origin/reference');
    await git('-C', projected, ...guest, 'cherry-pick', 'origin/reference');
    await git('-C', seed, 'fetch', upstream, 'agent/work');
    await git('-C', seed, 'checkout', '-qB', 'agent/work', 'FETCH_HEAD');
    await fs.writeFile(path.join(seed, 'update.txt'), 'upstream\n');
    await git('-C', seed, 'add', 'update.txt');
    await git('-C', seed, ...owner, 'commit', '-m', 'New upstream change');
    await git('-C', seed, 'push', upstream, 'agent/work');
    await git('-C', projected, 'fetch', 'origin');
    await git('-C', projected, ...guest, 'rebase', 'origin/work');
    await git('-C', projected, 'checkout', '-qb', 'attempt', 'origin/reference');
    await git('-C', projected, ...guest, 'commit', '--allow-empty', '-m', 'Проверка запрета записи в ветку чтения');
    await assert.rejects(git('-C', projected, 'push', 'origin', 'HEAD:reference'), /только в ветку work/);
    await git('-C', projected, 'checkout', '-q', 'work');
    await git('-C', projected, 'push', 'origin', 'HEAD:work');
    assert.equal((await git('-C', upstream, 'show', 'agent/work:reference.txt')).stdout, 'выбранное изменение\n');
    const announced = (await git('-C', projected, 'ls-remote', 'origin')).stdout;
    assert.ok(announced.includes('refs/heads/reference') && !announced.includes('hidden/reference-source'));
    assert.ok(!(await git('-C', projected, 'log', '--all', '--format=%B')).stdout.includes('Private provider message'));

    const head = (await git('-C', copy, 'rev-parse', 'HEAD')).stdout.trim();
    const other = path.join(root, 'other.git');
    const otherSeed = path.join(root, 'other-seed');
    await git('init', '--bare', '--initial-branch=main', other);
    await git('init', '--initial-branch=main', otherSeed);
    await fs.writeFile(path.join(otherSeed, 'other.txt'), 'other\n');
    await git('-C', otherSeed, 'add', '-A');
    await git('-C', otherSeed, ...owner, 'commit', '-m', 'other repository');
    await git('-C', otherSeed, 'push', other, 'main');
    process.env.GIT_MIRROR_REMOTE = 'file://' + other;
    await fs.writeFile(config, JSON.stringify({ repository: 'owner/other', write: false, token, writeBranch: '' }));
    const want = 'want ' + head + '\n';
    const stale = await fetch(remote + '/git-upload-pack', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-git-upload-pack-request' },
        body: (want.length + 4).toString(16).padStart(4, '0') + want + '00000009done\n',
    });
    assert.equal(stale.status, 409);
    assert.ok(!(await stale.text()).includes('PACK'));
    const fresh = path.join(root, 'fresh');
    await git('clone', remote, fresh);
    assert.deepEqual((await git('-C', fresh, 'ls-files')).stdout.split('\n').filter(Boolean), ['other.txt']);

    process.env.GIT_MIRROR_REMOTE = 'file://' + path.join(root, 'missing.git');
    const failed = await fetch(remote + '/info/refs?service=git-upload-pack');
    assert.equal(failed.status, 502);
    assert.equal(await failed.text(), '');
});

test('settings parser accepts GitHub and self-managed GitLab and hides the token', async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-git-settings-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const config = path.join(root, 'repository.json');
    const settings = async value => {
        await fs.writeFile(config, JSON.stringify(value));
        return run(process.execPath, [path.join(__dirname, '..', 'git-gateway.cjs'), '--settings', config]);
    };
    const gitlabToken = 'glpat-fixture.settings-0007';
    assert.equal((await settings({ repository: 'owner/project', token, write: false })).stdout,
        'github https://github.com owner/project false \n');
    const gitlab = await settings({
        provider: 'gitlab', origin: 'https://gitlab.fixture.invalid/scm', project: 'group/sub/project',
        token: gitlabToken, write: true, writeBranch: 'agent/task-1',
    });
    assert.equal(gitlab.stdout, 'gitlab https://gitlab.fixture.invalid/scm group/sub/project true agent/task-1\n');
    const deep = Array.from({ length: 20 }, (_, index) => 'g' + index).join('/');
    assert.equal((await settings({
        provider: 'gitlab', origin: 'https://gitlab.fixture.invalid:65535/a.b', project: deep, token: gitlabToken, write: false,
    })).stdout, `gitlab https://gitlab.fixture.invalid:65535/a.b ${deep} false \n`);
    assert.equal((await settings({ repository: 'owner/project.atom', token, write: false })).stdout,
        'github https://github.com owner/project.atom false \n');
    for (const invalid of [
        { provider: 'gitlab', project: 'group/project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'http://gitlab.fixture.invalid', project: 'group/project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'https://gitlab.fixture.invalid', project: 'project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'https://gitlab.fixture.invalid', repository: 'group/project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'https://gitlab.fixture.invalid/..', project: 'group/project', token: gitlabToken, write: false },
        { provider: null, repository: 'owner/project', token, write: false },
        { provider: ['github'], origin: 'https://gitlab.fixture.invalid', repository: 'owner/project', token, write: false },
        { provider: 'github', project: 'owner/project', token, write: false },
        { repository: 'owner/project', project: 'owner/project', token, write: false },
        { provider: 'gitlab', origin: 'https://10.0.0.5', project: 'group/project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'https://www.github.com', project: 'group/project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'https://github.com:443', project: 'group/project', token: gitlabToken, write: false },
        { provider: 'gitlab', origin: 'https://gitlab.fixture.invalid:65536', project: 'group/project', token: gitlabToken, write: false },
        ['not', 'an', 'object'],
        { provider: 'github', origin: 'https://gitlab.fixture.invalid', repository: 'owner/project', token, write: false },
        { provider: 'bitbucket', repository: 'owner/project', token, write: false },
    ]) {
        const failure = await settings(invalid).catch(error => error);
        assert.equal(failure.code, 1);
        assert.ok(!String(failure.stderr).includes(gitlabToken) && !String(failure.stderr).includes('gitlab.fixture'));
    }
});

test('GitLab assignment is served through the same guest URL', async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-gitlab-test-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const config = path.join(root, 'repository.json');
    const upstream = path.join(root, 'upstream.git');
    const seed = path.join(root, 'seed');
    const copy = path.join(root, 'clone');
    const saved = { ...process.env };
    t.after(() => {
        for (const name of Object.keys(process.env)) if (!(name in saved)) delete process.env[name];
        Object.assign(process.env, saved);
    });
    process.env.GIT_CONFIG_NOSYSTEM = '1';
    process.env.GIT_CONFIG_GLOBAL = '/dev/null';
    process.env.GIT_MIRROR_ROOT = path.join(root, 'mirror');
    process.env.GIT_MIRROR_CONFIG = config;
    process.env.GIT_MIRROR_REMOTE = 'file://' + upstream;
    const git = (...args) => run('git', args);
    await git('init', '--bare', '--initial-branch=main', upstream);
    await git('init', '--initial-branch=main', seed);
    await fs.writeFile(path.join(seed, 'app.txt'), 'owner\n');
    await git('-C', seed, 'add', '-A');
    await git('-C', seed, ...owner, 'commit', '-m', 'GitLab owner commit');
    await git('-C', seed, 'push', upstream, 'main');
    const gitlabToken = 'glpat-fixture.handler-0008';
    await fs.writeFile(config, JSON.stringify({
        provider: 'gitlab', origin: 'https://gitlab.fixture.invalid/scm', project: 'group/sub/project',
        token: gitlabToken, write: true, writeBranch: 'agent/task-1',
    }));
    const { createGitHandler } = require('../git-gateway.cjs');
    const gateway = http.createServer(createGitHandler(config, process.env.GIT_MIRROR_ROOT));
    t.after(() => gateway.close());
    const remote = 'http://127.0.0.1:' + await listen(gateway) + '/project.git';
    const listing = await (await fetch(remote + '/info/refs?service=git-upload-pack')).text();
    for (const secret of [gitlabToken, 'gitlab.fixture', 'group/sub', 'agent/task-1', 'Ivan']) {
        assert.ok(!listing.includes(secret), secret);
    }
    await git('clone', '--branch', 'work', remote, copy);
    assert.equal((await git('-C', copy, 'log', '--format=%an')).stdout, 'agent-docker\n');
    await fs.writeFile(path.join(copy, 'app.txt'), 'guest\n');
    await git('-C', copy, ...guest, 'commit', '-am', 'GitLab guest change');
    await git('-C', copy, 'push', 'origin', 'HEAD:refs/heads/work');
    assert.equal((await git('-C', upstream, 'log', '-1', '--format=%an|%s', 'agent/task-1')).stdout,
        'agent-docker|GitLab guest change\n');
    await assert.rejects(git('-C', copy, 'push', 'origin', 'HEAD:main'), /только в ветку work/);
});

test('host publication can be cloned without provider settings and refuses all writes', async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-git-public-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const generation = 'view-abc0123456';
    const seed = path.join(root, 'seed');
    const publicRoot = path.join(root, 'public');
    const clean = path.join(publicRoot, generation, 'clean.git');
    await fs.mkdir(path.dirname(clean), { recursive: true });
    const git = (...args) => run('git', args);
    await git('init', '--bare', '--initial-branch=main', clean);
    await git('init', '--initial-branch=main', seed);
    await fs.writeFile(path.join(seed, 'app.txt'), 'публичная копия\n');
    await git('-C', seed, 'add', 'app.txt');
    await git('-C', seed, ...guest, 'commit', '-m', 'Тестовый снимок');
    await git('-C', seed, 'push', clean, 'HEAD:main', 'HEAD:reference');

    const { createGitHandler } = require('../git-gateway.cjs');
    let locatorCalls = 0;
    const gateway = http.createServer(createGitHandler(path.join(root, 'no-token.json'), root,
        async () => { locatorCalls++; return null; }, publicRoot));
    t.after(() => gateway.close());
    const remote = 'http://127.0.0.1:' + await listen(gateway) + '/project.git';
    const upload = remote + '/info/refs?service=git-upload-pack';
    assert.equal((await fetch(upload)).status, 502);
    await fs.writeFile(path.join(publicRoot, 'current'), '../seed\n');
    assert.equal((await fetch(upload)).status, 502);
    await fs.writeFile(path.join(publicRoot, 'current'), generation + '\n');
    const clone = path.join(root, 'clone');
    await git('clone', remote, clone);
    assert.equal((await git('-C', clone, 'show', 'origin/reference:app.txt')).stdout, 'публичная копия\n');
    await git('-C', clone, ...guest, 'commit', '--allow-empty', '-m', 'Попытка отправки');
    await assert.rejects(git('-C', clone, 'push', 'origin', 'HEAD:main'), /403/);
    assert.equal(locatorCalls, 0);
});
