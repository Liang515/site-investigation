"""查詢熱度：每棟建物被查幾次（Upstash Redis，Vercel Marketplace 安裝後自動設定環境變數）。

只記建物編號與次數（Redis key：count:<建物 uid>），不記 IP、查詢文字或時間。
- 網站：查到建物時 incr(uid)，背景送出、逾時 TIMEOUT 秒、失敗就略過，不影響查詢。沒有設定時什麼都不做。
- 每週更新：fetch_all() 把全部計數拉回 data/state/popularity.json，audit.py 依熱度排序待查清單。
設定（擇一）：KV_REST_API_URL／KV_REST_API_TOKEN（Vercel 安裝 Upstash 時設的）、UPSTASH_REDIS_REST_URL／UPSTASH_REDIS_REST_TOKEN；
本機可用 `vercel env pull .env.local` 拉到專案根目錄的 .env.local（不進版控）。
"""

import json
import os
import re
import threading
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env.local"
OUT_FILE = ROOT / "data" / "state" / "popularity.json"
TIMEOUT = 0.5
PREFIX = "count:"
UID = re.compile(r"^(osm:r?\d+|tp:\d+|tc:\d+)$")


def _env_file() -> dict[str, str]:
    try:
        lines = ENV_FILE.read_text().splitlines()
    except FileNotFoundError:
        return {}
    out = {}
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def config() -> tuple[str, str] | None:
    """(REST 網址, token)；沒有設定回 None。"""
    env = {**_env_file(), **os.environ}
    for u, t in (("KV_REST_API_URL", "KV_REST_API_TOKEN"), ("UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")):
        if env.get(u) and env.get(t):
            return env[u].rstrip("/"), env[t]
    return None


def _call(cfg: tuple[str, str], *cmd: str, timeout: float = TIMEOUT):
    url, token = cfg
    req = urllib.request.Request(url, data=json.dumps(list(cmd)).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())["result"]


def incr(uid: str | None) -> None:
    """建物被查一次（背景送出，不等結果）。只接受建物編號格式，避免任意字串寫進 Redis。"""
    cfg = config()
    if not cfg or not uid or not UID.match(uid):
        return

    def send():
        try:
            _call(cfg, "INCR", PREFIX + uid)
        except (OSError, ValueError, KeyError):
            pass

    threading.Thread(target=send, daemon=True).start()


def fetch_all() -> dict[str, int]:
    """全部計數 {uid: 次數}，寫到 data/state/popularity.json。沒有設定回空 dict。"""
    cfg = config()
    if not cfg:
        return {}
    out: dict[str, int] = {}
    cursor = "0"
    while True:
        cursor, keys = _call(cfg, "SCAN", cursor, "MATCH", PREFIX + "*", "COUNT", "1000", timeout=30)
        if keys:
            vals = _call(cfg, "MGET", *keys, timeout=30)
            out.update({k[len(PREFIX):]: int(v) for k, v in zip(keys, vals) if v is not None})
        if str(cursor) == "0":
            break
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False))
    return out


if __name__ == "__main__":
    got = fetch_all()
    print(f"查詢熱度：{len(got):,} 棟、共 {sum(got.values()):,} 次" if got else "沒有設定 Upstash，或還沒有計數")
