"""下載各縣市門牌點位開放資料（data.gov.tw）→ data/raw/addr/<縣市>/

每個資料集優先抓 CSV；沒有 CSV 才抓 XLSX，再沒有才抓 JSON。
"""
import json
import re
import ssl
import sys
import urllib.request

import certifi
from pathlib import Path

DATASETS = {
    "新北市": 168887, "桃園市": 157689, "臺南市": 120044, "高雄市": 172400,
    "新竹市": 157547, "新竹縣": 172380, "苗栗縣": 176511, "彰化縣": 170727,
    "雲林縣": 166201, "嘉義縣": 172873, "屏東縣": 170847, "花蓮縣": 175221,
    "臺東縣": 165619, "澎湖縣": 170852, "金門縣": 171571,
}
OUT = Path("data/raw/addr")
UA = {"User-Agent": "Mozilla/5.0"}
# macOS 內建憑證檔較舊（缺 TWCA CYBER Root CA），改用 Mozilla 最新清單
CTX = ssl.create_default_context(cafile=certifi.where())


def get(url: str, timeout: int = 600) -> bytes:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout, context=CTX) as r:
        return r.read()


def main(names: list[str]):
    for county in names or DATASETS:
        meta = json.loads(get(f"https://data.gov.tw/api/v2/rest/dataset/{DATASETS[county]}"))["result"]
        dist = meta["distribution"]
        for fmt in ("CSV", "XLSX", "JSON", ""):
            picked = [d for d in dist if d["resourceFormat"].upper() == fmt]
            if picked:
                break
        # 同一資料集常附歷年／各月版本：描述有「NNN年(M月)」時只取最新一版
        def ver(res):
            m = re.search(r"(\d{2,3})年(?:(\d{1,2})月)?", res["resourceDescription"])
            return (int(m.group(1)), int(m.group(2) or 0)) if m else None
        vers = [ver(r) for r in picked]
        if all(vers) and len(set(vers)) > 1:
            newest = max(vers)
            picked = [r for r, v in zip(picked, vers) if v == newest]
        d = OUT / county
        d.mkdir(parents=True, exist_ok=True)
        ext = {"CSV": "csv", "XLSX": "xlsx", "JSON": "json"}.get(fmt) or picked[0]["resourceDownloadUrl"].rsplit(".", 1)[-1]
        for i, res in enumerate(picked):
            p = d / f"{i:02d}.{ext}"
            if p.exists() and p.stat().st_size > 0:
                continue
            try:
                p.write_bytes(get(res["resourceDownloadUrl"].strip()))
                print(f"{county} {p.name} {p.stat().st_size:,}B  {res['resourceDescription'][:40]}")
            except Exception as e:
                print(f"{county} {p.name} 失敗：{e}")


if __name__ == "__main__":
    main(sys.argv[1:])
