"""各縣市涵蓋率（預先算好存成 coverage 表，網頁統計直接讀）。

單位是「門牌號」（不含樓層）。依查詢時的優先順序計：自己的實價 → 同棟一致 → 同巷 → OSM 樓層 → 同棟不一致 → 建物圖。
工廠／倉庫的推估信心各降一級（同 lookup.py）。
用法：uv run python -m sitefloor.coverage   （建庫、匯入實價、derive 之後跑）
"""
from sitefloor.db import connect
from sitefloor.derive import DOOR_RE
from sitefloor.lookup import FACTORY_PAT, NB_MIN, NB_SPREAD, NB_WINDOW

SQL = f"""
CREATE OR REPLACE TABLE coverage AS
WITH a AS (
    SELECT county, town, base, any_value(bldg_uid) AS bldg_uid,
           regexp_extract(base, '{DOOR_RE}', 1) AS prefix,
           TRY_CAST(regexp_extract(base, '{DOOR_RE}', 2) AS INTEGER) AS num
    FROM addresses GROUP BY 1, 2, 3
),
nb AS (
    SELECT a.county, a.town, a.base, avg(regexp_matches(coalesce(d.btype, ''), '{FACTORY_PAT}')::int) >= 0.5 AS fac
    FROM a JOIN trade_doors d
      ON d.county = a.county AND d.town = a.town AND d.prefix = a.prefix
     AND d.num % 2 = a.num % 2 AND abs(d.num - a.num) <= {NB_WINDOW} AND d.base <> a.base
    GROUP BY 1, 2, 3
    HAVING count(*) >= {NB_MIN} AND max(d.floors) - min(d.floors) <= {NB_SPREAD}
),
flags AS (
    SELECT a.county,
           (t.base IS NOT NULL) AS own,
           (s.spread = 0) AS same_ok,
           (s.spread = 0 AND NOT regexp_matches(coalesce(s.btype, '') || '/' || coalesce(b.btype, ''), '{FACTORY_PAT}')) AS same_high,
           (nb.base IS NOT NULL AND NOT nb.fac AND NOT regexp_matches(coalesce(b.btype, ''), '{FACTORY_PAT}')) AS near,
           (nb.base IS NOT NULL) AS near_any,
           (s.base IS NOT NULL) AS same_any,
           (b.floors_above IS NOT NULL) AS map,
           (ob.levels IS NOT NULL) AS osm
    FROM a
    LEFT JOIN door_trade t USING (county, town, base)
    LEFT JOIN same_bldg s USING (county, town, base)
    LEFT JOIN nb USING (county, town, base)
    LEFT JOIN buildings b ON b.uid = a.bldg_uid
    LEFT JOIN door_osm o USING (county, town, base)
    LEFT JOIN osm_buildings ob ON ob.uid = o.osm_uid
)
SELECT county, count(*) AS doors,
       avg(own::int) AS by_trade,
       avg((own OR coalesce(same_high, false))::int) AS high,
       avg((own OR coalesce(same_ok, false) OR near)::int) AS high_mid,
       avg((own OR coalesce(same_ok, false) OR near_any OR osm OR same_any OR map)::int) AS any_floor,
       avg(map::int) AS by_map
FROM flags GROUP BY 1
"""

if __name__ == "__main__":
    con = connect(write=True)
    con.execute(SQL)
    print(con.sql("""
        SELECT county, doors, round(by_trade*100,1) AS 自己實價, round(high*100,1) AS 高信心,
               round(high_mid*100,1) AS 高加中, round(any_floor*100,1) AS 任一含低
        FROM coverage ORDER BY doors DESC"""))
    print(con.sql("""
        SELECT '全部' AS 範圍, sum(doors) AS doors,
               round(sum(by_trade*doors)/sum(doors)*100,1) AS 自己實價, round(sum(high*doors)/sum(doors)*100,1) AS 高信心,
               round(sum(high_mid*doors)/sum(doors)*100,1) AS 高加中, round(sum(any_floor*doors)/sum(doors)*100,1) AS 任一含低
        FROM coverage"""))
