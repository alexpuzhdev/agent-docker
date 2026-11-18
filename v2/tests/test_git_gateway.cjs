const assert = require('node:assert/strict');
const { execFile, spawn } = require('node:child_process');
const fs = require('node:fs/promises');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { test } = require('node:test');
const { promisify } = require('node:util');
const { createGitHandler } = require('../git-gateway.cjs');

const git = promisify(execFile);
const token = 'fixture_token_only';
const originRepository = 'owner/source';

function listen(server) {
    return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

function mockOrigin(root, observed, redirect) {
    return http.createServer((incoming, outgoing) => {
        const url = new URL(incoming.url, 'http://localhost');
        observed.push({ path: url.pathname, authorization: incoming.headers.authorization });
        if (redirect()) {
            return outgoing.writeHead(302, { Location: 'https://example.invalid/' }).end(token);
        }
        const backend = spawn('git', ['http-backend'], {
            env: {
                ...process.env,
                GIT_PROJECT_ROOT: root,
                GIT_HTTP_EXPORT_ALL: '1',
                PATH_INFO: url.pathname,
                REQUEST_METHOD: incoming.method,
                QUERY_STRING: url.search.slice(1),
                CONTENT_TYPE: incoming.headers['content-type'] ?? '',
                CONTENT_LENGTH: incoming.headers['content-length'] ?? '',
                HTTP_GIT_PROTOCOL: incoming.headers['git-protocol'] ?? '',
                REMOTE_USER: 'fixture',
            },
        });
        const chunks = [];
        incoming.pipe(backend.stdin);
        backend.stdout.on('data', chunk => chunks.push(chunk));
        backend.stderr.resume();
        backend.on('close', code => {
            if (code !== 0) return outgoing.writeHead(500).end();
            const output = Buffer.concat(chunks);
            const separator = output.indexOf('\r\n\r\n');
            let status = 200;
            for (const line of output.subarray(0, separator).toString().split('\r\n')) {
                const index = line.indexOf(':');
                const name = line.slice(0, index);
                const value = line.slice(index + 1).trim();
                if (name.toLowerCase() === 'status') status = Number(value.split(' ')[0]);
                else outgoing.setHeader(name, value);
            }
            outgoing.writeHead(status).end(output.subarray(separator + 4));
        });
    });
}

test('clone and fetch use one repository, while push requires explicit write access', async t => {
    const root = await fs.mkdtemp(path.join(os.tmpdir(), 'agent-git-test-'));
    t.after(() => fs.rm(root, { recursive: true, force: true }));
    const config = path.join(root, 'repository.json');
    const originPath = path.join(root, 'owner/source.git');
    const seed = path.join(root, 'seed');
    const copy = path.join(root, 'clone');
    await fs.mkdir(path.join(root, 'owner'));
    await git('git', ['init', '--bare', '--initial-branch=main', originPath]);
    await git('git', ['init', '--initial-branch=main', seed]);
    const commit = directory => git('git', ['-C', directory, '-c', 'user.name=Fixture',
        '-c', 'user.email=fixture@example.invalid', 'commit', '--allow-empty', '-m', 'fixture commit']);
    await commit(seed);
    await git('git', ['-C', seed, 'push', originPath, 'main']);

    const requests = [];
    let redirect = false;
    const origin = mockOrigin(root, requests, () => redirect);
    t.after(() => origin.close());
    const originPort = await listen(origin);
    const request = (options, callback) => {
        assert.equal(options.hostname, 'github.com');
        return http.request({ ...options, hostname: '127.0.0.1', port: originPort, agent: false }, callback);
    };
    const gateway = http.createServer(createGitHandler(null, config, request));
    t.after(() => gateway.close());
    const gatewayPort = await listen(gateway);
    const gatewayUrl = 'http://127.0.0.1:' + gatewayPort;
    const remote = gatewayUrl + '/project.git';
    const updateConfig = (write, writeBranch) => fs.writeFile(config, JSON.stringify({
        repository: originRepository, write, token, writeBranch,
    }));

    assert.equal((await fetch(remote + '/info/refs?service=git-upload-pack')).status, 404);
    await updateConfig(false);
    await git('git', ['clone', remote, copy]);
    await git('git', ['-C', copy, 'fetch', 'origin']);
    assert.ok(requests.some(r => r.path === '/owner/source.git/git-upload-pack'));
    const priorRequestCount = requests.length;
    assert.equal((await fetch(gatewayUrl + '/alien.git/info/refs?service=git-upload-pack')).status, 404);
    await commit(copy);
    await assert.rejects(git('git', ['-C', copy, 'push', 'origin', 'main']), /403/);
    assert.equal(requests.length, priorRequestCount);

    const advertisement = await fetch(remote + '/info/refs?service=git-upload-pack', {
        headers: { Authorization: 'Basic guest-supplied' },
    });
    assert.equal(advertisement.status, 200);
    assert.equal(advertisement.headers.get('authorization'), null);
    assert.ok(!(await advertisement.text()).includes(token));

    await updateConfig(true);
    assert.equal((await fetch(remote + '/info/refs?service=git-receive-pack')).status, 403);
    assert.equal((await fetch(remote + '/info/refs?service=git-upload-pack')).status, 200);
    await updateConfig(true, 'main');
    assert.equal((await fetch(remote + '/info/refs?service=git-receive-pack')).status, 403);
    await updateConfig(true, 'test/gateway-write');
    const writeRequests = () => requests.filter(r => r.path === '/owner/source.git/git-receive-pack').length;
    const beforeDenied = writeRequests();
    await assert.rejects(git('git', ['-C', copy, 'push', 'origin', 'main']), /403/);
    await assert.rejects(git('git', ['-C', copy, 'push', 'origin', 'HEAD:refs/tags/prohibited']), /403/);
    await assert.rejects(git('git', ['-C', copy, 'push', 'origin', 'main', 'HEAD:refs/heads/test/gateway-write']), /403/);
    const receivePack = remote + '/git-receive-pack';
    const packetHeaders = { 'Content-Type': 'application/x-git-receive-pack-request' };
    assert.equal((await fetch(receivePack, { method: 'POST', headers: packetHeaders, body: '0001' })).status, 403);
    assert.equal((await fetch(receivePack, {
        method: 'POST', headers: { ...packetHeaders, 'Content-Encoding': 'gzip' }, body: '0000',
    })).status, 415);
    assert.equal(writeRequests(), beforeDenied);
    await git('git', ['-C', copy, 'push', 'origin', 'HEAD:refs/heads/test/gateway-write']);
    const pushed = await git('git', ['-C', originPath, 'rev-parse', 'test/gateway-write']);
    const local = await git('git', ['-C', copy, 'rev-parse', 'main']);
    assert.equal(pushed.stdout, local.stdout);
    const afterPush = writeRequests();
    await assert.rejects(git('git', ['-C', copy, 'push', 'origin', ':refs/heads/test/gateway-write']), /403/);
    assert.equal(writeRequests(), afterPush);
    assert.equal((await git('git', ['-C', originPath, 'rev-parse', 'main'])).stdout,
        (await git('git', ['-C', seed, 'rev-parse', 'main'])).stdout);
    assert.ok(requests.some(r => r.path === '/owner/source.git/git-receive-pack'));

    redirect = true;
    const blocked = await fetch(remote + '/info/refs?service=git-upload-pack');
    assert.equal(blocked.status, 502);
    assert.equal(blocked.headers.get('location'), null);
    assert.equal(await blocked.text(), '');

    assert.ok(requests.every(r => r.authorization === 'Basic ' +
        Buffer.from('x-access-token:' + token).toString('base64')));
});
