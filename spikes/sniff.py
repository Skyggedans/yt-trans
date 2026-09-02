"""Диагностический слушатель: показывает, что именно шлёт браузер.

Демон видит «Invalid HTTP request», но не показывает байты. Этот слушатель
принимает соединение, печатает первые байты и закрывает его — по ним сразу
видно, TLS это (начинается с 0x16 0x03), нормальный HTTP-апгрейд или что-то ещё.

  python spikes/sniff.py            # слушает 8757
Затем в настройках расширения поменять порт на 8757 и перезагрузить страницу.
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
        return "пусто — соединение открыли и закрыли, ничего не отправив"

    if data[0] == 0x16 and data[1:3] == b"\x03\x01" or data[:1] == b"\x16":
        return "TLS ClientHello — клиент пришёл по wss://, то есть схему кто-то повысил"

    if data[:3] in (b"GET", b"POS", b"OPT", b"HEA"):
        line = data.split(b"\r\n", 1)[0].decode("latin1")
        upgrade = b"upgrade" in data.lower()

        return f"HTTP: {line}" + ("  (с Upgrade — нормальный WebSocket)" if upgrade else "  (БЕЗ Upgrade)")

    return "не TLS и не HTTP — неизвестный протокол"


def handle(conn: socket.socket, addr) -> None:
    conn.settimeout(3.0)

    try:
        data = conn.recv(4096)
    except OSError:
        data = b""

    stamp = time.strftime("%H:%M:%S")
    print(f"\n[{stamp}] соединение с {addr[0]}:{addr[1]}, {len(data)} байт")
    print(f"    вывод : {classify(data)}")

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

    print(f"слушаю 127.0.0.1:{args.port} — поставь этот порт в настройках расширения")

    while True:
        conn, addr = server.accept()
        threading.Thread(target=handle, args=(conn, addr), daemon=True).start()


if __name__ == "__main__":
    raise SystemExit(main())
