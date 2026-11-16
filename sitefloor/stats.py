"""統計頁用的預先彙整表（全國、縣市、鄉鎮三層；縣市層 town=''，全國層 county='全國'）。

- stats_price(county, town, grp, per, t, n, p25, p50, p75)：住宅買賣每坪單價（元／坪）的分位數
  per='Y' 年、'Q' 季，t＝期間第一天；grp＝大樓、華廈、公寓、透天、套房、全部
  只收實價登錄的買賣、有單價的住宅；排除特殊交易（SPECIAL：親友、僅車位、政府標售、分件登記、含增建等）
- stats_bldg(county, town, dim, bucket, n)：有實價紀錄的門牌（一個門牌號≈一棟）的屋齡、型態、構造、總樓層分布
  每個門牌取最近一筆有該欄位的紀錄（含租賃：只用建物欄位，不用價格）
- stats_risk(county, town, doors, flood, flood_deep, fault, landslide, debris, liq_high, liq_mid, liq_low,
  fire_far, fire_m)：門牌落在各災害潛勢範圍的數量（該縣市有沒有圖資見 risk_coverage）

用法：uv run python -m sitefloor.stats   （trades、risk 之後，export 之前）
"""

import duckdb

from sitefloor.trades import M2_PER_PING

START_YEAR = 2013  # 實價登錄 2012 年 8 月開始，第一個完整年度
FIRE_FAR_M = 3000  # 離最近消防隊超過這個距離（直線）算「偏遠」

GROUPS = ["全部", "大樓", "華廈", "公寓", "透天", "套房"]
# 分布圖的類別順序（stats_bldg.bucket）
BUCKETS = {
    "age": ["5 年內", "5–15 年", "15–30 年", "30–40 年", "40 年以上"],
    "type": ["大樓", "華廈", "公寓", "透天", "套房", "店面／辦公／廠房"],
    "structure": ["RC", "SRC", "鋼骨", "加強磚造", "磚造", "木造", "其他"],
    "floors": ["1–3 層", "4–5 層", "6–10 層", "11–20 層", "21 層以上"],
}
RISK_KINDS = {"flood": "flood500", "flood_deep": "flood500", "fault": "fault", "landslide": "landslide",
              "debris": "debris", "liq_high": "liquefaction", "liq_mid": "liquefaction", "liq_low": "liquefaction"}
NATION = "全國"

GROUP = """CASE
    WHEN btype LIKE '%大樓(11層含以上有電梯)' THEN '大樓'
    WHEN btype LIKE '華廈%' THEN '華廈'
    WHEN btype LIKE '公寓%' THEN '公寓'
    WHEN btype = '透天厝' THEN '透天'
    WHEN btype LIKE '套房%' THEN '套房'
END"""
# 價格不反映一般市場行情的交易（備註欄）
SPECIAL = ("親友|員工|共有人|特殊關係|僅車位|政府機關|協議價購|分件登記|分次登記|增建|未登記|頂樓加蓋|陽台外推|"
           "夾層|瑕疵|凶|毛胚|毛坯|急買急賣|債權債務|法拍|拍賣|祭祀|地上權|公共設施保留地")

# 三個層級：全國、縣市、鄉鎮（鄉鎮不明的紀錄只算進縣市、全國）
LEVELS = """
SELECT '全國' AS county, '' AS town, * EXCLUDE (county, town) FROM src
UNION ALL SELECT county, '' AS town, * EXCLUDE (county, town) FROM src
UNION ALL SELECT county, town, * EXCLUDE (county, town) FROM src WHERE town IS NOT NULL
"""

PRICE_SQL = f"""
CREATE OR REPLACE TABLE stats_price AS
WITH sale AS (
    SELECT county, town, {GROUP} AS grp, trade_date, unit_price * {M2_PER_PING} AS ppp FROM trades
    WHERE kind = '買賣' AND source = '實價登錄' AND unit_price > 0
      AND trade_date BETWEEN DATE '{START_YEAR}-01-01' AND current_date
      AND NOT regexp_matches(coalesce(note, ''), '{SPECIAL}')
),
src AS (
    SELECT county, town, grp, 'Y' AS per, date_trunc('year', trade_date) AS t, ppp FROM sale WHERE grp IS NOT NULL
    UNION ALL
    SELECT county, town, grp, 'Q', date_trunc('quarter', trade_date), ppp FROM sale WHERE grp IS NOT NULL
),
lv AS ({LEVELS}),
both_grp AS (SELECT * FROM lv UNION ALL SELECT * REPLACE ('全部' AS grp) FROM lv)
SELECT county, town, grp, per, t::DATE AS t, count(*)::INT AS n,
       round(quantile_cont(ppp, 0.25))::INT AS p25, round(median(ppp))::INT AS p50,
       round(quantile_cont(ppp, 0.75))::INT AS p75
FROM both_grp GROUP BY ALL ORDER BY county, town, grp, per, t
"""

