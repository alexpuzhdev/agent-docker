const fs = require('node:fs');
const path = require('node:path');
const { execFile, spawn } = require('node:child_process');
const { promisify } = require('node:util');
const execute = promisify(execFile);

const gitHost = 'git.agent-docker.test';
const configPath = '/config/git/repository.json';
const mirrorRoot = '/mirror';
const mirrorScript = path.join(__dirname, 'git-mirror.py');
const maxRequestBytes = 256 * 1024 * 1024;
const providers = {
    github: {
        field: 'repository',
        suffix: /\.git$/,
        repository: /^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}\/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/,
        token: /^[a-zA-Z0-9_-]{10,512}$/,
    },
    gitlab: {
        field: 'project',
        suffix: /\.(?:git|atom)$/,
        repository: /^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}(?:\/[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,254}){1,19}$/,
        token: /^[a-zA-Z0-9_.-]{10,512}$/,
    },
};
const github = 'https://github.com';
const originPattern = /^https:\/\/(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?(?::(?:[1-9][0-9]{0,3}|[1-5][0-9]{4}|6[0-4][0-9]{3}|65[0-4][0-9]{2}|655[0-2][0-9]|6553[0-5]))?(?:\/[a-zA-Z0-9_~-][a-zA-Z0-9_.~-]*)*$/;
function githubHost(origin) {
    const host = origin.slice('https://'.length).split('/', 1)[0].split(':', 1)[0];
    return host === 'github.com' || host.endsWith('.github.com');
}
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

function readBranches(config) {
    const branches = config.readBranches === undefined ? {} : config.readBranches;
    if (branches === null || typeof branches !== 'object' || Array.isArray(branches)) {
        throw new Error('Некорректная конфигурация веток чтения.');
    }
    for (const [alias, branch] of Object.entries(branches)) {
        if (!/^[a-z][a-z0-9-]{0,31}$/.test(alias) || ['main', 'work'].includes(alias) ||
            typeof branch !== 'string' || branch.length > 120 ||
            !/^(?:[a-zA-Z0-9][a-zA-Z0-9_-]*\/)*[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(branch)) {
            throw new Error('Некорректная конфигурация веток чтения.');
        }
    }
    return Object.entries(branches).sort(([a], [b]) => (a < b ? -1 : Number(a > b)));
}

function readRepository(path) {
    const config = JSON.parse(fs.readFileSync(path, 'utf8'));
    if (typeof config !== 'object' || config === null || Array.isArray(config)) {
        throw new Error('Некорректная конфигурация Git-шлюза.');
    }
    const provider = config.provider === undefined ? 'github' : config.provider;
    const origin = config.origin === undefined && provider === 'github' ? github : config.origin;
    const rules = typeof provider === 'string' && Object.hasOwn(providers, provider) ? providers[provider] : undefined;
    const repository = rules ? config[rules.field] : undefined;
    const foreign = rules ? ['repository', 'project'].filter(field => field !== rules.field && field in config) : [];
    if (!rules || foreign.length || typeof repository !== 'string' || repository.length > 1024 ||
        !rules.repository.test(repository) || rules.suffix.test(repository) ||
        typeof config.write !== 'boolean' || typeof config.token !== 'string' || !rules.token.test(config.token) ||
        typeof origin !== 'string' || origin.length > 512 || !originPattern.test(origin) ||
        (provider === 'github') !== (origin === github) || (provider !== 'github' && githubHost(origin))) {
        throw new Error('Некорректная конфигурация Git-шлюза.');
    }
    return { ...config, provider, origin, repository, branches: readBranches(config) };
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

function publishedGeneration(root) {
    try {
        const generation = fs.readFileSync(path.join(root, 'current'), 'utf8').trim();
        return /^view-[a-zA-Z0-9]{10}$/.test(generation) ? generation : null;
    } catch {
        return null;
    }
}

function publishAvailable(socketPath) {
    try {
        return Boolean(socketPath) && fs.statSync(socketPath).isSocket();
    } catch {
        return false;
    }
}

async function preparePush(root, published, view) {
    const temporary = await fs.promises.mkdtemp(path.join(root, 'receive-'));
    const bare = path.join(temporary, 'clean.git');
    try {
        const env = mirrorEnvironment(root);
        await execute('git', ['clone', '-q', '--bare', '--no-hardlinks', '--no-tags', '--template=',
            path.join(published, view, 'clean.git'), bare], { env, timeout: 20000 });
        await execute('git', ['-C', bare, 'config', 'receive.maxInputSize', '256m'], { env, timeout: 5000 });
        await execute('git', ['-C', bare, 'config', 'receive.denyNonFastForwards', 'true'], { env, timeout: 5000 });
        const hooks = path.join(bare, 'hooks');
        await fs.promises.mkdir(hooks);
        await fs.promises.writeFile(path.join(hooks, 'pre-receive'),
            '#!/bin/sh\nexec python3 "$AT_PUBLISH_HOOK"\n', { mode: 0o700 });
        return temporary;
    } catch (error) {
        await fs.promises.rm(temporary, { recursive: true, force: true });
        throw error;
    }
}

function createGitHandler(repositoryConfig = process.env.GIT_MIRROR_CONFIG ?? configPath,
    root = process.env.GIT_MIRROR_ROOT ?? mirrorRoot, locator = locate,
    publishedRoot = process.env.GIT_HOST_PUBLISHED_ROOT, publishSocket = process.env.GIT_HOST_PUBLISH_SOCKET) {
    return async (incoming, outgoing) => {
        const route = routes.get(incoming.method + ' ' + incoming.url);
        if (!route) return outgoing.writeHead(404).end();

        const writing = route.operation === 'git-receive-pack';
        if (publishedRoot) {
            if (writing && !publishAvailable(publishSocket)) return outgoing.writeHead(403).end();
        } else {
            let config;
            try {
                config = readRepository(repositoryConfig);
            } catch (error) {
                return outgoing.writeHead(error.code === 'ENOENT' ? 404 : 503).end();
            }
            if (writing && (!config.write || !isWritableBranch(config.writeBranch))) {
                return outgoing.writeHead(403).end();
            }
        }
        if (route.requestType && incoming.headers['content-type'] !== route.requestType) {
            return outgoing.writeHead(415).end();
        }
        const encoding = incoming.headers['content-encoding'];
        if (route.operation === 'git-receive-pack' && encoding && encoding !== 'identity') {
            return outgoing.writeHead(415).end();
        }
        let servingRoot = publishedRoot || root;
        let location = publishedRoot
            ? publishedGeneration(publishedRoot)
            : await locator(root, incoming.method === 'GET' ? 'sync' : 'resolve');
        if (outgoing.destroyed || incoming.socket?.destroyed) return;
        if (!location) return outgoing.writeHead(incoming.method === 'GET' ? 502 : 409).end();

        const view = location;
        let pushHome;
        if (publishedRoot && writing && incoming.method === 'POST') {
            try {
                pushHome = await preparePush(root, publishedRoot, location);
            } catch {
                return outgoing.writeHead(503).end();
            }
            if (outgoing.destroyed || incoming.socket?.destroyed) {
                await fs.promises.rm(pushHome, { recursive: true, force: true });
                return;
            }
            servingRoot = root;
            location = path.basename(pushHome);
        }
        const backendArgs = publishedRoot && writing
            ? ['-c', 'http.receivepack=true', 'http-backend']
            : ['http-backend'];
        const backend = spawn('git', backendArgs, {
            env: {
                ...mirrorEnvironment(servingRoot),
                GIT_PROJECT_ROOT: servingRoot,
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
                ...(pushHome ? {
                    AT_PUBLISH_HOOK: path.join(__dirname, 'host-push-hook.py'),
                    AT_PUBLISH_SOCKET: publishSocket,
                    AT_PUBLISHED_VIEW: view,
                    TMPDIR: pushHome,
                } : {}),
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
        if (pushHome) {
            backend.once('close', () => fs.promises.rm(pushHome, { recursive: true, force: true }).catch(() => {}));
        }
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

if (require.main === module && !['--host-publication', '--host-write'].includes(process.argv[2])) {
    try {
        const settings = process.argv[2] === '--settings';
        const branchMap = process.argv[2] === '--read-branches';
        const config = readRepository(process.argv[settings || branchMap ? 3 : 2]);
        const aliases = config.branches.map(([alias, branch]) => `${alias}=${branch}`).join(',');
        if (branchMap) console.log(aliases);
        if (settings) {
            const branch = config.write && isWritableBranch(config.writeBranch) ? config.writeBranch : '';
            const fields = [config.provider, config.origin, config.repository, config.write, branch];
            if (aliases) {
                fields[4] ||= '-';
                fields.push(aliases);
            }
            console.log(fields.join(' '));
        }
    } catch {
        console.error('Некорректный репозиторий, токен или режим записи Git-шлюза.');
        process.exitCode = 1;
    }
}

module.exports = { createGitHandler, gitHost };
