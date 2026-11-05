"""定期更新：偵測各資料來源有沒有新版 → 只下載有變的 → 重跑受影響的步驟 → 產出異動報告 → 通知。

- 有新版就自動下載並重建：實價登錄（本期、新的季檔）、臺北市實價週報、OpenStreetMap、使用執照。
- 只偵測、不自動重建（要手動處理，報告裡會列出）：各縣市門牌點位、臺中／臺北建物圖。
- 異動報告：比對更新前後的實價樓層、使照、OSM 建物、涵蓋率 → data/reports/<日期>.md
- 不會自動部署：看過報告後自己跑 `vercel deploy --prod --yes`。

用法：
  uv run python -m sitefloor.update            # 檢查並更新
  uv run python -m sitefloor.update --check    # 只檢查哪些來源有新版，不下載
  uv run python -m sitefloor.update --force    # 不管有沒有新版，全部重跑
  uv run python -m sitefloor.update --setup-notify   # 建立 ntfy 手機推播頻道（data/state/notify.json）
  uv run python -m sitefloor.update --test-notify    # 發一則測試推播
排程：每週一 09:00 由 launchd 執行（scripts/install_schedule.sh）。
"""

import datetime as dt
import json
import re
import ssl
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path

import certifi
import duckdb

from sitefloor.db import connect

ROOT = Path(__file__).resolve().parent.parent
STATE_FILE = ROOT / "data" / "state" / "sources.json"
PREV_DIR = ROOT / "data" / "state" / "prev"
REPORT_DIR = ROOT / "data" / "reports"
RAW = ROOT / "data" / "raw"
UA = {"User-Agent": "Mozilla/5.0 (site-investigation updater)"}
CTX = ssl.create_default_context(cafile=certifi.where())  # macOS 內建憑證缺 TWCA

PLVR_CURRENT = "https://plvr.land.moi.gov.tw/opendata/lvr_landAcsv.zip"
PLVR_SEASON = "https://plvr.land.moi.gov.tw/DownloadSeason?season={s}&type=zip&fileName=lvr_landcsv.zip"
OSM_PBF = "https://download.geofabrik.de/asia/taiwan-latest.osm.pbf"
# 使用執照：檔名 → (data.gov.tw 資料集編號, 下載網址)。
# 版本用 data.gov.tw 的詮釋資料更新時間（0.3 秒）；data.taipei／新北對 HEAD 要卡 60 秒
PERMITS = {
    "tp_use_115.xml": (
        128199,
        (
            "https://data.taipei/api/dataset/0816f991-e6c8-4da0-a789-d022fee1462b/resource/"
            "74a8b311-906e-44c4-91fb-7f413842d4e1/download"
        ),
    ),
    "tp_use_hist.xml": (
        128203,
        (
            "https://data.taipei/api/dataset/c876ff02-af2e-4eb8-bd33-d444f5052733/resource/"
            "0f3f9675-8356-4f1a-9908-1ce8892012fa/download"
        ),
    ),
    "ntpc_permit.csv": (123639, "https://data.ntpc.gov.tw/api/datasets/c1487d7b-fff1-43d3-a2ce-4716eab4d286/csv/file"),
    "hcc_use.csv": (
        163823,
        ("https://odws.hccg.gov.tw/001/Upload/25/opendataback/9059/948/15ac6848-42ca-47ee-a606-324affd3a6b6.csv"),
    ),
    "tn_apt.csv": (120168, "https://data.tainan.gov.tw/File/ResourceCsvDownload/c6c8ee63-c0dc-4721-a43a-043139e4fc0e"),
}
TP_WEEKLY_DS = 145630
# 只偵測（data.gov.tw 詮釋資料更新時間）：門牌點位、建物圖
WATCH_ONLY = {
    "臺中市建物圖": 158889,
    "臺北市建照套繪圖": 151936,
    "臺中市門牌": 177460,
    "臺北市門牌": 155472,
    "新北市門牌": 168887,
    "桃園市門牌": 157689,
    "臺南市門牌": 120044,
    "高雄市門牌": 172400,
    "新竹市門牌": 157547,
    "新竹縣門牌": 172380,
    "苗栗縣門牌": 176511,
    "彰化縣門牌": 170727,
    "雲林縣門牌": 166201,
    "嘉義縣門牌": 172873,
    "屏東縣門牌": 170847,
    "花蓮縣門牌": 175221,
    "臺東縣門牌": 165619,
    "澎湖縣門牌": 170852,
    "金門縣門牌": 171571,
}
# 步驟的執行順序；trades 之後要重算所有衍生表
PIPELINE = ["trades", "derive", "curated", "permits", "osm", "risk", "coverage", "gaps", "export",
            "smoke"]  # osm 用到 permits；risk 用到 trades（主要用途）
