const fs = require('node:fs');
const path = require('node:path');
const { execFile, spawn } = require('node:child_process');

const gitHost = 'git.agent-docker.test';
const configPath = '/config/git/repository.json';
const mirrorRoot = '/mirror';
const mirrorScript = path.join(__dirname, 'git-mirror.py');
const maxRequestBytes = 256 * 1024 * 1024;
const repositoryPattern = /^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}\/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/;
const routes = new Map();

for (const operation of ['git-upload-pack', 'git-receive-pack']) {
    const type = 'application/x-' + operation;
    routes.set('GET /project.git/info/refs?service=' + operation, {
        pathInfo: '/clean.git/info/refs', query: 'service=' + operation, operation,
    });
    routes.set('POST /project.git/' + operation, {
        pathInfo: '/clean.git/' + operation, query: '', requestType: type + '-request', operation,
    });
}

function isWritableBranch(branch) {
    return typeof branch === 'string' && branch.length <= 120 &&
        branch !== 'main' && branch !== 'master' &&
        /^(?:[a-zA-Z0-9][a-zA-Z0-9_-]*\/)*[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(branch);
}

function readRepository(path) {
    const config = JSON.parse(fs.readFileSync(path, 'utf8'));
    if (typeof config?.repository !== 'string' || !repositoryPattern.test(config.repository) ||
        config.repository.endsWith('.git') || typeof config.write !== 'boolean' ||
        typeof config.token !== 'string' || !/^[a-zA-Z0-9_-]{10,512}$/.test(config.token)) {
        throw new Error('Некорректная конфигурация Git-шлюза.');
    }
    return config;
}

function mirrorEnvironment(root) {
    const environment = {
        PATH: process.env.PATH, HOME: root, GIT_CONFIG_NOSYSTEM: '1', GIT_CONFIG_GLOBAL: '/dev/null',
    };
    for (const [name, value] of Object.entries(process.env)) {
        if (name.startsWith('GIT_MIRROR_')) environment[name] = value;
    }
    return environment;
}

function locate(root, command) {
    return new Promise(resolve => {
        execFile('python3', [mirrorScript, command], { env: mirrorEnvironment(root), timeout: 300000 },
            (error, stdout) => {
                const location = String(stdout).trim();
                resolve(!error && /^mirrors\/[0-9a-f]{64}$/.test(location) ? location : null);
            });
    });
}

function respond(backend, outgoing, stop) {
    let header = Buffer.alloc(0);
    const receive = chunk => {
        header = Buffer.concat([header, chunk]);
        const separator = header.indexOf('\r\n\r\n');
        if (separator < 0) {
            if (header.length > 65536) stop();
            return;
        }
        backend.stdout.removeListener('data', receive);
        let status = 200;
        let type;
        for (const line of header.subarray(0, separator).toString('latin1').split('\r\n')) {
            const index = line.indexOf(':');
            const name = line.slice(0, index).trim().toLowerCase();
            const value = line.slice(index + 1).trim();
            if (name === 'status') status = Number.parseInt(value, 10) || 502;
            if (name === 'content-type') type = value;
        }
        outgoing.writeHead(status, {
            ...(type ? { 'Content-Type': type } : {}),
            'Cache-Control': 'no-store',
        });
        outgoing.write(header.subarray(separator + 4));
        backend.stdout.pipe(outgoing);
    };
    backend.stdout.on('data', receive);
    backend.on('close', () => {
        if (!outgoing.headersSent) outgoing.writeHead(502).end();
    });
}

function createGitHandler(path = process.env.GIT_MIRROR_CONFIG ?? configPath,
    root = process.env.GIT_MIRROR_ROOT ?? mirrorRoot, locator = locate) {
    return async (incoming, outgoing) => {
        const route = routes.get(incoming.method + ' ' + incoming.url);
        if (!route) return outgoing.writeHead(404).end();

        let config;
        try {
            config = readRepository(path);
        } catch (error) {
            return outgoing.writeHead(error.code === 'ENOENT' ? 404 : 503).end();
        }
        if (route.operation === 'git-receive-pack' && (!config.write || !isWritableBranch(config.writeBranch))) {
            return outgoing.writeHead(403).end();
        }
        if (route.requestType && incoming.headers['content-type'] !== route.requestType) {
            return outgoing.writeHead(415).end();
        }
        const encoding = incoming.headers['content-encoding'];
        if (route.operation === 'git-receive-pack' && encoding && encoding !== 'identity') {
            return outgoing.writeHead(415).end();
        }
        const location = await locator(root, incoming.method === 'GET' ? 'sync' : 'resolve');
        if (outgoing.destroyed || incoming.socket?.destroyed) return;
        if (!location) return outgoing.writeHead(incoming.method === 'GET' ? 502 : 409).end();

        const backend = spawn('git', ['http-backend'], {
            env: {
                ...mirrorEnvironment(root),
                GIT_PROJECT_ROOT: root,
                GIT_HTTP_EXPORT_ALL: '1',
                PATH_INFO: '/' + location + route.pathInfo,
                QUERY_STRING: route.query,
                REQUEST_METHOD: incoming.method,
                CONTENT_TYPE: incoming.headers['content-type'] ?? '',
                CONTENT_LENGTH: incoming.headers['content-length'] ?? '',
                HTTP_CONTENT_ENCODING: incoming.headers['content-encoding'] ?? '',
                GIT_PROTOCOL: incoming.headers['git-protocol'] ?? '',
                REMOTE_USER: 'agent',
                REMOTE_ADDR: '127.0.0.1',
            },
            stdio: ['pipe', 'pipe', 'ignore'],
            detached: true,
        });
        let exited = false;
        const stop = () => {
            if (exited) return;
            try {
                process.kill(-backend.pid, 'SIGKILL');
            } catch {}
        };
        const deadline = setTimeout(stop, 600000);
        backend.on('exit', () => {
            exited = true;
            clearTimeout(deadline);
        });
        backend.on('error', () => {
            clearTimeout(deadline);
            if (!outgoing.headersSent) outgoing.writeHead(502).end();
            else outgoing.destroy();
        });
        outgoing.on('close', () => {
            if (!outgoing.writableFinished && route.operation !== 'git-receive-pack') stop();
        });
        let received = 0;
        incoming.on('data', chunk => {
            received += chunk.length;
            if (received > maxRequestBytes) {
                incoming.destroy();
                stop();
            }
        });
        incoming.on('error', stop);
        backend.stdin.on('error', stop);
        backend.stdout.on('error', stop);
        incoming.pipe(backend.stdin);
        respond(backend, outgoing, stop);
    };
}

if (require.main === module) {
    try {
        const settings = process.argv[2] === '--settings';
        const config = readRepository(process.argv[settings ? 3 : 2]);
        if (settings) {
            const branch = config.write && isWritableBranch(config.writeBranch) ? config.writeBranch : '';
            console.log([config.repository, config.write, branch].join(' '));
        }
    } catch {
        console.error('Некорректный репозиторий, токен или режим записи Git-шлюза.');
        process.exitCode = 1;
    }
}

module.exports = { createGitHandler, gitHost };
