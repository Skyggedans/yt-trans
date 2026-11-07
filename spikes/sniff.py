"""Diagnostic listener: shows exactly what the browser sends.

The daemon sees "Invalid HTTP request" but does not show the bytes. This listener
accepts a connection, prints the first bytes and closes it — from them it is immediately
clear whether it is TLS (starts with 0x16 0x03), a normal HTTP upgrade, or something else.

  python spikes/sniff.py            # listens on 8757
Then in the extension settings change the port to 8757 and reload the page.
"""

from __future__ import annotations

import argparse
import socket
import threading
import time


def dump(data: bytes) -> str:
    head = data[:200]
    printable = "".join(chr(b) if 32 <= b < 127 else "." for b in head)

    return f"    hex   : {head[:32].hex(' ')}\n    ascii : {printable[:180]}"


def classify(data: bytes) -> str:
    if not data:
        return "empty — the connection was opened and closed without sending anything"

    if data[0] == 0x16 and data[1:3] == b"\x03\x01" or data[:1] == b"\x16":
        return "TLS ClientHello — the client came over wss://, that is, someone upgraded the scheme"

    if data[:3] in (b"GET", b"POS", b"OPT", b"HEA"):
        line = data.split(b"\r\n", 1)[0].decode("latin1")
        upgrade = b"upgrade" in data.lower()

        return f"HTTP: {line}" + ("  (with Upgrade — a normal WebSocket)" if upgrade else "  (WITHOUT Upgrade)")

    return "neither TLS nor HTTP — unknown protocol"


def handle(conn: socket.socket, addr) -> None:
    conn.settimeout(3.0)

    try:
        data = conn.recv(4096)
    except OSError:
        data = b""

    stamp = time.strftime("%H:%M:%S")
    print(f"\n[{stamp}] connection from {addr[0]}:{addr[1]}, {len(data)} bytes")
    print(f"    verdict: {classify(data)}")

    if data:
        print(dump(data))

    conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8757)
    args = parser.parse_args()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.port))
    server.listen(16)

    print(f"listening on 127.0.0.1:{args.port} — set this port in the extension settings")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
