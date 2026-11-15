"""下載各縣市門牌點位開放資料（data.gov.tw）→ data/raw/addr/<縣市>/

每個資料集優先抓 CSV；沒有 CSV 才抓 XLSX，再沒有才抓 JSON。
整個縣市的檔案先下載到暫存資料夾，全部成功才換掉舊版（下載失敗時舊檔照舊可用）。
每週更新（update.py）偵測到 data.gov.tw 有新版時會自動呼叫；手動：uv run python -m sitefloor.fetch_addr [縣市 ...]
"""
import json
import re
import shutil
import ssl
import sys
import urllib.request
from pathlib import Path

import certifi

DATASETS = {
    "新北市": 168887, "桃園市": 157689, "臺南市": 120044, "高雄市": 172400,
    "新竹市": 157547, "新竹縣": 172380, "苗栗縣": 176511, "彰化縣": 170727,
    "雲林縣": 166201, "嘉義縣": 172873, "屏東縣": 170847, "花蓮縣": 175221,
    "臺東縣": 165619, "澎湖縣": 170852, "金門縣": 171571,
}
OUT = Path(__file__).resolve().parent.parent / "data" / "raw" / "addr"
UA = {"User-Agent": "Mozilla/5.0"}
# macOS 內建憑證檔較舊（缺 TWCA CYBER Root CA），改用 Mozilla 最新清單
CTX = ssl.create_default_context(cafile=certifi.where())


def get(url: str, timeout: int = 600) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=CTX) as r:
        return r.read()


def pick_resources(dist: list[dict]) -> tuple[str, list[dict]]:
    """資料集的檔案清單 → (格式, 要下載的檔案)：CSV 優先；同一資料集附歷年／各月版本時只取最新一版。"""
    for fmt in ("CSV", "XLSX", "JSON", ""):
        picked = [d for d in dist if d["resourceFormat"].upper() == fmt]
        if picked:
            break

    # 描述有「NNN年(M月)」時只取最新一版
    def ver(res):
        m = re.search(r"(\d{2,3})年(?:(\d{1,2})月)?", res["resourceDescription"])
        return (int(m.group(1)), int(m.group(2) or 0)) if m else None

    vers = [ver(r) for r in picked]
    if all(vers) and len(set(vers)) > 1:
        newest = max(vers)
        picked = [r for r, v in zip(picked, vers) if v == newest]
    return fmt, picked


def fetch(county: str) -> None:
    """下載一個縣市的門牌點位，取代 data/raw/addr/<縣市>/。任何一個檔案失敗就丟例外、舊檔不動。"""
    meta = json.loads(get(f"https://data.gov.tw/api/v2/rest/dataset/{DATASETS[county]}"))["result"]
    fmt, picked = pick_resources(meta["distribution"])
    if not picked:
        raise RuntimeError(f"{county}：資料集沒有可下載的檔案")
    ext = {"CSV": "csv", "XLSX": "xlsx", "JSON": "json"}.get(fmt) or picked[0]["resourceDownloadUrl"].rsplit(".", 1)[-1]
    d = OUT / county
    tmp = d.with_name(f"{county}.new")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    for i, res in enumerate(picked):
        p = tmp / f"{i:02d}.{ext}"
        p.write_bytes(get(res["resourceDownloadUrl"].strip()))
        if not p.stat().st_size:
            raise RuntimeError(f"{county}：{res['resourceDescription'][:40]} 下載到空檔")
        print(f"  {county} {p.name} {p.stat().st_size:,}B  {res['resourceDescription'][:40]}")
    old = d.with_name(f"{county}.old")
    shutil.rmtree(old, ignore_errors=True)
    if d.exists():
        d.rename(old)
    tmp.rename(d)
    shutil.rmtree(old, ignore_errors=True)


def main(names: list[str]):
    for county in names or DATASETS:
        fetch(county)


if __name__ == "__main__":
    main(sys.argv[1:])
