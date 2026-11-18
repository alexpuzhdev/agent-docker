import argparse
import http.client
import json
import socket
import ssl
import struct
from contextlib import closing
from urllib.parse import urlsplit


class GatewayConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, gateway):
        super().__init__(host, port, timeout=5)
        self.gateway = gateway

    def connect(self):
        connection = socket.create_connection((self.gateway, self.port), self.timeout)
        try:
            self.sock = ssl.create_default_context().wrap_socket(connection, server_hostname=self.host)
        except OSError:
            connection.close()
            raise


def https_reaches_gateway(url, gateway):
    target = urlsplit(url)
    path = target.path or '/'
    if target.query:
        path += '?' + target.query
    try:
        if socket.gethostbyname(target.hostname) != gateway:
            return False
        with closing(GatewayConnection(target.hostname, target.port, gateway)) as client:
            client.request('GET', path)
            return client.getresponse().status == 200
    except (OSError, http.client.HTTPException):
        return False


def tcp_reachable(host, port):
    try:
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except OSError:
        return False


def udp_echo_reachable(host, port):
    data = b'agent-docker-isolation-probe'
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
            client.settimeout(2)
            client.connect((host, int(port)))
            client.sendall(data)
            return client.recv(256) == data
    except OSError:
        return False


def dns_name(packet, offset):
    labels = []
    limit = len(packet)
    end = None
    while offset < limit:
        size = packet[offset]
        if size == 0:
            return tuple(labels), end if end is not None else offset + 1
        if size & 0xc0 == 0xc0:
            if offset + 1 >= limit:
                break
            pointer = ((size & 0x3f) << 8) | packet[offset + 1]
            if not 12 <= pointer < offset:
                break
            if end is None:
                end = offset + 2
            limit, offset = offset, pointer
            continue
        offset += 1
        if size > 63 or offset + size > limit:
            break
        labels.append(packet[offset:offset + size].lower())
        offset += size
    raise ValueError('Некорректное доменное имя в ответе DNS.')


def dns_has_address(query, packet):
    if len(packet) < len(query) or packet[12:len(query)] != query[12:]:
        return False
    response_id, flags, questions, answers, _, _ = struct.unpack_from('!6H', packet)
    if response_id != int.from_bytes(query[:2], 'big') or flags & 0x820f != 0x8000 or questions != 1:
        return False

    asked, _ = dns_name(query, 12)
    offset = len(query)
    addresses, aliases = set(), {}
    try:
        for _ in range(answers):
            name, offset = dns_name(packet, offset)
            record_type, record_class, _, size = struct.unpack_from('!HHIH', packet, offset)
            start = offset + 10
            offset = start + size
            if offset > len(packet):
                return False
            if record_class == 1 and record_type == 1 and size == 4:
                addresses.add(name)
            if record_class == 1 and record_type == 5:
                alias, end = dns_name(packet, start)
                if end != offset:
                    return False
                aliases[name] = alias
    except (ValueError, struct.error):
        return False

    for _ in range(len(aliases) + 1):
        if asked in addresses:
            return True
        if asked not in aliases:
            return False
        asked = aliases[asked]
    return False


def dns_answered(host, server, transport):
    query_id = 45192
    labels = b''.join(bytes([len(part)]) + part.encode('idna') for part in host.split('.')) + b'\0'
    query = struct.pack('!6H', query_id, 0x0100, 1, 0, 0, 0) + labels + struct.pack('!2H', 1, 1)
    try:
        with socket.socket(socket.AF_INET, transport) as client:
            client.settimeout(2)
            client.connect((server, 53))
            if transport == socket.SOCK_STREAM:
                client.sendall(struct.pack('!H', len(query)) + query)
                with client.makefile('rb') as stream:
                    size = stream.read(2)
                    if len(size) != 2:
                        return False
                    packet = stream.read(int.from_bytes(size, 'big'))
                    if len(packet) != int.from_bytes(size, 'big'):
                        return False
            else:
                client.sendall(query)
                packet = client.recv(65535)
    except OSError:
        return False
    return dns_has_address(query, packet)


def main():
    parser = argparse.ArgumentParser(description='Проверить изоляцию из гостевой Linux VM.')
    parser.add_argument('--allowed-url', required=True)
    parser.add_argument('--gateway', required=True)
    parser.add_argument('--block-tcp', nargs=2, action='append', default=[])
    parser.add_argument('--block-udp', nargs=2, action='append', default=[])
    parser.add_argument('--dns-server', action='append', default=[])
    args = parser.parse_args()
    host = urlsplit(args.allowed_url).hostname
    if urlsplit(args.allowed_url).scheme != 'https' or not host:
        parser.error('Укажите разрешённый HTTPS URL.')
    results = {'allowed_https': https_reaches_gateway(args.allowed_url, args.gateway)}
    for endpoint in args.block_tcp:
        results[f'denied_tcp:{":".join(endpoint)}'] = not tcp_reachable(*endpoint)
    for endpoint in args.block_udp:
        results[f'denied_udp:{":".join(endpoint)}'] = not udp_echo_reachable(*endpoint)
    for server in args.dns_server:
        results[f'denied_dns_tcp:{server}'] = not dns_answered(host, server, socket.SOCK_STREAM)
        results[f'denied_dns_udp:{server}'] = not dns_answered(host, server, socket.SOCK_DGRAM)
    print(json.dumps(results, sort_keys=True))
    return 0 if all(results.values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
