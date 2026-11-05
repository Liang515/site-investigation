"""審核樓層回報：把私人 repo 裡標了「採用」的 GitHub Issue 寫進 curated/floor_corrections.csv。

流程：網站收到回報 → 開 Issue（GitHub 寄信通知）→ 你查證後加標籤「採用」（不採用就直接關閉）
→ 本機執行這支：寫進校正表、重建（curated → osm → coverage → gaps → export → smoke）、留言並關閉 Issue
→ 確認沒問題後 vercel deploy --prod --yes。

用法：
  uv run python -m sitefloor.review           處理標了「採用」的回報
  uv run python -m sitefloor.review --list    只列出還沒處理的回報
  uv run python -m sitefloor.review --dry-run 只顯示會寫進校正表的內容
需要：已登入的 gh（gh auth status），data/state/notify.json 裡的 github_repo（例："帳號/site-investigation-feedback"）。
"""

import csv
import json
import subprocess
import sys
import time

from sitefloor import curated
from sitefloor.address import parse
from sitefloor.feedback import APPROVE_LABEL, apply_comments, parse_issue
from sitefloor.update import NOTIFY_FILE, ROOT, run_step

REBUILD = ["curated", "osm", "coverage", "gaps", "export", "smoke"]


def repo() -> str:
    cfg = json.loads(NOTIFY_FILE.read_text()) if NOTIFY_FILE.exists() else {}
    if not cfg.get("github_repo"):
        sys.exit(f"{NOTIFY_FILE} 沒有 github_repo（例：\"帳號/site-investigation-feedback\"）")
    return cfg["github_repo"]


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        sys.exit(f"gh {' '.join(args[:2])} 失敗：{r.stderr.strip()}")
    return r.stdout


def issues(rp: str, label: str | None) -> list[dict]:
    args = ["issue", "list", "-R", rp, "--state", "open", "--limit", "200", "--json", "number,title,body,labels,url,comments"]
    return json.loads(gh(*args, *(["--label", label] if label else [])))


def read(issue: dict) -> tuple[dict | None, str]:
    """讀 Issue 的回報並套用審核留言的修正。讀不到時回傳 (None, 原因)。"""
    fb = parse_issue(issue["body"] or "")
    if not fb:
        return None, "讀不到回報內容（JSON 被改壞了？）"
    try:
        fb, used = apply_comments(fb, issue.get("comments") or [])
    except ValueError as e:
        return None, f"留言修正後不合理（{e}）"
    for u in used:
        print(f"  #{issue['number']} 套用留言修正：{u}")
    return fb, ""


def to_row(fb: dict, number: int) -> dict:
    a = parse(fb["address"])
    basis = fb["basis"] or "未填依據"
    return {"county": a.county or "", "town": a.town or "", "base": a.base or fb["address"], "osm_uid": "",
            "floors_above": str(fb["floors_above"]),
            "floors_below": "" if fb["floors_below"] is None else str(fb["floors_below"]),
            "source": f"使用者回報（回報 #{number}）：{basis}", "note": "使用者回報", "checked": time.strftime("%Y-%m-%d")}


def _key(r: dict) -> tuple:
    return r["county"], r["town"], r["base"]


def merge(rows: list[dict]) -> tuple[int, int]:
    """寫進校正表；同一門牌已經有校正就換成新的。回傳 (新增, 取代)。"""
    with open(curated.CSV, encoding="utf-8", newline="") as f:
        old = list(csv.DictReader(f))
    new_keys = {_key(r) for r in rows}
    kept = [r for r in old if not r.get("base") or _key(r) not in new_keys]
    with open(curated.CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=curated.COLUMNS)
        w.writeheader()
        w.writerows(kept + rows)
    replaced = len(old) - len(kept)
    return len(rows) - replaced, replaced


def main(argv: list[str]):
    rp = repo()
    if "--list" in argv:
        todo = issues(rp, None)
        if not todo:
            print("沒有待處理的回報")
        for i in todo:
            fb, _ = read(i)
            mark = "✓ 已標採用" if any(lb["name"] == APPROVE_LABEL for lb in i["labels"]) else "待審核"
            line = f"地上 {fb['floors_above']} 層，依據：{fb['basis'] or '未填'}" if fb else "（格式無法讀取）"
            print(f"#{i['number']} [{mark}] {i['title']}｜{line}\n   {i['url']}")
        return

    approved, rows, bad = issues(rp, APPROVE_LABEL), [], []
    for i in approved:
        fb, why = read(i)
        if fb:
            rows.append((i, to_row(fb, i["number"])))
        else:
            bad.append((i, why))
    for i, why in bad:
        print(f"#{i['number']} {why}，略過：{i['url']}")
    if not rows:
        print(f"沒有標了「{APPROVE_LABEL}」的回報")
        return
    for i, r in rows:
        below = f"、地下 {r['floors_below']} 層" if r["floors_below"] else ""
        print(f"#{i['number']} {r['county']}{r['town']}{r['base']} → 地上 {r['floors_above']} 層{below}")
    if "--dry-run" in argv:
        return

    added, replaced = merge([r for _, r in rows])
    print(f"校正表：新增 {added}、取代 {replaced} → {curated.CSV.relative_to(ROOT)}")
    log: list[str] = []
    for step in REBUILD:
        print(f"  {step}…", flush=True)
        try:
            run_step(step, log)
        except RuntimeError as e:
            print("\n".join(log[-1:]))
            sys.exit(f"{e}；校正表已寫入但 Issue 沒有關閉，修好後再跑一次即可")
    for i, _ in rows:
        gh("issue", "close", str(i["number"]), "-R", rp, "--comment", "已寫進校正表，下次部署後生效。")
    print(f"完成，關閉 {len(rows)} 個 Issue。確認後部署：vercel deploy --prod --yes")


if __name__ == "__main__":
    main(sys.argv[1:])
