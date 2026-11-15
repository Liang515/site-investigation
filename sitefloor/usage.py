"""使用統計：每天各功能被用幾次（Upstash Redis，和 popularity.py 共用設定），每週自動推播摘要。

只存「日期 → 各項次數」（Redis hash：usage:<YYYY-MM-DD>，保留 KEEP_DAYS 天），不存地址、IP、裝置或操作順序。
欄位：
- lookup、lookup:via:<入口>、lookup:county:<縣市>、lookup:conf:<信心>、none:<縣市>（查不到樓層）
- building（點地圖建物）、batch、batch_rows（整批查詢次數與筆數）
- ev:<事件>（只發生在瀏覽器的動作，例：定位；EVENTS 以外的名稱不收）
網站：record() 背景送出、逾時就略過，不影響查詢。每週一由 Vercel Cron 呼叫 /api/cron/weekly 推播上週摘要。
本機查看：uv run python -m sitefloor.usage [天數]    立即推播：uv run python -m sitefloor.usage --send
"""

import datetime as dt
import json
import sys
import threading
import urllib.request
from collections import Counter

from sitefloor.popularity import config

PREFIX = "usage:"
SENT_PREFIX = "usage-sent:"
KEEP_DAYS = 400
TZ = dt.timezone(dt.timedelta(hours=8))
VIA = {"type": "輸入地址", "suggest": "建議清單", "pick": "選擇地址", "pillar": "點地圖方柱"}
EVENTS = {"geolocate": "定位"}
CONF = ["高", "中", "低", "待確認", "無"]


def today() -> dt.date:
    return dt.datetime.now(TZ).date()


def _pipeline(cfg: tuple[str, str], cmds: list[list[str]], timeout: float = 2.0) -> list:
    """一次送多個 Redis 指令（Upstash REST 的 /pipeline）。"""
    url, token = cfg
    req = urllib.request.Request(url + "/pipeline", data=json.dumps(cmds).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return [x.get("result") for x in json.loads(r.read())]


def record(fields: dict[str, int]) -> None:
    """今天的各項次數加上去（背景送出，不等結果）。沒有設定 Upstash 時什麼都不做。"""
    cfg = config()
    if not cfg or not fields:
        return
    key = PREFIX + today().isoformat()
    cmds = [["HINCRBY", key, f, str(n)] for f, n in fields.items()] + [["EXPIRE", key, str(KEEP_DAYS * 86400)]]

    def send():
        try:
            _pipeline(cfg, cmds)
        except (OSError, ValueError, KeyError):
            pass

    threading.Thread(target=send, daemon=True).start()


def lookup_fields(r: dict, via: str | None) -> dict[str, int]:
    """一次查地址要加的欄位（r：lookup() 的結果）。"""
    county = r.get("county") or "未知"
    conf = r.get("confidence") or "無"
    f = {"lookup": 1, f"lookup:county:{county}": 1, f"lookup:conf:{conf}": 1}
    if via in VIA:
        f[f"lookup:via:{via}"] = 1
    if conf == "無":
        f[f"none:{county}"] = 1
    return f


def fetch(start: dt.date, days: int) -> Counter:
    """start 起 days 天的各項次數加總。"""
    cfg = config()
    if not cfg:
        return Counter()
    keys = [PREFIX + (start + dt.timedelta(d)).isoformat() for d in range(days)]
    total: Counter = Counter()
    for res in _pipeline(cfg, [["HGETALL", k] for k in keys], timeout=10):
        flat = res or []
        total.update({flat[i]: int(flat[i + 1]) for i in range(0, len(flat), 2)})
    return total


def _top(c: Counter, prefix: str, n: int, names: dict | None = None) -> str:
    items = sorted(((k[len(prefix):], v) for k, v in c.items() if k.startswith(prefix)), key=lambda x: -x[1])[:n]
    return "、".join(f"{(names or {}).get(k, k)} {v:,}" for k, v in items) or "—"


def summary(end: dt.date, days: int = 7) -> str:
    """end（不含）之前 days 天的摘要，附上前一段同長度的比較。"""
    start = end - dt.timedelta(days)
    c, prev = fetch(start, days), fetch(start - dt.timedelta(days), days)
    n, p = c["lookup"], prev["lookup"]
    change = f"（前 {days} 天 {p:,}，{(n - p) / p * 100:+.0f}%）" if p else ""
    conf = "、".join(f"{k} {c[f'lookup:conf:{k}'] / n * 100:.0f}%" for k in CONF if n and c[f"lookup:conf:{k}"]) or "—"
    lines = [
        f"{start:%m/%d}–{(end - dt.timedelta(1)):%m/%d}",
        f"查地址 {n:,} 次{change}",
        f"點地圖建物 {c['building']:,}、整批查詢 {c['batch']:,} 次（{c['batch_rows']:,} 筆）、"
        + "、".join(f"{v} {c['ev:' + k]:,}" for k, v in EVENTS.items()),
        f"查詢方式：{_top(c, 'lookup:via:', 4, VIA)}",
        f"縣市：{_top(c, 'lookup:county:', 5)}",
        f"信心：{conf}",
        f"查不到最多：{_top(c, 'none:', 3)}",
    ]
    return "\n".join(lines)


def already_sent(end: dt.date) -> bool:
    """這週的摘要已經送過（或沒有設定 Upstash，無從記錄）就回 True：排程重送或被重複呼叫時，一週只推一次。"""
    cfg = config()
    if not cfg:
        return True
    return _pipeline(cfg, [["EXISTS", SENT_PREFIX + end.isoformat()]])[0] == 1


def mark_sent(end: dt.date) -> None:
    """記下這週的摘要已經送出。"""
    cfg = config()
    if cfg:
        _pipeline(cfg, [["SET", SENT_PREFIX + end.isoformat(), "1", "EX", str(30 * 86400)]])


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    days = int(args[0]) if args else 7
    if not config():
        sys.exit("沒有設定 Upstash（.env.local 或環境變數），看不到統計")
    text = summary(today() + dt.timedelta(1), days)
    print(text)
    if "--send" in sys.argv:
        from sitefloor.update import notify
        notify("建物查詢平台：使用統計", text)
