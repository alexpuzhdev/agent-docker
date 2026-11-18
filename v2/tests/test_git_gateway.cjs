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
    await assert.rejects(git('-C', copy, 'push', 'origin', 'HEAD:refs/heads/agent/work'), /403/);
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
    await git('-C', copy, ...guest, 'rebase', 'origin/agent/work');
    await assert.rejects(git('-C', copy, 'push', 'origin', 'HEAD:main'), /назначенную ветку/);
    await git('-C', copy, 'push', 'origin', 'HEAD:refs/heads/agent/work');
    const exported = await git('-C', upstream, 'log', '--format=%an <%ae>|%s', 'agent/work');
    assert.equal(exported.stdout.split('\n')[0], 'agent-docker <agent-docker@localhost>|Правка агента');
    assert.equal((await git('-C', upstream, 'show', 'agent/work:.github/CODEOWNERS')).stdout, '* @ivan-petrov\n');
    assert.equal((await git('-C', upstream, 'show', 'agent/work:app.txt')).stdout, 'guest\n');
    assert.equal((await git('-C', upstream, 'log', '--format=%ae', '--all')).stdout.includes('guest@vm.invalid'), false);
    await assert.rejects(git('-C', copy, 'push', 'origin', ':refs/heads/agent/work'));

    process.env.GIT_MIRROR_REMOTE = 'file://' + path.join(root, 'missing.git');
    const failed = await fetch(remote + '/info/refs?service=git-upload-pack');
    assert.equal(failed.status, 502);
    assert.equal(await failed.text(), '');
});
