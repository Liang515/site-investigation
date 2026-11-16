import datetime as dt

from sitefloor import usage


def _fake_redis(monkeypatch):
    """記憶體裡的假 Upstash：支援 HINCRBY、EXPIRE、HGETALL、SET NX。背景執行緒改成同步。"""
    store: dict = {}

    def pipeline(cfg, cmds, timeout=2.0):
        out = []
        for c in cmds:
            if c[0] == "HINCRBY":
                h = store.setdefault(c[1], {})
                h[c[2]] = h.get(c[2], 0) + int(c[3])
                out.append(h[c[2]])
            elif c[0] == "HGETALL":
                out.append([x for k, v in store.get(c[1], {}).items() for x in (k, str(v))])
            elif c[0] == "SET":
                store[c[1]] = c[2]
                out.append("OK")
            elif c[0] == "EXISTS":
                out.append(int(c[1] in store))
            else:
                out.append(1)
        return out

    monkeypatch.setattr(usage, "config", lambda: ("https://x", "t"))
    monkeypatch.setattr(usage, "_pipeline", pipeline)
    monkeypatch.setattr(usage.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})())
    return store


def test_lookup_fields():
    f = usage.lookup_fields({"county": "臺北市", "confidence": "中"}, "pick")
    assert f == {"lookup": 1, "lookup:county:臺北市": 1, "lookup:conf:中": 1, "lookup:via:pick": 1}
    # 查不到：記在 none:<縣市>；未知的入口不記
    f = usage.lookup_fields({"county": None, "confidence": "無"}, "<script>")
    assert f == {"lookup": 1, "lookup:county:未知": 1, "lookup:conf:無": 1, "none:未知": 1}


def test_record_and_summary(monkeypatch):
    store = _fake_redis(monkeypatch)
    day = dt.date(2026, 11, 10)
    monkeypatch.setattr(usage, "today", lambda: day)
    usage.record(usage.lookup_fields({"county": "臺北市", "confidence": "高"}, "type"))
    usage.record(usage.lookup_fields({"county": "宜蘭縣", "confidence": "無"}, "pick"))
    usage.record({"building": 1})
    usage.record({"ev:geolocate": 1})
    usage.record({"ev:stats": 1})
    usage.record({"ev:stats": 1})
    assert store["usage:2026-11-10"]["lookup"] == 2
    text = usage.summary(dt.date(2026, 11, 16))
    assert "11/09–11/15" in text and "查地址 2 次" in text
    assert "點地圖建物 1" in text and "定位 1" in text and "統計頁 2" in text and "價格圖 0" in text
    assert "信心：高 50%、無 50%" in text and "查不到最多：宜蘭縣 1" in text


def test_sent_once_per_week(monkeypatch):
    _fake_redis(monkeypatch)
    end = dt.date(2026, 11, 16)
    assert usage.already_sent(end) is False
    usage.mark_sent(end)
    assert usage.already_sent(end) is True
    monkeypatch.setattr(usage, "config", lambda: None)
    assert usage.already_sent(end) is True  # 沒有設定 Upstash：無從記錄，不送


def test_record_without_config_is_noop(monkeypatch):
    monkeypatch.setattr(usage, "config", lambda: None)
    usage.record({"lookup": 1})  # 不丟例外、不連線
