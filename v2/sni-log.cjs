function createSniLog(enabled, write = line => process.stdout.write(line), now = () => new Date()) {
    if (!enabled) return () => {};
    return (domain, allowed) => {
        write(JSON.stringify({
            time: now().toISOString(),
            domain: typeof domain === 'string' ? domain.slice(0, 255) : '',
            decision: allowed ? 'allow' : 'deny',
        }) + '\n');
    };
}

module.exports = { createSniLog };
