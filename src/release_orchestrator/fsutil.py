"""Filesystem helpers for temporary, process-owned directories."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path


def remove_tree(path: Path) -> None:
    """Remove a directory we created. Git marks pack files read-only on Windows."""
    if not path.exists():
        return
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            target = os.path.join(root, name)
            with suppress(OSError):
                os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
    shutil.rmtree(path, ignore_errors=True)


@contextmanager
def temp_dir(prefix: str = "lro-") -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield path
    finally:
        remove_tree(path)
