"""推估用的衍生表（匯入實價後跑一次）。

- door_trade：每個門牌號（county, town, base）的實價總樓層眾數
- trade_doors：door_trade 拆出「路巷弄前綴＋號＋之號」，給同巷推估用
- same_bldg：同棟推估。同一建物多邊形（臺中、臺北），或座標完全相同的門牌（多為主號＋之號），
  取其中有實價門牌的總樓層眾數
- roads：每個鄉鎮市區的路街（門牌去掉巷弄、號），給模糊搜尋比對路名（規則同 suggest.split_base）
- btype：各表都帶實價的建物型態眾數，查詢時用來辨認工廠／倉庫（推估準確率較低）

用法：uv run python -m sitefloor.derive
"""
from sitefloor.db import connect

# 門牌拆路名的規則：路巷弄＋（臨）號＋之號、巷弄從路名尾端拆出（suggest.py 共用）
SUGGEST_DOOR = r"^(.*?)(臨)?(\d+)(?:之(\d+))?號$"
SUGGEST_LANE = r"^(.*?)((?:\d+巷)?(?:\d+弄)?)$"

# 門牌號拆解：前綴（路街巷弄）＋號＋之號
DOOR_RE = r"^(.*?)(\d+)(之(\d+))?號$"

SQL = f"""
-- 總樓層取眾數；平手時優先「有記載所在樓層」的紀錄（整棟出租的紀錄常把
-- 總樓層記錯），再取最近一筆成交、再取較高（DuckDB 的 mode() 平手時每次結果不同）
CREATE OR REPLACE TABLE door_trade AS
WITH t AS (
    SELECT * FROM trades WHERE total_floors IS NOT NULL AND town IS NOT NULL AND base LIKE '%號'
),
c AS (
    SELECT county, town, base, total_floors, count(*) AS k, count(floor) AS kf, max(trade_date) AS d
    FROM t GROUP BY ALL
),
r AS (
    SELECT *, sum(k) OVER w AS n, count(*) OVER w AS nfl,
           row_number() OVER (PARTITION BY county, town, base ORDER BY k DESC, kf DESC, d DESC NULLS LAST, total_floors DESC) AS rn
    FROM c WINDOW w AS (PARTITION BY county, town, base)
),
b AS (SELECT county, town, base, mode(btype) AS btype FROM t GROUP BY ALL)
SELECT r.county, r.town, r.base, r.total_floors AS floors, r.n, r.nfl, b.btype
FROM r JOIN b USING (county, town, base) WHERE rn = 1;

CREATE OR REPLACE TABLE trade_doors AS
SELECT county, town, base, floors, n, btype,
       regexp_extract(base, '{DOOR_RE}', 1) AS prefix,
       CAST(regexp_extract(base, '{DOOR_RE}', 2) AS INTEGER) AS num,
       TRY_CAST(regexp_extract(base, '{DOOR_RE}', 4) AS INTEGER) AS sub
FROM door_trade
WHERE regexp_matches(base, '{DOOR_RE}') AND length(regexp_extract(base, '{DOOR_RE}', 2)) <= 5;

CREATE OR REPLACE TABLE same_bldg AS
WITH door AS (
    SELECT DISTINCT county, town, base, bldg_uid, round(lon, 6) AS x, round(lat, 6) AS y FROM addresses
),
dt AS (SELECT d.*, t.floors, t.btype FROM door d JOIN door_trade t USING (county, town, base)),
g1 AS (
    SELECT bldg_uid, mode(floors) AS f, count(DISTINCT base) AS n, max(floors) - min(floors) AS spread,
           mode(btype) AS btype
    FROM dt WHERE bldg_uid IS NOT NULL GROUP BY 1
),
g2 AS (
    SELECT county, x, y, mode(floors) AS f, count(DISTINCT base) AS n, max(floors) - min(floors) AS spread,
           mode(btype) AS btype
    FROM dt GROUP BY ALL
),
cand AS (
    SELECT d.county, d.town, d.base, g1.f AS floors, g1.n, g1.spread, g1.btype, '同棟（建物圖）' AS method
    FROM door d JOIN g1 USING (bldg_uid)
    UNION ALL
    SELECT d.county, d.town, d.base, g2.f, g2.n, g2.spread, g2.btype, '同點位門牌'
    FROM door d JOIN g2 USING (county, x, y)
)
SELECT * FROM cand
QUALIFY row_number() OVER (PARTITION BY county, town, base ORDER BY spread, n DESC) = 1;

CREATE OR REPLACE TABLE roads AS
WITH d AS (
    SELECT DISTINCT county, town, base FROM addresses WHERE town IS NOT NULL
    UNION SELECT county, town, base FROM door_trade
),
p AS (
    SELECT county, town, regexp_extract(base, '{SUGGEST_DOOR}', 1) AS prefix
    FROM d WHERE regexp_matches(base, '{SUGGEST_DOOR}')
)
SELECT county, town, road, count(*) AS n
FROM (SELECT county, town, regexp_extract(prefix, '{SUGGEST_LANE}', 1) AS road FROM p)
-- 開頭就是縣市或區名的，是實價登錄沒拆乾淨的地址，不算路名
WHERE road <> '' AND NOT starts_with(road, county) AND NOT starts_with(road, town)
  AND NOT regexp_matches(road, '[．、,，~～\\-]|\\d$')  -- 「266．268號」這類合併門牌拆出的殘段
GROUP BY 1, 2, 3;
"""

if __name__ == "__main__":
    con = connect(write=True)
    con.execute(SQL)
    for t in ("door_trade", "trade_doors", "same_bldg", "roads"):
        print(t, f"{con.execute(f'SELECT count(*) FROM {t}').fetchall()[0][0]:,}")
