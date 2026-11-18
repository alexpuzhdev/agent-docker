const https = require('node:https');
const { connect, readUpstream } = require('./upstream.cjs');

(async () => {
    const proxy = readUpstream(process.argv[2]);
    const host = 'downloads.claude.ai';
    const path = '/claude-code/apt/stable/dists/stable/InRelease';
    for (let index = 0; index < 3; index++) {
        await new Promise((resolve, reject) => {
            const agent = new https.Agent({keepAlive: false});
            agent.createConnection = (options, callback) => {
                connect(proxy, host + ':443', (error, socket) => {
                    if (error) return callback(error);
                    callback(null, require('node:tls').connect({...options, socket, servername: host}));
                });
            };
            const request = https.get({hostname:host, path, agent, timeout:10000}, response => {
                response.resume();
                response.on('end', () => response.statusCode === 200 ? resolve() : reject(new Error('HTTPS ' + response.statusCode)));
            });
            request.on('error', reject);
            request.on('timeout', () => request.destroy(new Error('Истекло время ответа.')));
        });
        if (index < 2) await new Promise(resolve => setTimeout(resolve, 1000));
    }
})().catch(error => {
    console.error('Проверка прокси не пройдена. ' + error.message);
    process.exitCode = 1;
});
