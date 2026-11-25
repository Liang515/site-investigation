"""自動部署：GitHub 上有新程式就 pull → 等連續 push 結束 → 重建受影響的資料 → 部署前檢查 → 部署 → 推播結果。

由排程每 5 分鐘執行一次（scripts/install_schedule.sh），放在存放最新資料的那台電腦上；
每週資料更新（update.py）成功後也呼叫 deploy_and_record() 直接部署。
- 和上次部署的 commit（data/state/deployed.json）比對，沒有新 commit 就什麼都不做
- 最新的 commit 推上來滿 QUIET_S 秒、期間沒有更新的 commit 才部署：連續 push 時合併成一次部署
  （Vercel 每次部署都會用掉一些 CPU 時間額度；--force 不等）
- 依改到的檔案決定要重建什麼（rebuild_steps）；改到其他建庫程式時只部署程式，資料等每週更新重建
- 部署失敗只通知一次（同一個 commit 不重試）；修好再 push 新 commit 就會重新部署
- 和每週更新共用一把鎖（pipeline_lock），不會同時寫資料庫

用法：uv run python -m sitefloor.autodeploy           檢查並部署
      uv run python -m sitefloor.autodeploy --force   不管有沒有新 commit、commit 多新都馬上部署
"""

import fcntl
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state" / "deployed.json"  # {"commit": 上次成功部署, "failed": 部署失敗的 commit}
LOCK = ROOT / "data" / "state" / "pipeline.lock"
SERVE_DIR = ROOT / "data" / "serve"
# 改到這些檔案時要重建的步驟（其餘建庫程式的改動等每週更新重建）
NAMES_FILES = {"sitefloor/names.py", "curated/building_names.csv"}
STATS_FILES = {"sitefloor/stats.py"}
EXPORT_FILES = {"sitefloor/export.py"}
QUIET_S = 30 * 60  # 最新 commit 至少這麼久沒有新的 commit 才部署


@contextmanager
def pipeline_lock(wait: bool):
    """資料庫的獨佔鎖；wait=False 時拿不到就回 False（另一個排程正在跑）。"""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def git(*a: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, check=False)


def head() -> str:
    return git("rev-parse", "HEAD").stdout.strip()


def commit_age(rev: str) -> float:
    """commit 距今幾秒（以 commit 時間計；讀不到時當成很久以前）。"""
    t = git("log", "-1", "--format=%ct", rev).stdout.strip()
    return time.time() - int(t) if t.isdigit() else float("inf")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save_state(**kw) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps({**load_state(), **kw}, ensure_ascii=False, indent=1))


def changed_files(old: str | None, new: str) -> list[str] | None:
    """old..new 改到的檔案；不知道上次部署哪個 commit（或不在歷史裡）時回 None。"""
    if not old or git("merge-base", "--is-ancestor", old, new).returncode != 0:
        return None
    return [f for f in git("diff", "--name-only", old, new).stdout.splitlines() if f]


def same_tree(old: str | None, new: str) -> bool:
    """兩個 commit 的檔案內容完全一樣（例如 force push 改寫歷史後）。"""
    return bool(old) and git("diff", "--quiet", old, new).returncode == 0


def rebuild_steps(files: list[str] | None) -> list[str]:
    """要重跑的建庫步驟（照 update.PIPELINE 的順序；names、stats、export 在最後）。files=None：不確定，重建便宜的那幾步。"""
    from sitefloor.update import NEEDS, PIPELINE

    if files is None:
        return ["names", "stats", "export"]
    steps: set[str] = set()
    for f in files:
        if f in NAMES_FILES:
            steps.add("names")
        elif f.startswith("curated/"):  # 人工校正樓層、自訂建物外框：osm 步驟會一起重建名稱表
            steps |= NEEDS["curated"]
        elif f in STATS_FILES:
            steps.add("stats")
        elif f in EXPORT_FILES:
            steps.add("export")
    if "osm" in steps:
        steps.discard("names")
    if steps:
        steps.add("export")
    order = [s for s in PIPELINE if s in steps and s not in ("export", "smoke", "stats")]
    return order + [s for s in ("names", "stats", "export") if s in steps]