BLDG_SQL = f"""
CREATE OR REPLACE TABLE stats_bldg AS
WITH door AS (
    SELECT county, any_value(town) FILTER (WHERE town IS NOT NULL) AS town, base,
           arg_max(built_year, trade_date) FILTER (WHERE built_year BETWEEN 1900 AND year(current_date)) AS y,
           arg_max(btype, trade_date) FILTER (WHERE btype <> '其他') AS btype,  -- 「其他」多是只有土地的交易
           arg_max(structure, trade_date) FILTER (WHERE structure IS NOT NULL) AS structure,
           arg_max(total_floors, trade_date) FILTER (WHERE total_floors BETWEEN 1 AND 110) AS floors
    FROM trades GROUP BY county, base
),
src AS (
    SELECT county, town, 'age' AS dim, CASE
        WHEN year(current_date) - y < 5 THEN '5 年內'
        WHEN year(current_date) - y < 15 THEN '5–15 年'
        WHEN year(current_date) - y < 30 THEN '15–30 年'
        WHEN year(current_date) - y < 40 THEN '30–40 年'
        ELSE '40 年以上' END AS bucket FROM door WHERE y IS NOT NULL
    UNION ALL
    SELECT county, town, 'type', coalesce({GROUP}, '店面／辦公／廠房') FROM door WHERE btype IS NOT NULL
    UNION ALL
    SELECT county, town, 'structure', CASE
        WHEN structure LIKE 'SRC%' THEN 'SRC'
        WHEN structure LIKE 'RC%' THEN 'RC'
        WHEN structure LIKE '鋼骨%' THEN '鋼骨'
        WHEN structure = '加強磚造' THEN '加強磚造'
        WHEN structure = '磚造' THEN '磚造'
        WHEN structure = '木造' THEN '木造'
        ELSE '其他' END FROM door WHERE structure IS NOT NULL
    UNION ALL
    SELECT county, town, 'floors', CASE
        WHEN floors <= 3 THEN '1–3 層'
        WHEN floors <= 5 THEN '4–5 層'
        WHEN floors <= 10 THEN '6–10 層'
        WHEN floors <= 20 THEN '11–20 層'
        ELSE '21 層以上' END FROM door WHERE floors IS NOT NULL
),
lv AS ({LEVELS})
SELECT county, town, dim, bucket, count(*)::INT AS n FROM lv GROUP BY ALL ORDER BY county, town, dim, bucket
"""

RISK_SQL = f"""
CREATE OR REPLACE TABLE stats_risk AS
WITH src AS (SELECT * FROM door_risk),
lv AS ({LEVELS})
SELECT county, town, count(*)::INT AS doors,
       count(flood500)::INT AS flood,
       count(*) FILTER (WHERE flood500 NOT IN ('0.3-0.5'))::INT AS flood_deep,
       count(fault)::INT AS fault,
       count(landslide)::INT AS landslide,
       count(debris)::INT AS debris,
       count(*) FILTER (WHERE liquefaction = '高潛勢')::INT AS liq_high,
       count(*) FILTER (WHERE liquefaction = '中潛勢')::INT AS liq_mid,
       count(*) FILTER (WHERE liquefaction = '低潛勢')::INT AS liq_low,
       count(*) FILTER (WHERE fire_station_m > {FIRE_FAR_M})::INT AS fire_far,
       round(median(fire_station_m))::INT AS fire_m
FROM lv GROUP BY ALL ORDER BY county, town
"""


def build(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    out = {}
    for name, sql in (("stats_price", PRICE_SQL), ("stats_bldg", BLDG_SQL), ("stats_risk", RISK_SQL)):
        con.execute(sql)
        out[name] = con.execute(f"SELECT count(*) FROM {name}").fetchall()[0][0]
    return out


if __name__ == "__main__":
    from sitefloor.db import connect

    for name, n in build(connect(write=True)).items():
        print(f"  {name} {n:,}")
