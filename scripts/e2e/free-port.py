#!/usr/bin/env python3
"""Print N free TCP ports, space-separated on one line.

Binds N sockets to 127.0.0.1:0 and collects every assigned port *before*
closing any of them, so the N ports are guaranteed distinct within one call.
Space-separated output lets a caller do `read A B < <(free-port.py 2)`.

There is an unavoidable TOCTOU window between closing the sockets here and the
consumer binding the port; that is acceptable for the per-worktree harness
(the DB port is allocated race-free by Docker, and the API/embedding consumers
fail loudly on a bind collision rather than corrupting anything).
"""
from __future__ import annotations

import socket
import sys


def free_ports(n: int) -> list[int]:
    socks = []
    try:
        for _ in range(n):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            socks.append(s)
        return [s.getsockname()[1] for s in socks]
    finally:
        for s in socks:
            s.close()


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    if n < 1:
        print("usage: free-port.py [N>=1]", file=sys.stderr)
        return 2
    print(" ".join(str(p) for p in free_ports(n)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
