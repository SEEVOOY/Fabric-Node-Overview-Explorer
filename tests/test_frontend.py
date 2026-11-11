"""The page's pure JavaScript (layout, threads) has its own tests: run them when Node is installed."""
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js isn't installed")
def test_column_lineage_page_modules():
    root = Path(__file__).resolve().parent.parent
    files = sorted(str(f) for f in (root / "tests" / "js").glob("*.test.mjs"))
    r = subprocess.run(["node", "--test", *files], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
