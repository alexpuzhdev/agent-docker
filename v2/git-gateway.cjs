const fs = require('node:fs');
const https = require('node:https');
const tls = require('node:tls');
const { Readable } = require('node:stream');
const { connect } = require('./upstream.cjs');

const gitHost = 'git.agent-docker.test';
const origin = 'github.com';
const configPath = '/config/git/repository.json';
const repositoryPattern = /^[a-zA-Z0-9][a-zA-Z0-9-]{0,38}\/[a-zA-Z0-9][a-zA-Z0-9_.-]{0,99}$/;
const routes = new Map();

for (const operation of ['git-upload-pack', 'git-receive-pack']) {
    const type = 'application/x-' + operation;
    routes.set('GET /project.git/info/refs?service=' + operation, {
        path: '/info/refs?service=' + operation, responseType: type + '-advertisement', operation,
    });
    routes.set('POST /project.git/' + operation, {
        path: '/' + operation, requestType: type + '-request', responseType: type + '-result', operation,
    });
}

function isWritableBranch(branch) {
    return typeof branch === 'string' && branch.length <= 120 &&
        branch !== 'main' && branch !== 'master' &&
        /^(?:[a-zA-Z0-9][a-zA-Z0-9_-]*\/)*[a-zA-Z0-9][a-zA-Z0-9_-]*$/.test(branch);
}

async function authorizePush(incoming, branch) {
    const parts = incoming.iterator({ destroyOnReturn: false });
    let prefix = Buffer.alloc(0);
    let packetSize;
    for (;;) {
        const part = await parts.next();
        if (part.done) return null;
        prefix = Buffer.concat([prefix, part.value]);
        if (prefix.length < 4) continue;
        if (packetSize === undefined) {
            const length = prefix.toString('latin1', 0, 4);
            if (!/^[0-9a-fA-F]{4}$/.test(length)) return null;
            packetSize = Number.parseInt(length, 16);
            if (packetSize < 4 || packetSize > 65520) return null;
        }
        if (prefix.length >= packetSize + 4) break;
    }

    const command = prefix.toString('latin1', 4, packetSize).split('\0', 1)[0];
    const fields = /^([0-9a-f]{40}) ([0-9a-f]{40}) (refs\/heads\/[a-zA-Z0-9_/-]+)\n?$/.exec(command);
    if (prefix.toString('latin1', packetSize, packetSize + 4) !== '0000' ||
        !fields || fields[2] === '0'.repeat(40) || fields[3] !== 'refs/heads/' + branch) {
        return null;
    }
    return Readable.from((async function* () {
        yield prefix;
        for (;;) {
            const part = await parts.next();
            if (part.done) break;
            yield part.value;
        }
    })());
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

function createGitHandler(upstream, path = configPath, request = https.request) {
    const agent = new https.Agent({ keepAlive: false });
    agent.createConnection = (_, callback) => {
        connect(upstream, origin + ':443', (error, socket) => {
            if (error) return callback(error);
            callback(null, tls.connect({ socket, servername: origin }));
        });
    };
    return async (incoming, outgoing) => {
        const route = routes.get(incoming.method + ' ' + incoming.url);
        if (!route) return outgoing.writeHead(404).end();

        let config;
        try {
            config = readRepository(path);
        } catch (error) {
            return outgoing.writeHead(error.code === 'ENOENT' ? 404 : 503).end();
        }
        const writeRoute = route.operation === 'git-receive-pack';
        if (writeRoute && (!config.write || !isWritableBranch(config.writeBranch))) {
            return outgoing.writeHead(403).end();
        }
        if (route.requestType && incoming.headers['content-type'] !== route.requestType) {
            return outgoing.writeHead(415).end();
        }

        let body = incoming;
        if (writeRoute && route.requestType) {
            if (incoming.headers['content-encoding'] && incoming.headers['content-encoding'] !== 'identity') {
                return outgoing.writeHead(415).end();
            }
            incoming.setTimeout(10000, () => incoming.destroy());
            try {
                body = await authorizePush(incoming, config.writeBranch);
            } catch {
                return outgoing.destroy();
            } finally {
                incoming.setTimeout(0);
            }
            if (!body) return outgoing.writeHead(403, { Connection: 'close' }).end();
        }

        const headers = {
            'User-Agent': 'agent-docker-git-gateway',
            Authorization: 'Basic ' + Buffer.from('x-access-token:' + config.token).toString('base64'),
        };
        for (const name of ['content-type', 'content-length', 'content-encoding', 'git-protocol']) {
            if (incoming.headers[name] !== undefined) headers[name] = incoming.headers[name];
        }
        const remote = request({
            hostname: origin,
            path: '/' + config.repository + '.git' + route.path,
            method: incoming.method,
            agent,
            headers,
            timeout: 30000,
        }, response => {
            if (response.statusCode !== 200 || response.headers['content-type'] !== route.responseType) {
                response.destroy();
                return outgoing.writeHead(502).end();
            }
            outgoing.writeHead(200, {
                'Content-Type': route.responseType,
                'Cache-Control': 'no-store',
            });
            response.on('error', () => outgoing.destroy());
            response.pipe(outgoing);
        });
        remote.on('timeout', () => remote.destroy());
        remote.on('error', () => {
            if (!outgoing.headersSent) outgoing.writeHead(502).end();
            else outgoing.destroy();
        });
        outgoing.on('close', () => remote.destroy());
        body.on('error', () => remote.destroy());
        body.pipe(remote);
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
