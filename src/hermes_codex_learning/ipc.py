import json
import socket

from .config import Settings


def request(settings: Settings, method: str, params: dict | None = None, *, timeout: float = 15) -> dict:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(timeout)
        client.connect(str(settings.socket))
        client.sendall((json.dumps({"method": method, "params": params or {}}) + "\n").encode())
        stream = client.makefile("rb")
        response = json.loads(stream.readline(4 * 1024 * 1024))
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]
