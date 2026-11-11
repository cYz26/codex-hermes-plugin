"""Trusted sandbox entry; resource limits apply only to this isolated process tree."""
import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_AS, (1024**3, 1024**3))
resource.setrlimit(resource.RLIMIT_CPU, (55, 55))
resource.setrlimit(resource.RLIMIT_FSIZE, (1024**2, 1024**2))
resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
os.execv(sys.argv[1], sys.argv[1:])
