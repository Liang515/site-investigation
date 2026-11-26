from sitefloor.update import NEEDS, PIPELINE, _next_season


def test_next_season():
    assert _next_season("115S3") == "115S4"
    assert _next_season("115S4") == "116S1"


def test_pipeline_covers_needs():
    for steps in NEEDS.values():
        assert steps <= set(PIPELINE)
    assert "export" in NEEDS["osm"]  # 任何更新都要重新匯出部署用 Parquet


def _git(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                        "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"], "HOME": str(cwd)})


def test_self_update(monkeypatch, tmp_path):
    """有新 commit：pull 後用新程式重新執行；本機有未 commit 修改：不動程式、只記下提醒；沒有新程式：什麼都不做。"""
    import os

    from sitefloor import update

    origin, mini, dev = tmp_path / "origin.git", tmp_path / "mini", tmp_path / "dev"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(dev))
    (dev / "a.txt").write_text("1")
    _git(dev, "add", "."), _git(dev, "commit", "-qm", "1"), _git(dev, "push", "-q", "origin", "HEAD:main")
    _git(tmp_path, "clone", "-q", str(origin), str(mini))
    monkeypatch.setattr(update, "ROOT", mini)
    monkeypatch.setattr(update.shutil, "which", lambda x: "/usr/bin/git" if x == "git" else None)  # 不跑 uv sync
    execs = []
    monkeypatch.setattr(update.os, "execv", lambda exe, args: execs.append(args))
    monkeypatch.setattr(update.os, "chdir", lambda p: None)
    monkeypatch.delenv(update.PULL_ENV, raising=False)

    update.self_update([])  # 沒有新程式
    assert execs == [] and update.PULL_ENV not in os.environ

    (dev / "a.txt").write_text("2")
    _git(dev, "commit", "-qam", "2"), _git(dev, "push", "-q", "origin", "HEAD:main")
    update.self_update(["--force"])
    assert (mini / "a.txt").read_text() == "2"
    assert execs and execs[0][-2:] == ["--force", "--no-pull"]
    assert "1 個 commit" in os.environ[update.PULL_ENV]

    (mini / "a.txt").write_text("local edit")
    execs.clear()
    update.self_update([])
    assert execs == [] and "未 commit" in os.environ[update.PULL_ENV]


def test_addr_auto_rebuild(monkeypatch, tmp_path):
    """只有門牌點位的縣市（新北）有新版：自動下載、重建；臺北門牌（和建物圖一起建）仍列為手動。"""
    from sitefloor import update

    monkeypatch.setattr(update, "head_token", lambda url, tries=2: "same")
    monkeypatch.setattr(update, "new_seasons", lambda: [])
    monkeypatch.setattr(update, "ROOT", tmp_path)  # 沒有人工校正表
    ver = {ds: "v1" for ds in update.WATCH_ONLY.values()}
    state = {"plvr_current": "same", "osm": "same", **{f"watch:{ds}": v for ds, v in ver.items()}}
    state.update({f"permit:{n}": "v1" for n in update.PERMITS}, tp_weekly="v1")
    ver[update.WATCH_ONLY["新北市門牌"]] = ver[update.WATCH_ONLY["臺北市門牌"]] = "v2"
    monkeypatch.setattr(update, "datagov_modified", lambda ds: ver.get(ds, "v1"))

    new, steps, notes, manual = update.check_sources(state, force=False)
    assert steps == {"addr"} and update.addr_counties(new) == ["新北市"]
    assert any("新北市門牌" in n for n in notes) and any("臺北市門牌" in m for m in manual)
    run = [s for s in update.PIPELINE if any(s in update.NEEDS[k] for k in steps)]
    assert run[:2] == ["addr", "trades"] and run[-2:] == ["export", "smoke"]

    fetched = []
    monkeypatch.setattr(update, "fetch_addr", fetched.append)
    saved = update.fetch_changed(new)
    assert fetched == ["新北市"] and saved[f"watch:{update.WATCH_ONLY['新北市門牌']}"] == "v2"
    # 沒有縣市要重建時不跑 sitefloor.build（不帶縣市會重建全部）
    called = []
    monkeypatch.setattr(update.subprocess, "run", lambda *a, **k: called.append(a))
    update.run_step("addr", [], [])
    assert called == []