NEEDS = {
    "trades": {"trades", "derive", "curated", "osm", "permits", "risk", "coverage", "gaps", "export", "smoke"},
    "curated": {"curated", "osm", "coverage", "gaps", "export", "smoke"},
    "osm": {"curated", "osm", "coverage", "gaps", "export", "smoke"},
    "permits": {"permits", "osm", "coverage", "gaps", "export", "smoke"},
}


# ── 網路 ──
def _open(url: str, method: str = "GET", timeout: int = 120):
    req = urllib.request.Request(url, headers=UA, method=method)
    return urllib.request.urlopen(req, timeout=timeout, context=CTX)


def head_token(url: str, tries: int = 2) -> str | None:
    """用 HEAD 的 ETag／Last-Modified／Content-Length 當版本記號；拿不到回 None（逾時重試一次）。"""
    for _ in range(tries):
        try:
            with _open(url, "HEAD", 30) as r:
                h = r.headers
                parts = [h.get("ETag"), h.get("Last-Modified"), h.get("Content-Length")]
                return "|".join(p for p in parts if p) or None
        except OSError:
            continue
    return None


def download(url: str, dest: Path):
    """下載到暫存檔再搬過去（避免下載中斷留下半個檔）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _open(url, timeout=1800) as r, open(tmp, "wb") as f:
        while chunk := r.read(1 << 20):
            f.write(chunk)
    tmp.replace(dest)


def datagov_modified(ds_id: int) -> str | None:
    try:
        with _open(f"https://data.gov.tw/api/v2/rest/dataset/{ds_id}", timeout=30) as r:
            meta = json.load(r).get("result", {})
        return meta.get("metadataModifiedDate") or meta.get("modifiedDate") or meta.get("updatedDate")
    except (OSError, ValueError):
        return None


# ── 季別 ──
def _next_season(s: str) -> str:
    y, q = int(s[:-2]), int(s[-1])
    return f"{y + 1}S1" if q == 4 else f"{y}S{q + 1}"


def new_seasons() -> list[str]:
    """本機最新季之後，網站已提供的季（下載檔要大於 1 MB 才算有）。"""
    have = sorted((p.stem for p in (RAW / "plvr" / "seasons").glob("*S*.zip")), key=lambda s: (int(s[:-2]), s[-1]))
    out, s = [], _next_season(have[-1]) if have else "101S4"
    for _ in range(4):
        tok = head_token(PLVR_SEASON.format(s=s))
        size = int(re.search(r"(\d+)$", tok).group(1)) if tok and re.search(r"(\d+)$", tok) else 0
        if size < 1_000_000:
            break
        out.append(s)
        s = _next_season(s)
    return out


# ── 檢查與下載 ──
def check_sources(state: dict, force: bool) -> tuple[dict, set, list[str], list[str]]:
    """回傳（新狀態、要跑的步驟、異動說明、只偵測到但要手動處理的）。"""
    new, steps, notes, manual = dict(state), set(), [], []

    def changed(key: str, token: str | None) -> bool:
        return token is not None and (force or state.get(key) != token)

    tok = head_token(PLVR_CURRENT)
    if changed("plvr_current", tok):
        notes.append(f"實價登錄本期有新版（{tok.split('|')[1] if tok and '|' in tok else tok}）")
        steps.add("trades")
        new["plvr_current"] = ("fetch", tok)
    seasons = new_seasons()
    if seasons:
        notes.append(f"實價登錄新的季檔：{'、'.join(seasons)}")
        steps.add("trades")
        new["plvr_seasons"] = ("fetch", seasons)
    tok = datagov_modified(TP_WEEKLY_DS)
    if changed("tp_weekly", tok):
        notes.append(f"臺北市實價週報有新資料（{tok}）")
        steps.add("trades")
        new["tp_weekly"] = tok
    corr = ROOT / "curated" / "floor_corrections.csv"
    tok = str(corr.stat().st_mtime_ns) + ":" + str(corr.stat().st_size) if corr.exists() else None
    if changed("curated", tok):
        notes.append("人工校正樓層表有更新")
        steps.add("curated")
        new["curated"] = tok
    tok = head_token(OSM_PBF)
    if changed("osm", tok):
        notes.append("OpenStreetMap 有新版")
        steps.add("osm")
        new["osm"] = ("fetch", tok)
    for name, (ds, _url) in PERMITS.items():
        tok = datagov_modified(ds)
        if changed(f"permit:{name}", tok):
            notes.append(f"使用執照有新版：{name}（{tok}）")
            steps.add("permits")
            new[f"permit:{name}"] = ("fetch", tok)
    for name, ds in WATCH_ONLY.items():
        tok = datagov_modified(ds)
        key = f"watch:{ds}"
        if tok and state.get(key) and state[key] != tok:
            manual.append(f"{name}（data.gov.tw #{ds}）更新於 {tok}")
        if tok:
            new[key] = tok
    return new, steps, notes, manual


def fetch_changed(new: dict) -> dict:
    """下載標記為 fetch 的來源，回傳只含版本記號的狀態。"""
    out = {}
    for key, val in new.items():
        if not (isinstance(val, tuple) and val[0] == "fetch"):
            out[key] = val
            continue
        tok = val[1]
        if key == "plvr_current":
            download(PLVR_CURRENT, RAW / "plvr" / "lvr_landAcsv.zip")
        elif key == "plvr_seasons":
            for s in tok:
                download(PLVR_SEASON.format(s=s), RAW / "plvr" / "seasons" / f"{s}.zip")
            continue  # 季檔不存版本記號：以本機已有的檔案為準
        elif key == "osm":
            download(OSM_PBF, RAW / "osm" / "taiwan-latest.osm.pbf")
        elif key.startswith("permit:"):
            name = key.split(":", 1)[1]
            download(PERMITS[name][1], RAW / "permits" / name)
        out[key] = tok
    return out


# ── 快照與異動 ──
SNAP = {
    "door_trade": "SELECT county, town, base, floors, n FROM door_trade",
    "permits": "SELECT county, town, base, floors_above, source, permit_no FROM permits",
    "osm": "SELECT uid, name, levels, height FROM osm_buildings",
    "coverage": "SELECT county, doors, high_mid, any_floor FROM coverage",
}


def snapshot(con):
    PREV_DIR.mkdir(parents=True, exist_ok=True)
    for name, sql in SNAP.items():
        try:
            con.execute(f"COPY ({sql}) TO '{PREV_DIR / name}.parquet' (FORMAT parquet)")
        except duckdb.Error as e:  # 表還不存在（例：還沒跑過 osm／permits）
            print(f"  快照略過 {name}：{e}")


def diff_report(con) -> tuple[list[str], int]:
    """比對快照與現在，回傳（報告行、異動門牌數）。"""
    lines, changed = [], 0

    def prev(name):
        p = PREV_DIR / f"{name}.parquet"
        return f"read_parquet('{p}')" if p.exists() else None

    if p := prev("door_trade"):
        new_doors = con.execute(
            f"SELECT count(*) FROM door_trade n ANTI JOIN {p} o USING (county, town, base)"
        ).fetchone()[0]
        moved = con.execute(f"""
            SELECT n.county || n.town || n.base, o.floors, n.floors, n.n
            FROM door_trade n JOIN {p} o USING (county, town, base)
            WHERE n.floors <> o.floors ORDER BY n.n DESC""").fetchall()
        changed += new_doors + len(moved)
        lines += ["## 實價登錄", f"- 第一次有實價樓層的門牌：{new_doors:,}", f"- 樓層改變的門牌：{len(moved):,}"]
        if moved:
            lines += ["", "| 門牌 | 原本 | 現在 | 實價筆數 |", "|---|---|---|---|"]
            lines += [f"| {a} | {o} | {n} | {k} |" for a, o, n, k in moved[:50]]
            if len(moved) > 50:
                lines.append(f"| …另 {len(moved) - 50:,} 筆 | | | |")
        lines.append("")
    if p := prev("permits"):
        added = con.execute(f"SELECT count(*) FROM permits n ANTI JOIN {p} o USING (county, town, base)").fetchone()[0]
        moved = con.execute(f"""
            SELECT n.county || n.town || n.base, o.floors_above, n.floors_above, n.permit_no
            FROM permits n JOIN {p} o USING (county, town, base) WHERE n.floors_above <> o.floors_above""").fetchall()
        changed += added + len(moved)
        lines += ["## 使用執照", f"- 新增門牌：{added:,}", f"- 樓層改變：{len(moved):,}"]
        lines += [f"  - {a}：{o} → {n}（{no}）" for a, o, n, no in moved[:20]]
        lines.append("")
    if p := prev("osm"):
        added = con.execute(f"SELECT count(*) FROM osm_buildings n ANTI JOIN {p} o USING (uid)").fetchone()[0]
        removed = con.execute(f"SELECT count(*) FROM {p} o ANTI JOIN osm_buildings n USING (uid)").fetchone()[0]
        moved = con.execute(f"""
            SELECT n.uid, coalesce(n.name, ''), o.levels, n.levels FROM osm_buildings n JOIN {p} o USING (uid)
            WHERE n.levels IS DISTINCT FROM o.levels""").fetchall()
        lines += ["## OpenStreetMap", f"- 新增建物：{added:,}、移除：{removed:,}", f"- 樓層標註改變：{len(moved):,}"]
        lines += [f"  - {u} {nm}：{o} → {n}" for u, nm, o, n in moved[:20]]
        lines.append("")
    if p := prev("coverage"):
        rows = con.execute(f"""
            SELECT n.county, o.high_mid, n.high_mid, o.any_floor, n.any_floor FROM coverage n JOIN {p} o USING (county)
            WHERE abs(n.high_mid - o.high_mid) >= 0.0005 OR abs(n.any_floor - o.any_floor) >= 0.0005
            ORDER BY n.doors DESC""").fetchall()
        lines += ["## 涵蓋率（高＋中／含低）"]
        lines += [
            f"- {c}：{a * 100:.1f}% → {b * 100:.1f}%／{x * 100:.1f}% → {y * 100:.1f}%" for c, a, b, x, y in rows
        ] or ["- 沒有變化"]
        lines.append("")
    return lines, changed


# ── 執行 ──
def run_step(step: str, log: list[str]):
    args = {"trades": ["sitefloor.trades", "plvr", "taipei_weekly"]}.get(step, [f"sitefloor.{step}"])
    r = subprocess.run([sys.executable, "-m", *args], cwd=ROOT, capture_output=True, text=True, check=False)
    out = "\n".join(line for line in (r.stdout + r.stderr).splitlines() if "%▕" not in line and "Warning" not in line)
    log.append(f"### {step}\n```\n{out.strip()[-3000:]}\n```")
    if r.returncode != 0:
        raise RuntimeError(f"步驟 {step} 失敗（exit {r.returncode}）")


NOTIFY_FILE = (
    ROOT / "data" / "state" / "notify.json"
)  # {"ntfy_topic": "...", "ntfy_server": "https://ntfy.sh"}；不進版控


def notify(title: str, msg: str, priority: int = 3):
    """通知：macOS 通知（本機）＋ ntfy 手機推播（有設定 notify.json 時）。"""
    if sys.platform == "darwin":
        # AppleScript 字串只認 \\ 與 \" 跳脫，不認 \\uXXXX，所以不能用 json.dumps
        def q(x: str) -> str:
            return '"' + x.replace("\\", "\\\\").replace('"', '\\"') + '"'

        first = msg.splitlines()[0] if msg else ""
        subprocess.run(["osascript", "-e", f"display notification {q(first)} with title {q(title)}"], check=False)
    if NOTIFY_FILE.exists():
        cfg = json.loads(NOTIFY_FILE.read_text())
        # JSON 發布：標題、內文可用中文（HTTP header 只能 latin-1）
        body = json.dumps(
            {"topic": cfg["ntfy_topic"], "title": title, "message": msg[:3800], "priority": priority},
            ensure_ascii=False,
        ).encode()
        req = urllib.request.Request(
            cfg.get("ntfy_server", "https://ntfy.sh"),
            data=body,
            method="POST",
            headers={**UA, "Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=30, context=CTX).read()
        except OSError as e:
            print(f"  ntfy 推播失敗：{e}")


def setup_notify():
    """產生隨機（猜不到的）ntfy 頻道名稱，寫進 data/state/notify.json。"""
    import secrets

    if NOTIFY_FILE.exists():
        topic = json.loads(NOTIFY_FILE.read_text())["ntfy_topic"]
        print(f"已設定過：頻道 {topic}")
        return
    topic = f"siteinv-{secrets.token_urlsafe(12).replace('_', '').replace('-', '')}"
    NOTIFY_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTIFY_FILE.write_text(json.dumps({"ntfy_topic": topic, "ntfy_server": "https://ntfy.sh"}, indent=1))
    print(f"已建立推播頻道：{topic}\n手機裝 ntfy App → 訂閱（Subscribe）→ 頻道名稱填上面這串")


def main(argv: list[str]):
    if "--setup-notify" in argv:
        return setup_notify()
    if "--test-notify" in argv:
        return notify("地址樓層查詢：測試", "收到這則就代表推播設定成功")
    force, check_only = "--force" in argv, "--check" in argv
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
    new, steps, notes, manual = check_sources(state, force)
    first_run = not state
    print(
        "\n".join(
            ["檢查結果："] + [f"- {n}" for n in notes or ["沒有新資料"]] + [f"- （要手動更新）{m}" for m in manual]
        )
    )
    if check_only:
        return
    today = dt.date.today().isoformat()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report = REPORT_DIR / f"{today}.md"
    if first_run and not force:
        # 第一次執行只記錄目前各來源的版本，不重建（資料剛建好）
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                {k: (v[1] if isinstance(v, tuple) else v) for k, v in new.items() if k != "plvr_seasons"},
                ensure_ascii=False,
                indent=1,
            )
        )
        print("第一次執行：已記錄各來源目前的版本，之後有新版才會更新")
        return
    if not steps:
        notify("地址樓層查詢", "本週沒有新資料" + (f"；{len(manual)} 項要手動更新" if manual else ""))
        if manual:
            report.write_text(
                f"# {today} 資料更新\n\n沒有可自動更新的新資料。\n\n## 要手動更新\n"
                + "\n".join(f"- {m}" for m in manual)
                + "\n"
            )
        return
    run = [s for s in PIPELINE if any(s in NEEDS[k] for k in steps)]
    log: list[str] = []
    try:
        snapshot(connect())
        saved = fetch_changed(new)
        for s in run:
            print(f"[{s}]")
            run_step(s, log)
        lines, n_changed = diff_report(connect())
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(saved, ensure_ascii=False, indent=1))
        body = [
            f"# {today} 資料更新",
            "",
            "## 偵測到的新資料",
            *[f"- {n}" for n in notes],
            "",
            f"重跑步驟：{' → '.join(run)}",
            "",
            *lines,
        ]
        if manual:
            body += ["## 要手動更新（只偵測、未自動重建）", *[f"- {m}" for m in manual], ""]
        body += [
            "## 下一步",
            "看過異動沒問題後部署：",
            "```",
            "cd ~/Documents/site-investigation && vercel deploy --prod --yes",
            "```",
            "",
            "## 執行紀錄",
            *log,
        ]
        report.write_text("\n".join(body) + "\n")
        print(f"報告：{report}")
        cov = [x for x in lines if x.startswith("- ") and "→" in x and "%" in x][:3]
        summary = [f"{n_changed:,} 個門牌異動", *notes[:4], *cov]
        if manual:
            summary.append(f"另有 {len(manual)} 項要手動更新")
        summary.append(f"報告：data/reports/{today}.md；確認後執行 vercel deploy --prod --yes")
        notify("地址樓層查詢：資料已更新", "\n".join(summary))
    except Exception as e:
        report.write_text(f"# {today} 資料更新失敗\n\n{e}\n\n```\n{traceback.format_exc()}\n```\n\n" + "\n".join(log))
        notify("地址樓層查詢：更新失敗", f"{e}\n報告：data/reports/{today}.md", priority=4)
        raise


if __name__ == "__main__":
    main(sys.argv[1:])
