"""推估用的衍生表（匯入實價後跑一次）。

- door_trade：每個門牌號（county, town, base）的實價總樓層眾數
- trade_doors：door_trade 拆出「路巷弄前綴＋號＋之號」，給同巷推估用
- same_bldg：同棟推估。同一建物多邊形（臺中、臺北），或座標完全相同的門牌（多為主號＋之號），
  取其中有實價門牌的總樓層眾數
- btype：各表都帶實價的建物型態眾數，查詢時用來辨認工廠／倉庫（推估準確率較低）

用法：uv run python -m sitefloor.derive
"""
from sitefloor.db import connect

# 門牌號拆解：前綴（路街巷弄）＋號＋之號
DOOR_RE = r"^(.*?)(\d+)(之(\d+))?號$"

SQL = f"""
CREATE OR REPLACE TABLE door_trade AS
SELECT county, town, base, mode(total_floors) AS floors, count(*) AS n,
       count(DISTINCT total_floors) AS nfl, mode(btype) AS btype
FROM trades
WHERE total_floors IS NOT NULL AND town IS NOT NULL AND base LIKE '%號'
GROUP BY ALL;

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
"""

if __name__ == "__main__":
    con = connect(write=True)
    con.execute(SQL)
    for t in ("door_trade", "trade_doors", "same_bldg"):
        print(t, f"{con.execute(f'SELECT count(*) FROM {t}').fetchall()[0][0]:,}")
