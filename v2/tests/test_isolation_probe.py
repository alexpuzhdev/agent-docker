import struct
import unittest
from unittest.mock import patch

from isolation_probe import GatewayConnection, dns_has_address, https_reaches_gateway


class IsolationProbeTest(unittest.TestCase):
    def test_dns_requires_complete_address_answer(self):
        query_id = 45192
        question = b'\x07example\x04test\0' + struct.pack('!2H', 1, 1)
        query = struct.pack('!6H', query_id, 0x0100, 1, 0, 0, 0) + question
        answer = b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 300, 4) + b'\x01\x02\x03\x04'
        packet = struct.pack('!6H', query_id, 0x8180, 1, 1, 0, 0) + question + answer
        truncated = struct.pack('!6H', query_id, 0x8380, 1, 1, 0, 0)
        self.assertTrue(dns_has_address(query, packet))
        self.assertFalse(dns_has_address(query, truncated))
        self.assertFalse(dns_has_address(query, packet[:-1]))
        self.assertFalse(dns_has_address(query, truncated + question + answer))
        alias_name = b'\x05other\x04test\0'
        unrelated = alias_name + answer[2:]
        self.assertFalse(dns_has_address(query, packet[:len(query)] + unrelated))
        alias = b'\xc0\x0c' + struct.pack('!HHIH', 5, 1, 300, len(alias_name)) + alias_name
        chain = struct.pack('!6H', query_id, 0x8180, 1, 2, 0, 0) + question + alias + unrelated
        self.assertTrue(dns_has_address(query, chain))

    @patch('isolation_probe.GatewayConnection')
    @patch('isolation_probe.socket.gethostbyname', return_value='10.243.95.2')
    def test_https_redirect_is_not_a_success(self, resolve, connection):
        client = connection.return_value
        client.getresponse.return_value.status = 302
        self.assertFalse(https_reaches_gateway('https://example.test/resource', '10.243.95.2'))
        resolve.assert_called_once_with('example.test')
        client.request.assert_called_once_with('GET', '/resource')
        client.close.assert_called_once()

    @patch('isolation_probe.ssl.create_default_context')
    @patch('isolation_probe.socket.create_connection')
    def test_connection_uses_gateway_ip_and_original_tls_name(self, connect, create_context):
        connection = GatewayConnection('example.test', None, '10.243.95.2')
        connection.connect()
        connect.assert_called_once_with(('10.243.95.2', 443), 5)
        create_context.return_value.wrap_socket.assert_called_once_with(
            connect.return_value, server_hostname='example.test'
        )
