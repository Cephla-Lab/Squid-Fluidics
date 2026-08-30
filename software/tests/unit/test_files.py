# tests/unit/test_files.py
"""atomic_write: the one way this package replaces a file -- whole or
not at all."""

import os

import pytest

from fluidics.files import atomic_write


class TestAtomicWrite:
    def test_the_block_finishing_is_what_creates_the_file(self, tmp_path):
        path = tmp_path / "out.txt"
        with atomic_write(path) as f:
            f.write("all of it")
            assert not path.exists(), "the name must not appear early"
        assert path.read_text() == "all of it"
        assert os.listdir(tmp_path) == ["out.txt"], "no temp left behind"

    def test_a_failure_leaves_the_standing_file_untouched(self, tmp_path):
        path = tmp_path / "out.txt"
        path.write_text("the earlier content")
        with pytest.raises(OSError, match="disk full"):
            with atomic_write(path) as f:
                f.write("half of")
                raise OSError("disk full")
        assert path.read_text() == "the earlier content"
        assert os.listdir(tmp_path) == ["out.txt"], "no temp left behind"

    def test_a_failure_during_mode_setup_leaks_no_descriptor(
            self, tmp_path, monkeypatch):
        """The temp's descriptor belongs to the handle from the first
        line: a chmod that fails must close it with the unwind, not
        strand it (repeated saves against a bad filesystem would
        exhaust the process's descriptors)."""
        import fluidics.files as files

        def refuses(*args, **kwargs):
            raise PermissionError("chmod refused")

        monkeypatch.setattr(files.os, "fchmod", refuses)
        open_fds = os.listdir("/proc/self/fd")
        for _ in range(3):
            with pytest.raises(PermissionError):
                with atomic_write(tmp_path / "out.txt"):
                    pass
        assert os.listdir("/proc/self/fd") == open_fds, "descriptors leaked"
        assert list(tmp_path.iterdir()) == [], "no temp left behind"

    def test_a_replaced_file_keeps_its_permissions(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("a: 1")
        os.chmod(path, 0o640)
        with atomic_write(path) as f:
            f.write("a: 2")
        assert (os.stat(path).st_mode & 0o777) == 0o640

    def test_a_write_never_flips_the_process_umask(self, tmp_path,
                                                    monkeypatch):
        """Reading the umask means setting it -- there is no getter --
        and that flip is process-wide: a file another thread creates
        inside the window is born unmasked (reproduced at a small switch
        interval). The read happens once at import, never in a write."""
        import fluidics.files as files
        flips = []
        monkeypatch.setattr(files.os, "umask",
                            lambda mask: flips.append(mask) or 0o022)
        with atomic_write(tmp_path / "new.txt") as f:
            f.write("x")
        assert flips == [], "a write flipped the process umask"

    def test_a_new_file_gets_a_plain_opens_mode_not_the_temps(self, tmp_path):
        """mkstemp creates a private 0600; a config or a report the
        operator reads back must get what open() would have given."""
        path = tmp_path / "new.txt"
        with atomic_write(path) as f:
            f.write("x")
        umask = os.umask(0)
        os.umask(umask)
        assert (os.stat(path).st_mode & 0o777) == (0o666 & ~umask)
