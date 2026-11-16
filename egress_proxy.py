"""Connection-time public-destination proxy for the Playwright browser."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


async def resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    """Resolve a host once and return only when every answer is globally routable."""
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        try:
            records = await asyncio.wait_for(
                asyncio.to_thread(
                    socket.getaddrinfo,
                    host,
                    port,
                    0,
                    socket.SOCK_STREAM,
                ),
                timeout=5,
            )
            addresses = {
                ipaddress.ip_address(record[4][0].split("%", 1)[0])
                for record in records
            }
        except (OSError, ValueError, asyncio.TimeoutError):
            return ()
    else:
        addresses = {literal}

    if not addresses or any(not address.is_global for address in addresses):
        return ()
    return tuple(sorted(str(address) for address in addresses))


class PublicEgressProxy:
    """Loopback-only HTTP proxy that blocks private destinations and DNS rebinding."""

    def __init__(self) -> None:
        self._server: asyncio.AbstractServer | None = None

    @property
    def url(self) -> str:
        if self._server is None or not self._server.sockets:
            raise RuntimeError("The egress proxy has not been started.")
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        if self._server is not None:
            return
        self._server = await asyncio.start_server(
            self._handle_client,
            host="127.0.0.1",
            port=0,
            limit=64 * 1024,
        )

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def __aenter__(self) -> "PublicEgressProxy":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def _handle_client(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            request_line = await asyncio.wait_for(client_reader.readline(), timeout=10)
            if not request_line or len(request_line) > 8192:
                await self._reply(client_writer, 400, "Bad Request")
                return
            parts = request_line.decode("latin-1").strip().split(" ", 2)
            if len(parts) != 3:
                await self._reply(client_writer, 400, "Bad Request")
                return
            method, target, version = parts
            headers = await self._read_headers(client_reader)
            if headers is None:
                await self._reply(client_writer, 400, "Bad Request")
                return

            if method.upper() == "CONNECT":
                host, port = self._parse_authority(target)
                if not host or port != 443:
                    await self._reply(client_writer, 403, "Forbidden")
                    return
                addresses = await resolve_public_addresses(host, port)
                if not addresses:
                    await self._reply(client_writer, 403, "Forbidden")
                    return
                upstream_reader, upstream_writer = await self._open_upstream(addresses, port)
                client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await client_writer.drain()
                await self._relay(
                    client_reader,
                    client_writer,
                    upstream_writer,
                    upstream_reader,
                )
                return

            parsed = urlsplit(target)
            try:
                host = (parsed.hostname or "").rstrip(".").lower()
                port = parsed.port or (80 if parsed.scheme.lower() == "http" else 443)
            except ValueError:
                host, port = "", 0
            if (
                parsed.scheme.lower() != "http"
                or not host
                or parsed.username is not None
                or parsed.password is not None
                or port != 80
            ):
                await self._reply(client_writer, 403, "Forbidden")
                return

            addresses = await resolve_public_addresses(host, port)
            if not addresses:
                await self._reply(client_writer, 403, "Forbidden")
                return
            upstream_reader, upstream_writer = await self._open_upstream(addresses, port)
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            forwarded_headers = self._rewrite_http_headers(headers, host)
            upstream_writer.write(
                f"{method} {path} {version}\r\n".encode("latin-1")
                + forwarded_headers
                + b"\r\n"
            )
            await upstream_writer.drain()
            await self._relay(
                client_reader,
                client_writer,
                upstream_writer,
                upstream_reader,
            )
        except asyncio.TimeoutError:
            await self._safe_reply(client_writer, 504, "Gateway Timeout")
        except (asyncio.IncompleteReadError, ValueError):
            await self._safe_reply(client_writer, 400, "Bad Request")
        except OSError:
            await self._safe_reply(client_writer, 502, "Bad Gateway")
        except Exception:
            logger.exception("Public egress proxy request failed")
            await self._safe_reply(client_writer, 502, "Bad Gateway")
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                try:
                    await upstream_writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
            client_writer.close()
            try:
                await client_writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    @staticmethod
    async def _read_headers(reader: asyncio.StreamReader) -> list[bytes] | None:
        headers: list[bytes] = []
        size = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not line:
                return None
            size += len(line)
            if size > 64 * 1024:
                return None
            if line in {b"\r\n", b"\n"}:
                return headers
            if b":" not in line:
                return None
            headers.append(line.rstrip(b"\r\n"))

    @staticmethod
    async def _open_upstream(
        addresses: tuple[str, ...],
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        last_error: OSError | asyncio.TimeoutError | None = None
        for address in addresses:
            try:
                return await asyncio.wait_for(
                    asyncio.open_connection(address, port),
                    timeout=5,
                )
            except (OSError, asyncio.TimeoutError) as exc:
                last_error = exc
        raise OSError("Could not connect to any validated public IP address.") from last_error

    @staticmethod
    def _parse_authority(authority: str) -> tuple[str, int]:
        try:
            parsed = urlsplit("//" + authority)
            host = (parsed.hostname or "").rstrip(".").lower()
            port = parsed.port or 443
            if parsed.username is not None or parsed.password is not None:
                return "", 0
            return host, port
        except ValueError:
            return "", 0

    @staticmethod
    def _rewrite_http_headers(headers: list[bytes], host: str) -> bytes:
        host_header = f"[{host}]" if ":" in host else host
        rewritten: list[bytes] = []
        saw_host = False
        for raw in headers:
            name, _, value = raw.partition(b":")
            lowered = name.strip().lower()
            if lowered in {b"connection", b"proxy-connection", b"proxy-authorization"}:
                continue
            if lowered == b"host":
                rewritten.append(b"Host: " + host_header.encode("idna"))
                saw_host = True
            else:
                rewritten.append(name + b":" + value)
        if not saw_host:
            rewritten.append(b"Host: " + host_header.encode("idna"))
        rewritten.append(b"Connection: close")
        return b"\r\n".join(rewritten) + b"\r\n"

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        body = reason.encode("ascii")
        response = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        writer.write(response)
        await writer.drain()

    @classmethod
    async def _safe_reply(cls, writer: asyncio.StreamWriter, code: int, reason: str) -> None:
        if writer.is_closing():
            return
        try:
            await cls._reply(writer, code, reason)
        except (ConnectionError, OSError):
            pass

    @staticmethod
    async def _relay(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
    ) -> None:
        async def copy_stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                while chunk := await reader.read(64 * 1024):
                    writer.write(chunk)
                    await writer.drain()
            except (ConnectionError, OSError):
                pass

        tasks = {
            asyncio.create_task(copy_stream(client_reader, upstream_writer)),
            asyncio.create_task(copy_stream(upstream_reader, client_writer)),
        }
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
