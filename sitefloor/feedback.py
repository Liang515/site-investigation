"""樓層回報的格式：網站收到回報 → 開成私人 repo 的 GitHub Issue；審核指令（sitefloor.review）再讀回來。

Issue 內文的使用者輸入全部放進程式碼區塊，避免 @提及、連結、Markdown 被解讀；
另附一段 JSON 讓審核指令讀，不靠解析顯示用的文字。
"""

import json
import re

MARKER = "sitefloor-feedback"
APPROVE_LABEL = "採用"


def clean(x, n: int) -> str:
    """去掉控制字元並截斷長度。"""
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(x or "")).strip()[:n]


_BUILDING_UID = re.compile(r"osm:r?\d{1,12}|tp:\d{1,7}|tc:\d{1,8}")


def building_uid(x) -> str:
    """地圖上點的建物編號（OSM 或市府建物圖）；格式不對就當沒有。"""
    x = clean(x, 30)
    return x if _BUILDING_UID.fullmatch(x) else ""


def validate(body: dict) -> dict:
    """檢查回報內容；不合格丟 ValueError（訊息給使用者看）。"""
    addr = clean(body.get("address"), 100)
    try:
        above = int(body.get("floors_above"))
        below = int(body["floors_below"]) if str(body.get("floors_below") or "").strip() else None
    except (TypeError, ValueError):
        raise ValueError("樓層要填整數") from None
    if not addr or not 1 <= above <= 110 or (below is not None and not 0 <= below <= 20):
        raise ValueError("地址必填；地上樓層 1–110、地下樓層 0–20")
    return {"address": addr, "floors_above": above, "floors_below": below,
            "basis": clean(body.get("basis"), 200), "contact": clean(body.get("contact"), 100),
            "shown": clean(body.get("shown"), 20), "building": building_uid(body.get("building"))}


def summary(fb: dict) -> str:
    """純文字摘要（推播、部署紀錄用）。"""
    below = f"、地下 {fb['floors_below']} 層" if fb["floors_below"] is not None else ""
    lines = [f"地址：{fb['address']}", f"網站顯示：{fb['shown'] or '—'} 層", f"回報：地上 {fb['floors_above']} 層{below}",
             f"依據：{fb['basis'] or '未填'}"]
    if fb.get("building"):
        lines.append(f"建物：{fb['building']}（在地圖上點選的建物）")
    if fb["contact"]:
        lines.append(f"聯絡：{fb['contact']}")
    return "\n".join(lines)


def issue(fb: dict) -> dict:
    """GitHub Issue 的標題與內文。"""
    text = summary(fb).replace("`", "'")
    data = json.dumps(fb, ensure_ascii=False).replace("`", "'")
    body = "\n".join([
        "```text", text, "```", "",
        f"審核：查證無誤就加上標籤「{APPROVE_LABEL}」，不採用直接關閉。",
        "要修正就在下面留言再加標籤，例：「改成 18 層」「地下 3 層」「地址：臺北市信義區忠孝東路四段535號」。",
        "之後在本機執行 `uv run python -m sitefloor.review` 寫進校正表。", "",
        f"<!-- {MARKER} -->", "```json", data, "```",
    ])
    return {"title": f"樓層回報：{fb['address']}", "body": body}


def parse_issue(body: str) -> dict | None:
    """從 Issue 內文讀回回報；不是回報格式就回 None。"""
    if MARKER not in body:
        return None
    m = re.search(r"```json\s*(\{.*?\})\s*```", body.split(MARKER, 1)[1], re.DOTALL)
    if not m:
        return None
    try:
        return validate(json.loads(m.group(1)))
    except (ValueError, json.JSONDecodeError):
        return None


TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}  # 只採用這些人的留言修正
_NUM = r"(\d{1,3})"


def corrections(text: str) -> dict:
    """從審核留言讀出修正：地上樓層、地下樓層、地址（沒寫到的不改）。
    「改成 18 層」「地上 18 層」「18F」→ 地上；「地下 3 層」「B3」→ 地下；「沒有地下室」→ 地下 0；「地址：…」→ 地址。"""
    out: dict = {}
    t = text.replace("　", " ")
    if m := re.search(r"地址\s*[:：]\s*([^\s，,。；;]+)", t):
        out["address"] = clean(m.group(1), 100)
        t = t.replace(m.group(0), " ")
    if m := re.search(r"(?:地下|B)\s*" + _NUM, t, re.IGNORECASE):
        out["floors_below"] = int(m.group(1))
        t = t.replace(m.group(0), " ")
    elif re.search(r"(?:沒有|無)地下", t):
        out["floors_below"] = 0
    if m := re.search(r"(?:地上|改成|改為|應為|是)?\s*" + _NUM + r"\s*(?:層|樓|F)", t, re.IGNORECASE):
        out["floors_above"] = int(m.group(1))
    return out


def apply_comments(fb: dict, comments: list[dict]) -> tuple[dict, list[str]]:
    """依時間順序套用可信任的留言修正（後面的蓋前面的）。回傳 (修正後的回報, 套用了哪些留言)。
    修正後不合理（例：地上 0 層）就丟 ValueError。"""
    fb, used = dict(fb), []
    for c in sorted(comments, key=lambda c: c.get("createdAt") or ""):
        if c.get("authorAssociation") not in TRUSTED:
            continue
        fix = corrections(c.get("body") or "")
        if fix:
            fb.update(fix)
            used.append("、".join(f"{k}={v}" for k, v in fix.items()))
    return validate(fb), used
