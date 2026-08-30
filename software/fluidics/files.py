"""One way to replace a file: whole or not at all.

Everything this package writes for keeps -- the hand-maintained config,
a sequence file, a run's report -- goes through atomic_write, so a dump
that dies midway (full disk, a crash, a bad value) can neither truncate
what stood at the path nor leave half a file under its name. The crash
covered here is the process's own; surviving a power cut mid-write is
the filesystem's business, not this module's.
"""

import os
import tempfile
from contextlib import contextmanager, suppress
from pathlib import Path

# Read once, here, rather than per write: reading the umask means setting
# it (there is no getter), and that flip is process-wide -- a file another
# thread creates inside the window is born unmasked. Import runs on the
# main thread before the GUI loop or a report writer exists; a run's
# report is written off-thread while the Qt thread may be saving a config.
_UMASK = os.umask(0)
os.umask(_UMASK)


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
        # The handle owns the descriptor from the first line: a failure
        # anywhere below -- the mode setup included -- must close it, not
        # strand it open while the name is unlinked.
        with open(fd, "w", encoding=encoding) as f:
            try:
                mode = os.stat(path).st_mode & 0o777
            except FileNotFoundError:
                mode = 0o666 & ~_UMASK
            os.fchmod(f.fileno(), mode)
            yield f
        os.replace(tmp, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmp)
        raise
