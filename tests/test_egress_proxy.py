import asyncio
import socket
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from egress_proxy import PublicEgressProxy, resolve_public_addresses


class EgressProxyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def record(address):
        return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))

    async def test_resolver_accepts_public_answers(self):
        records = [self.record("8.8.8.8"), self.record("1.1.1.1")]
        with patch("egress_proxy.socket.getaddrinfo", return_value=records):
            addresses = await resolve_public_addresses("public.example", 443)
        self.assertEqual(addresses, ("1.1.1.1", "8.8.8.8"))

    async def test_resolver_rejects_mixed_public_and_private_answers(self):
        records = [self.record("8.8.8.8"), self.record("192.168.1.20")]
        with patch("egress_proxy.socket.getaddrinfo", return_value=records):
            addresses = await resolve_public_addresses("mixed.example", 443)
        self.assertEqual(addresses, ())

    async def test_proxy_blocks_private_destination_at_connect_time(self):
        proxy = PublicEgressProxy()
        await proxy.start()
        reader = None
        writer = None
        try:
            port = urlsplit(proxy.url).port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"CONNECT 127.0.0.1:443 HTTP/1.1\r\n"
                b"Host: 127.0.0.1:443\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            self.assertTrue(response.startswith(b"HTTP/1.1 403 Forbidden"))
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await proxy.close()

    async def test_proxy_blocks_private_plain_http_destination(self):
        proxy = PublicEgressProxy()
        await proxy.start()
        reader = None
        writer = None
        try:
            port = urlsplit(proxy.url).port
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(
                b"GET http://127.0.0.1/secret HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n\r\n"
            )
            await writer.drain()
            response = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2)
            self.assertTrue(response.startswith(b"HTTP/1.1 403 Forbidden"))
        finally:
            if writer is not None:
                writer.close()
                await writer.wait_closed()
            await proxy.close()


if __name__ == "__main__":
    unittest.main()
