import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def test_help_works_when_command_is_symlinked_outside_repository(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            link = Path(temp_dir) / "af"
            os.symlink(ROOT / "bin" / "af", link)

            result = subprocess.run([str(link), "--help"], text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout.lower())


if __name__ == "__main__":
    unittest.main()
