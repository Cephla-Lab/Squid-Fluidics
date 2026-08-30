"""One way to replace a file: whole or not at all.

Everything this package writes for keeps -- the hand-maintained config,
a sequence file, a run's report -- goes through atomic_write, so a dump
that dies midway (full disk, a crash, a bad value) can neither truncate
what stood at the path nor leave half a file under its name.
"""

import os
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path


@contextmanager
def atomic_write(path, encoding="utf-8"):
    """A text handle that becomes `path` only if the block finishes.

    The handle is a sibling temp file, os.replace'd onto `path` after
    the block; on any failure the temp is removed, `path` is untouched,
    and the exception goes on. A file being replaced keeps its own
    permissions; a new file gets the mode a plain open() would have
    given it (mkstemp's private 0600 must not stick to a config or a
    report the operator reads back).
    """
    path = Path(path)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".",
                               suffix=".part")
    try:
        if path.exists():
            os.chmod(tmp, os.stat(path).st_mode & 0o777)
        else:
            umask = os.umask(0)
            os.umask(umask)
            os.chmod(tmp, 0o666 & ~umask)
        with open(fd, "w", encoding=encoding) as f:
            yield f
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
