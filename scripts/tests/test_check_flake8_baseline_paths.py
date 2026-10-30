"""Path normalisation used when attributing a Flake8 row to a file."""

import unittest
from pathlib import Path

from scripts.check_flake8_baseline import _normalise_path


class NormalisePathTests(unittest.TestCase):
    def test_relative_path_is_rendered_posix(self):
        self.assertEqual(_normalise_path("itambox/core/models.py", Path.cwd()), "itambox/core/models.py")

    def test_absolute_path_inside_the_working_directory_becomes_relative(self):
        cwd = Path.cwd()
        self.assertEqual(_normalise_path(str(cwd / "itambox" / "core" / "models.py"), cwd), "itambox/core/models.py")

    def test_absolute_path_outside_the_working_directory_is_kept(self):
        outside = Path(Path.cwd().anchor) / "elsewhere" / "module.py"
        self.assertEqual(_normalise_path(str(outside), Path.cwd()), outside.as_posix())


if __name__ == "__main__":
    unittest.main()
