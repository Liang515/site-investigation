import json

from sitefloor import popularity


def test_incr_without_config_is_noop(monkeypatch, tmp_path):
    monkeypatch.setattr(popularity, "ENV_FILE", tmp_path / "none")
    for k in ("KV_REST_API_URL", "KV_REST_API_TOKEN", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    assert popularity.config() is None
    popularity.incr("osm:1")  # 不丟例外、不連線


def test_only_building_uids_are_counted(monkeypatch):
    sent = []
    monkeypatch.setattr(popularity, "config", lambda: ("https://x", "t"))
    monkeypatch.setattr(popularity, "_call", lambda cfg, *cmd, **kw: sent.append(cmd))
    monkeypatch.setattr(popularity.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})())
    for uid in ("osm:123", "osm:r456", "tp:7", "tc:8", "臺北市信義區", "osm:1 OR 1", None):
        popularity.incr(uid)
    assert [c[1] for c in sent] == ["count:osm:123", "count:osm:r456", "count:tp:7", "count:tc:8"]


def test_fetch_all_pages_through_scan(monkeypatch, tmp_path):
    monkeypatch.setattr(popularity, "OUT_FILE", tmp_path / "pop.json")
    monkeypatch.setattr(popularity, "config", lambda: ("https://x", "t"))
    pages = {"0": ["5", ["count:osm:1"]], "5": ["0", ["count:tp:2"]]}  # SCAN 分兩頁
    store = {"count:osm:1": "3", "count:tp:2": "7"}

    def call(cfg, *cmd, **kw):
        return pages[cmd[1]] if cmd[0] == "SCAN" else [store[k] for k in cmd[1:]]

    monkeypatch.setattr(popularity, "_call", call)
    assert popularity.fetch_all() == {"osm:1": 3, "tp:2": 7}
    assert json.loads((tmp_path / "pop.json").read_text()) == {"osm:1": 3, "tp:2": 7}
