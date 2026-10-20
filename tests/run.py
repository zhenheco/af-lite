import os
import subprocess
import sys
import tempfile


with tempfile.TemporaryDirectory(prefix="af-tests-home-") as home:
    env = dict(os.environ, HOME=home, PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([sys.executable, "-m", "unittest", "discover",
                             "-s", "tests", "-p", "test_*.py", "-v"], env=env)
    raise SystemExit(result.returncode)
