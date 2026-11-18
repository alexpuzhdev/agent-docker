const fs = require('node:fs');
const http = require('node:http');

function readUpstream(path) {
    try {
        const input = fs.readFileSync(path, 'utf8').trim();
        const proxy = new URL(input);
        if (proxy.protocol !== 'http:' || !proxy.hostname ||
                !['', '/'].includes(proxy.pathname) || proxy.search || proxy.hash ||
                Boolean(proxy.username) !== Boolean(proxy.password)) {
            throw new Error();
        }
        const headers = {};
        if (proxy.username) {
            const account = decodeURIComponent(proxy.username) + ':' + decodeURIComponent(proxy.password);
            headers['Proxy-Authorization'] = 'Basic ' + Buffer.from(account).toString('base64');
        }
        return {hostname: proxy.hostname, port: proxy.port || 80, headers};
    } catch {
        throw new Error('Укажите адрес HTTP-прокси с портом. Допускается авторизация в URL.');
    }
}

function connect(upstream, authority, callback) {
    const request = http.request({
        ...upstream,
        method: 'CONNECT',
        path: authority,
        headers: { ...upstream.headers, Host: authority },
        timeout: 10000,
    });
    request.on('timeout', () => request.destroy(new Error('Истекло время ожидания upstream.')));
    request.on('error', callback);
    request.on('connect', (response, socket, head) => {
        if (response.statusCode !== 200) {
            socket.destroy();
            callback(new Error('Upstream отклонил соединение: ' + response.statusCode));
            return;
        }
        if (head.length) socket.unshift(head);
        callback(null, socket);
    });
    request.end();
    return request;
}

module.exports = { readUpstream, connect };