def deploy() -> tuple[bool, str]:
    """scripts/deploy.sh（部署前檢查 → 部署 → 刪舊版本）；回傳（成功與否, 輸出最後幾行）。"""
    r = subprocess.run(["zsh", str(ROOT / "scripts" / "deploy.sh")], cwd=ROOT, capture_output=True, text=True,
                       check=False)
    out = [line for line in (r.stdout + r.stderr).splitlines() if line.strip()]
    keep = [line for line in out if "部署前檢查" in line or "資料版本" in line or "Error" in line or "失敗" in line
            or line.startswith("  ")]
    return r.returncode == 0, "\n".join((keep or out)[-12:])


def deploy_and_record() -> tuple[bool, str]:
    """部署目前的程式與資料，記下部署的 commit（每週更新成功後呼叫）。"""
    commit = head()
    ok, tail = deploy()
    if ok:
        save_state(commit=commit, failed=None, at=time.strftime("%Y-%m-%d %H:%M"))
    else:
        save_state(failed=commit)
    return ok, tail


def pull(argv: list[str]) -> str | None:
    """抓 GitHub 上的新程式；有新程式就 uv sync 後用新程式重新執行。回傳不能更新的原因（有的話）。"""
    if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        return "這台電腦的程式有未 commit 的修改，沒有自動更新程式"
    before = head()
    r = git("pull", "--ff-only", "--quiet")
    if r.returncode != 0:
        # GitHub 的歷史被改寫（force push，例如改作者 email）但內容完全一樣：直接對齊 GitHub
        if git("diff", "--quiet", "HEAD", "@{u}").returncode == 0:
            r = git("reset", "--hard", "--quiet", "@{u}")
        if r.returncode != 0:
            return f"git pull 失敗：{(r.stderr or r.stdout).strip()[:200]}"
    if head() != before:
        if uv := shutil.which("uv"):
            subprocess.run([uv, "sync", "--quiet"], cwd=ROOT, check=False)
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "sitefloor.autodeploy", *argv, "--no-pull"])
    return None


def subjects(old: str | None, new: str, n: int = 4) -> list[str]:
    """old..new 的 commit 標題（不知道 old 時只列最新一個）。"""
    rng = [f"{old}..{new}"] if changed_files(old, new) is not None else ["-1", new]
    lines = git("log", "--format=%h %s", *rng).stdout.splitlines()
    return [x[:70] for x in lines[:n]] + ([f"…另有 {len(lines) - n} 個 commit"] if len(lines) > n else [])


def main(argv: list[str]) -> int:
    from sitefloor.update import notify, run_step

    with pipeline_lock(wait=False) as got:
        if not got:
            print("每週更新或另一個部署正在執行，這次略過")
            return 0
        if "--no-pull" not in argv and (problem := pull(argv)):
            st = load_state()
            if st.get("blocked") != problem:  # 同一個問題只通知一次
                notify("建物查詢平台：無法自動部署", problem, priority=4)
                save_state(blocked=problem)
            print(problem)
            return 1
        save_state(blocked=None)
        st, now = load_state(), head()
        if "--force" not in argv and now in (st.get("commit"), st.get("failed")):
            return 0  # 沒有新的 commit：不寫紀錄（每 5 分鐘一次，紀錄會被灌滿）
        if "--force" not in argv and same_tree(st.get("commit"), now):
            save_state(commit=now)  # 歷史改寫過、內容和已部署的一樣：不用重新部署
            return 0
        if "--force" not in argv and commit_age(now) < QUIET_S:
            return 0  # 剛 push：等一陣子沒有新的 commit 再一起部署
        files = changed_files(st.get("commit"), now)
        steps = rebuild_steps(files)
        if not SERVE_DIR.exists() and "export" not in steps:
            steps.append("export")
        log: list[str] = []
        what = subjects(st.get("commit"), now)
        try:
            for s in steps:
                print(f"[{s}]")
                run_step(s, log)
        except RuntimeError as e:
            save_state(failed=now)
            notify("建物查詢平台：自動部署失敗", f"{e}\n" + "\n".join(what) + "\n紀錄：data/logs/autodeploy.log", priority=4)
            print("\n".join(log))
            return 1
        ok, tail = deploy_and_record()
        print(tail)
        rebuilt = f"重建：{'、'.join(steps)}\n" if steps else ""
        if ok:
            notify("建物查詢平台：已部署", f"{now[:7]} 已上線\n{rebuilt}" + "\n".join(what), priority=2)
        else:
            notify("建物查詢平台：自動部署失敗", f"{now[:7]}\n{tail}\n" + "\n".join(what), priority=4)
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
