const fs = require('node:fs');
const http = require('node:http');
const net = require('node:net');
const { connect, readUpstream } = require('./upstream.cjs');

const upstream = readUpstream('/config/upstream');
const domains = new Set(fs.readFileSync('/config/allowed-domains', 'utf8')
    .split('\n').map(line => line.split('#', 1)[0].trim().toLowerCase()).filter(Boolean));
const listen = process.env.GATEWAY_IP;

function sniFromHello(hello) {
    try {
        if (hello[0] !== 1) return '';
        let offset = 38;
        offset += 1 + hello.readUInt8(offset);
        offset += 2 + hello.readUInt16BE(offset);
        offset += 1 + hello.readUInt8(offset);
        const end = offset + 2 + hello.readUInt16BE(offset);
        offset += 2;
        if (end > hello.length) return '';
        while (offset + 4 <= end) {
            const kind = hello.readUInt16BE(offset);
            const size = hello.readUInt16BE(offset + 2);
            offset += 4;
            if (offset + size > end) return '';
            if (kind === 0 && size >= 5 && hello[offset + 2] === 0) {
                const length = hello.readUInt16BE(offset + 3);
                if (length > size - 5) return '';
                return hello.toString('ascii', offset + 5, offset + 5 + length).toLowerCase();
            }
            offset += size;
        }
    } catch {
        return '';
    }
    return '';
}

net.createServer(client => {
    let wire = Buffer.alloc(0);
    let hello = Buffer.alloc(0);
    let cursor = 0;
    let tunnel;
    client.on('error', () => client.destroy());
    client.on('close', () => tunnel?.destroy());
    client.setTimeout(6000, () => client.destroy());
    const receive = chunk => {
        wire = Buffer.concat([wire, chunk]);
        if (wire.length > 65536) return client.destroy();
        while (wire.length >= cursor + 5) {
            if (wire[cursor] !== 22) return client.destroy();
            const end = cursor + 5 + wire.readUInt16BE(cursor + 3);
            if (wire.length < end) return;
            hello = Buffer.concat([hello, wire.subarray(cursor + 5, end)]);
            cursor = end;
            if (hello.length < 4 || hello.length < 4 + hello.readUIntBE(1, 3)) continue;
            const domain = sniFromHello(hello);
            if (!domains.has(domain)) return client.destroy();
            client.removeListener('data', receive);
            client.pause();
            tunnel = connect(upstream, domain + ':443', (error, remote) => {
                if (error || client.destroyed) {
                    remote?.destroy();
                    return client.destroy();
                }
                client.setTimeout(0);
                client.on('close', () => remote.destroy());
                remote.on('close', () => client.destroy());
                remote.on('error', () => client.destroy());
                remote.write(wire);
                client.pipe(remote).pipe(client);
            });
            return;
        }
    };
    client.on('data', receive);
}).listen(443, listen);

http.createServer((incoming, outgoing) => {
    const host = (incoming.headers.host || '').toLowerCase();
    if (!domains.has(host) || !incoming.url.startsWith('/') || incoming.url.startsWith('//')) {
        outgoing.writeHead(403).end();
        return;
    }
    const request = http.request({
        ...upstream,
        method: incoming.method,
        path: 'http://' + host + incoming.url,
        headers: { ...incoming.headers, ...upstream.headers },
        timeout: 10000,
    }, response => {
        outgoing.writeHead(response.statusCode, response.headers);
        response.pipe(outgoing);
    });
    request.on('timeout', () => request.destroy());
    request.on('error', () => outgoing.writeHead(502).end());
    incoming.pipe(request);
}).listen(80, listen);
