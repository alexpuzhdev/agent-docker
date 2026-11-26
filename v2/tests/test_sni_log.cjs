const assert = require('node:assert/strict');
const { test } = require('node:test');
const { createSniLog } = require('../sni-log.cjs');

const moment = new Date('2026-11-18T19:17:52.555Z');

function capture(enabled) {
    const lines = [];
    return { lines, log: createSniLog(enabled, line => lines.push(line), () => moment) };
}

test('disabled log writes nothing', () => {
    const { lines, log } = capture(false);
    log('api.anthropic.com', true);
    log('example.com', false);
    assert.deepEqual(lines, []);
});

test('each decision is one JSON line with time, domain and decision only', () => {
    const { lines, log } = capture(true);
    log('api.anthropic.com', true);
    log('example.com', false);
    log('', false);
    assert.equal(lines.length, 3);
    for (const line of lines) {
        assert.ok(line.endsWith('\n'));
        assert.equal(line.indexOf('\n'), line.length - 1);
    }
    assert.deepEqual(lines.map(line => JSON.parse(line)), [
        { time: '2026-11-18T19:17:52.555Z', domain: 'api.anthropic.com', decision: 'allow' },
        { time: '2026-11-18T19:17:52.555Z', domain: 'example.com', decision: 'deny' },
        { time: '2026-11-18T19:17:52.555Z', domain: '', decision: 'deny' },
    ]);
});

test('guest-controlled names cannot inject lines and are truncated', () => {
    const { lines, log } = capture(true);
    log('evil.test\n{"decision":"allow"}\u0000\u001b[2J', false);
    log('a'.repeat(1000), false);
    assert.equal(lines.length, 2);
    for (const line of lines) assert.equal(line.split('\n').length, 2);
    const [injected, long] = lines.map(line => JSON.parse(line));
    assert.equal(injected.decision, 'deny');
    assert.equal(injected.domain, 'evil.test\n{"decision":"allow"}\u0000\u001b[2J');
    assert.deepEqual(Object.keys(injected), ['time', 'domain', 'decision']);
    assert.equal(long.domain.length, 255);
});

test('non-string domain is logged as empty', () => {
    const { lines, log } = capture(true);
    log(undefined, false);
    log(null, false);
    assert.deepEqual(lines.map(line => JSON.parse(line).domain), ['', '']);
});
