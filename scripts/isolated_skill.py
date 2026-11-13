"""Trusted sandbox entry; resource limits apply only to this isolated process tree."""
import os
import resource
import sys


def confinement_probe(outside):
    import socket

    def network():
        with socket.socket() as connection:
            connection.bind(("127.0.0.1", 0))

    checks = (
        lambda: os.open(outside, os.O_RDONLY),
        lambda: os.open(".probe-write", os.O_WRONLY | os.O_CREAT),
        network,
        lambda: os.posix_spawn("/bin/bash", ["/bin/bash", "-c", "exit"], {}),
    )
    for check in checks:
        try:
            check()
        except PermissionError:
            continue
        return 1
    return 0


resource.setrlimit(resource.RLIMIT_CPU, (55, 55))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
if sys.platform == "linux":
    resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
    resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
if sys.argv[1] == "--probe":
    sys.exit(confinement_probe(sys.argv[2]))
os.execv(sys.argv[1], sys.argv[1:])
