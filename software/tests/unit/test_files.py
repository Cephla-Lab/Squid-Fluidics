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

    def test_a_replaced_file_keeps_its_permissions(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text("a: 1")
        os.chmod(path, 0o640)
        with atomic_write(path) as f:
            f.write("a: 2")
        assert (os.stat(path).st_mode & 0o777) == 0o640

    def test_a_new_file_gets_a_plain_opens_mode_not_the_temps(self, tmp_path):
        """mkstemp creates a private 0600; a config or a report the
        operator reads back must get what open() would have given."""
        path = tmp_path / "new.txt"
        with atomic_write(path) as f:
            f.write("x")
        umask = os.umask(0)
        os.umask(umask)
        assert (os.stat(path).st_mode & 0o777) == (0o666 & ~umask)