def test_hazard_watch_only(monkeypatch, tmp_path):
    """災害潛勢圖資有新版：只列為手動，不重跑任何步驟。"""
    from sitefloor import update

    monkeypatch.setattr(update, "head_token", lambda url, tries=2: "same")
    monkeypatch.setattr(update, "new_seasons", list)
    monkeypatch.setattr(update, "ROOT", tmp_path)
    state = {"plvr_current": "same", "osm": "same", "tp_weekly": "v1",
             **{f"watch:{ds}": "v1" for ds in update.WATCH_ONLY.values()},
             **{f"permit:{n}": "v1" for n in update.PERMITS}}
    flood = update.WATCH_ONLY["淹水潛勢圖"]
    monkeypatch.setattr(update, "datagov_modified", lambda ds: "v2" if ds == flood else "v1")

    new, steps, notes, manual = update.check_sources(state, force=False)
    assert steps == set() and notes == []
    assert any("淹水潛勢圖" in m for m in manual) and new[f"watch:{flood}"] == "v2"


def test_fetch_addr_replaces_only_on_success(monkeypatch, tmp_path):
    import json

    import pytest

    from sitefloor import fetch_addr

    monkeypatch.setattr(fetch_addr, "OUT", tmp_path)
    (tmp_path / "新北市").mkdir()
    (tmp_path / "新北市" / "00.csv").write_text("old")
    meta = {"result": {"distribution": [
        {"resourceFormat": "CSV", "resourceDescription": "114年門牌", "resourceDownloadUrl": "https://x/114.csv"},
        {"resourceFormat": "CSV", "resourceDescription": "115年門牌", "resourceDownloadUrl": "https://x/115.csv"},
        {"resourceFormat": "JSON", "resourceDescription": "115年門牌", "resourceDownloadUrl": "https://x/115.json"}]}}
    files = {"https://x/115.csv": b"new"}
    monkeypatch.setattr(fetch_addr, "get", lambda url, timeout=600: json.dumps(meta).encode() if "api" in url
                        else files[url])
    fetch_addr.fetch("新北市")  # 只取最新年度的 CSV
    assert [p.read_text() for p in (tmp_path / "新北市").iterdir()] == ["new"]
    files["https://x/115.csv"] = b""  # 下載到空檔：丟例外，舊檔不動
    with pytest.raises(RuntimeError):
        fetch_addr.fetch("新北市")
    assert (tmp_path / "新北市" / "00.csv").read_text() == "new"


def test_manual_only_is_reported_once(monkeypatch, tmp_path):
    """只有要手動更新的項目：記下新版本，下週不再重複提醒。"""
    import json

    from sitefloor import update

    monkeypatch.setattr(update, "STATE_FILE", tmp_path / "sources.json")
    monkeypatch.setattr(update, "REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr(update, "run_audit", lambda log: "")
    notes = []
    monkeypatch.setattr(update, "notify", lambda title, msg, priority=3: notes.append(msg))
    update.STATE_FILE.write_text(json.dumps({"watch:1": "v1", "osm": "a"}))
    result = {"manual": ["淹水潛勢圖更新於 v2"]}

    def check(state, force):
        manual = result["manual"] if state["watch:1"] == "v1" else []
        return {**state, "watch:1": "v2"}, set(), [], manual

    monkeypatch.setattr(update, "check_sources", check)
    update.run_update(force=False, check_only=False)
    update.run_update(force=False, check_only=False)
    assert json.loads(update.STATE_FILE.read_text())["watch:1"] == "v2"
    assert ["1 項要手動更新" in n for n in notes] == [True, False]


def test_taipei_weekly_rejects_non_csv(monkeypatch, tmp_path):
    import io

    import pytest

    from sitefloor import trades

    monkeypatch.setattr(trades, "TP_WEEKLY_DIR", tmp_path)
    body = {"raw": b"<html>503 Service Unavailable</html>"}
    monkeypatch.setattr(trades.urllib.request, "urlopen", lambda req, timeout, context: io.BytesIO(body["raw"]))
    with pytest.raises(RuntimeError):
        trades.fetch_taipei_weekly()
    assert list(tmp_path.iterdir()) == []  # 錯誤頁不存成快照
    body["raw"] = "\ufeffCASE_T,DISTRICT,LOCATION,SDATE\n買賣,大安區,x,1150101\n".encode()
    assert trades.fetch_taipei_weekly().read_bytes() == body["raw"]
