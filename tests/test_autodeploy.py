import subprocess

import pytest

from sitefloor import autodeploy, update


def test_rebuild_steps():
    rs = autodeploy.rebuild_steps
    assert rs(None) == ["names", "stats", "export"]  # 不知道上次部署哪個 commit：重建便宜的那幾步
    assert rs([]) == []
    assert rs(["app/static/index.html", "README.md"]) == []  # 只改網頁：直接部署
    assert rs(["sitefloor/stats.py"]) == ["stats", "export"]
    assert rs(["curated/building_names.csv"]) == ["names", "export"]
    got = rs(["curated/floor_corrections.csv", "curated/building_names.csv"])
    assert got[0] == "curated" and "osm" in got and "names" not in got and got[-1] == "export"  # osm 會一起建名稱表
    assert "smoke" not in got  # deploy.sh 會跑部署前檢查
    assert rs(["sitefloor/osm.py"]) == []  # 其他建庫程式：等每週更新重建


def _setup(monkeypatch, tmp_path, now="c2", files=None):
    monkeypatch.setattr(autodeploy, "STATE", tmp_path / "deployed.json")
    monkeypatch.setattr(autodeploy, "LOCK", tmp_path / "pipeline.lock")
    monkeypatch.setattr(autodeploy, "SERVE_DIR", tmp_path)
    monkeypatch.setattr(autodeploy, "pull", lambda argv: None)
    monkeypatch.setattr(autodeploy, "head", lambda: now)
    monkeypatch.setattr(autodeploy, "changed_files", lambda old, new: files if old else None)
    monkeypatch.setattr(autodeploy, "subjects", lambda old, new: [f"{new} 改了東西"])
    monkeypatch.setattr(autodeploy, "commit_age", lambda rev: autodeploy.QUIET_S + 1)
    calls = {"steps": [], "deploys": 0, "notes": []}
    monkeypatch.setattr(update, "run_step", lambda s, log, counties=None: calls["steps"].append(s))
    monkeypatch.setattr(update, "notify", lambda title, msg, priority=3: calls["notes"].append(title))
    return calls


def test_deploys_new_commit_once(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, files=["sitefloor/stats.py"])
    autodeploy.save_state(commit="c1")

    def ok():
        calls["deploys"] += 1
        return True, "部署前檢查：通過"

    monkeypatch.setattr(autodeploy, "deploy", ok)
    assert autodeploy.main([]) == 0
    assert calls["steps"] == ["stats", "export"] and calls["deploys"] == 1
    assert autodeploy.load_state()["commit"] == "c2" and calls["notes"] == ["建物查詢平台：已部署"]
    assert autodeploy.main([]) == 0 and calls["deploys"] == 1  # 同一個 commit 不再部署
    assert autodeploy.main(["--force"]) == 0 and calls["deploys"] == 2


def test_failed_deploy_notifies_once(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, files=["app/static/index.html"])
    autodeploy.save_state(commit="c1")
    monkeypatch.setattr(autodeploy, "deploy", lambda: (False, "部署前檢查：失敗"))
    assert autodeploy.main([]) == 1
    st = autodeploy.load_state()
    assert st["commit"] == "c1" and st["failed"] == "c2"
    assert autodeploy.main([]) == 0  # 同一個失敗的 commit 不重試、不重複通知
    assert calls["notes"] == ["建物查詢平台：自動部署失敗"] and calls["steps"] == []


def test_skips_when_locked(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(autodeploy, "deploy", lambda: (True, ""))
    with autodeploy.pipeline_lock(wait=False) as got:
        assert got
        assert autodeploy.main([]) == 0  # 每週更新正在跑：略過
    assert calls["notes"] == []


def test_blocked_pull_notifies_once(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(autodeploy, "pull", lambda argv: "這台電腦的程式有未 commit 的修改")
    assert autodeploy.main([]) == 1
    assert autodeploy.main([]) == 1
    assert calls["notes"] == ["建物查詢平台：無法自動部署"]


def test_waits_for_quiet_period(monkeypatch, tmp_path):
    calls = _setup(monkeypatch, tmp_path, files=["app/static/index.html"])
    autodeploy.save_state(commit="c1")
    monkeypatch.setattr(autodeploy, "commit_age", lambda rev: 60)  # 剛 push

    def ok():
        calls["deploys"] += 1
        return True, ""

    monkeypatch.setattr(autodeploy, "deploy", ok)
    assert autodeploy.main([]) == 0
    assert calls["deploys"] == 0 and not calls["notes"]  # 先不部署，也不通知
    assert autodeploy.main(["--force"]) == 0
    assert calls["deploys"] == 1  # --force 不等


def _repos(tmp_path, monkeypatch):
    """GitHub（bare repo）與這台電腦的 clone；回傳 (遠端路徑, clone 路徑, 跑 git 的函式)。"""
    def g(cwd, *a):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    remote, work = tmp_path / "remote.git", tmp_path / "work"
    g(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    g(tmp_path, "clone", "-q", str(remote), str(work))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        g(work, "config", k, v)
    (work / "a.txt").write_text("1")
    g(work, "add", "a.txt")
    g(work, "commit", "-q", "-m", "c1")
    g(work, "push", "-q", "origin", "main")
    monkeypatch.setattr(autodeploy, "ROOT", work)
    monkeypatch.chdir(tmp_path)  # pull() 會 chdir 到 ROOT；測試結束時還原工作目錄
    monkeypatch.setattr(autodeploy.shutil, "which", lambda _: None)

    def execv(*a):
        raise SystemExit("execv")  # 有新程式時會重新執行自己

    monkeypatch.setattr(autodeploy.os, "execv", execv)
    return remote, work, g


def test_pull_reports_unreachable_remote(tmp_path, monkeypatch):
    remote, _, _ = _repos(tmp_path, monkeypatch)
    remote.rename(tmp_path / "gone.git")  # 等同網路斷線、金鑰失效
    got = autodeploy.pull([])
    assert got and got.startswith("git pull 失敗")


def test_pull_aligns_rewritten_history(tmp_path, monkeypatch):
    remote, work, g = _repos(tmp_path, monkeypatch)
    other = tmp_path / "other"
    g(tmp_path, "clone", "-q", str(remote), str(other))
    g(other, "-c", "user.email=new@t", "-c", "user.name=t", "commit", "-q", "--amend", "--no-edit", "--reset-author")
    g(other, "push", "-q", "-f", "origin", "main")  # 改作者、內容相同
    with pytest.raises(SystemExit, match="execv"):
        autodeploy.pull([])
    assert g(work, "rev-parse", "HEAD") == g(other, "rev-parse", "HEAD")
