"""推估方法的留一驗證：拿有實價總樓層的門牌當答案，假裝它沒有實價，看推估準不準。

用法：PYTHONPATH=. uv run python scripts/validate_infer.py
"""
from sitefloor.db import connect

con = connect()

print("【同棟】同建物多邊形／同點位的其他門牌（排除自己）")
print(con.sql("""
WITH door AS (SELECT DISTINCT county, town, base, bldg_uid, round(lon, 6) AS x, round(lat, 6) AS y FROM addresses),
dt AS (SELECT d.*, t.floors FROM door d JOIN door_trade t USING (county, town, base)),
pb AS (SELECT a.county, a.town, a.base, a.floors AS truth, b.floors AS other, '同棟（建物圖）' AS method
       FROM dt a JOIN dt b ON a.bldg_uid = b.bldg_uid AND a.base <> b.base WHERE a.bldg_uid IS NOT NULL),
pc AS (SELECT a.county, a.town, a.base, a.floors, b.floors, '同點位門牌'
       FROM dt a JOIN dt b ON a.county = b.county AND a.x = b.x AND a.y = b.y AND a.base <> b.base),
est AS (
    SELECT method, county, town, base, any_value(truth) AS truth, mode(other) AS est,
           max(other) - min(other) AS spread
    FROM (SELECT * FROM pb UNION ALL SELECT * FROM pc) GROUP BY ALL
)
SELECT method, (spread = 0) AS 其他門牌一致, count(*) AS 門牌數,
       round(avg((est = truth)::int) * 100, 1) AS 完全相符pct,
       round(avg((abs(est - truth) <= 1)::int) * 100, 1) AS 差一層內pct
FROM est GROUP BY ALL ORDER BY 1, 2 DESC
"""))

print("【同巷】同前綴（路巷弄）、同側、±W 號內的鄰居（排除自己），不同參數：")
for w, k, s in [(4, 2, 1), (6, 2, 1), (6, 2, 0), (6, 3, 1), (10, 2, 1)]:
    r = con.execute(f"""
    WITH q AS (SELECT * FROM trade_doors USING SAMPLE 200000 ROWS (reservoir, 42)),
    nb AS (
        SELECT q.county, q.town, q.base, any_value(q.floors) AS truth,
               round(median(d.floors)) AS est, count(*) AS n, max(d.floors) - min(d.floors) AS spread
        FROM q JOIN trade_doors d
          ON d.county = q.county AND d.town = q.town AND d.prefix = q.prefix
         AND d.num % 2 = q.num % 2 AND abs(d.num - q.num) <= {w} AND d.base <> q.base
        GROUP BY 1, 2, 3
    )
    SELECT count(*) FILTER (WHERE n >= {k} AND spread <= {s}) / 200000.0 AS 可推估比例,
           avg((est = truth)::int) FILTER (WHERE n >= {k} AND spread <= {s}) AS 完全相符,
           avg((abs(est - truth) <= 1)::int) FILTER (WHERE n >= {k} AND spread <= {s}) AS 差一層內
    FROM nb
    """).fetchall()[0]
    print(f"  ±{w:>2} 號、至少 {k} 個鄰居、鄰居差 ≤{s} 層：可推估 {r[0]:.1%}，完全相符 {r[1]:.1%}，差一層內 {r[2]:.1%}")
