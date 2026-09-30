import asyncio

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, generate_latest

_ALLOWED_BIND_HOSTS = frozenset({"127.0.0.1", "0.0.0.0"})
_MAX_REQUEST_LINE_BYTES = 4096
_MAX_HEADER_BYTES = 8192


def validate_metrics_listener(host: str, port: int) -> None:
    if host not in _ALLOWED_BIND_HOSTS:
        raise ValueError("scheduler metrics listener host is not allowed")
    if type(port) is not int or not 1 <= port <= 65_535:
        raise ValueError("scheduler metrics listener port is out of range")


async def start_scheduler_metrics_listener(
    host: str,
    port: int,
    registry: CollectorRegistry,
) -> asyncio.AbstractServer:
    validate_metrics_listener(host, port)

    async def handle_client(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        status = "404 Not Found"
        body = b"not found\n"
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
            if len(request_line) > _MAX_REQUEST_LINE_BYTES or not request_line.endswith(b"\n"):
                status = "400 Bad Request"
                body = b"bad request\n"
            else:
                header_bytes = 0
                while True:
                    line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                    header_bytes += len(line)
                    if header_bytes > _MAX_HEADER_BYTES or not line:
                        status = "400 Bad Request"
                        body = b"bad request\n"
                        break
                    if line in {b"\r\n", b"\n"}:
                        parts = request_line.decode("ascii", "ignore").strip().split()
                        if len(parts) == 3 and parts[0] != "GET":
                            status = "405 Method Not Allowed"
                            body = b"method not allowed\n"
                        elif len(parts) == 3 and parts[1] == "/metrics":
                            status = "200 OK"
                            body = generate_latest(registry)
                        break
        except TimeoutError, asyncio.IncompleteReadError, ConnectionError, OSError:
            status = "400 Bad Request"
            body = b"bad request\n"

        content_type = CONTENT_TYPE_LATEST if status == "200 OK" else "text/plain; charset=utf-8"
        writer.write(
            f"HTTP/1.1 {status}\r\n".encode("ascii")
            + f"Content-Type: {content_type}\r\n".encode("ascii")
            + f"Content-Length: {len(body)}\r\n".encode("ascii")
            + b"Connection: close\r\n\r\n"
            + body
        )
        try:
            await writer.drain()
        except ConnectionError, OSError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError, OSError:
                pass

    return await asyncio.start_server(handle_client, host=host, port=port, limit=8192)
