import subprocess
import sys
from pathlib import Path

import pytest

SERVE = Path(__file__).resolve().parent.parent / "data" / "serve"


@pytest.mark.skipif(not SERVE.exists(), reason="沒有部署用 Parquet（先跑 sitefloor.export）")
def test_parquet_smoke():
    # 另開行程：Parquet 模式由環境變數決定，不影響其他測試
    r = subprocess.run([sys.executable, "-m", "sitefloor.smoke"], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
