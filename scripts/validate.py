"""用實價登錄的總樓層數，驗證建物圖（門牌點→建物）比對出來的地上樓層。

用法：uv run python scripts/validate.py [縣市]   （預設臺中市）
"""
import sys

from sitefloor.db import connect

county = sys.argv[1] if len(sys.argv) > 1 else "臺中市"
con = connect()

# 每個門牌（town+base）的實價總樓層眾數 vs 該門牌對到建物的樓層
con.execute("""
CREATE TEMP TABLE v AS
WITH t AS (
    SELECT town, base, mode(total_floors) AS t_floors, count(*) AS n,
           count(DISTINCT total_floors) AS n_distinct
    FROM trades WHERE county = $1 AND source = '實價登錄' AND total_floors IS NOT NULL
    GROUP BY 1, 2
),
a AS (
    SELECT town, base, bldg_uid, match FROM addresses
    WHERE county = $1 AND bldg_uid IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY town, base ORDER BY match = 'nearest', bldg_uid) = 1
)
SELECT t.*, a.match, b.floors_above AS b_floors, b.btype
FROM t JOIN a USING (town, base) JOIN buildings b ON b.uid = a.bldg_uid
WHERE b.floors_above IS NOT NULL
""".replace("$1", "?"), [county] * 2)

total = con.execute("SELECT count(*) FROM trades WHERE county = ? AND source = '實價登錄' AND total_floors IS NOT NULL", [county]).fetchall()[0][0]
print(f"{county}：實價有總樓層的紀錄 {total:,} 筆，能對到建物圖的門牌 {con.execute('SELECT count(*) FROM v').fetchall()[0][0]:,} 個\n")

print("整體／依比對方式：")
print(con.sql("""
SELECT coalesce(match, '合計') AS 比對方式, count(*) AS 門牌數,
       round(avg((b_floors = t_floors)::int) * 100, 1) AS 完全相符pct,
       round(avg((abs(b_floors - t_floors) <= 1)::int) * 100, 1) AS 差一層內pct,
       round(avg((b_floors < t_floors)::int) * 100, 1) AS 低估pct
FROM v GROUP BY ROLLUP (match) ORDER BY match NULLS LAST
"""))

print("依實價總樓層級距：")
print(con.sql("""
SELECT CASE WHEN t_floors <= 3 THEN '1-3' WHEN t_floors <= 5 THEN '4-5' WHEN t_floors <= 10 THEN '6-10'
            WHEN t_floors <= 20 THEN '11-20' ELSE '21+' END AS 級距,
       count(*) AS 門牌數,
       round(avg((b_floors = t_floors)::int) * 100, 1) AS 完全相符pct,
       round(avg((abs(b_floors - t_floors) <= 1)::int) * 100, 1) AS 差一層內pct,
       round(median(b_floors - t_floors), 1) AS 誤差中位數
FROM v GROUP BY 1 ORDER BY min(t_floors)
"""))

print("差最多的例子：")
print(con.sql("""
SELECT town, base, t_floors AS 實價, b_floors AS 建物圖, btype, n AS 筆數 FROM v
ORDER BY abs(b_floors - t_floors) DESC, n DESC LIMIT 12
"""))
