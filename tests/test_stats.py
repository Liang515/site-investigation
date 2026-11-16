import datetime as dt

import duckdb

from sitefloor import stats

TRADE_COLS = "county, town, base, btype, structure, built_year, total_floors, trade_date, kind, source, unit_price, note"


def _con():
    con = duckdb.connect()
    con.execute("""CREATE TABLE trades (county VARCHAR, town VARCHAR, base VARCHAR, btype VARCHAR, structure VARCHAR,
        built_year INT, total_floors INT, trade_date DATE, kind VARCHAR, source VARCHAR, unit_price DOUBLE, note VARCHAR)""")
    rows = []
    big = "住宅大樓(11層含以上有電梯)"
    for i, p in enumerate([300_000, 310_000, 320_000, 330_000, 340_000]):  # 元／m²
        rows.append(("臺北市", "大安區", f"仁愛路{i}號", big, "RC 鋼筋混凝土", 2000, 14, dt.date(2024, 3, 1), "買賣", "實價登錄", p, None))
    rows += [
        ("臺北市", "大安區", "仁愛路0號", big, "RC 鋼筋混凝土", 2000, 14, dt.date(2024, 5, 1), "買賣", "實價登錄", 10_000,
         "親友、員工或其他特殊關係間之交易。"),  # 特殊交易：不算
        ("臺北市", "大安區", "仁愛路1號", big, None, None, None, dt.date(2024, 6, 1), "租賃", "實價登錄", 900, None),  # 租賃
        ("臺北市", None, "某路9號", "透天厝", "加強磚造", 1970, 3, dt.date(2024, 7, 1), "買賣", "實價登錄", 100_000, None),  # 鄉鎮不明
        ("臺北市", "大安區", "空地1號", "其他", None, None, None, dt.date(2025, 1, 1), "買賣", "實價登錄", 50_000, None),  # 只有土地
    ]
    con.executemany(f"INSERT INTO trades ({TRADE_COLS}) VALUES ({', '.join('?' * 12)})", rows)
    con.execute("""CREATE TABLE door_risk AS SELECT * FROM (VALUES
        ('臺北市', '大安區', '仁愛路0號', '0.5-1.0', NULL, NULL, NULL, '高潛勢', 500),
        ('臺北市', '大安區', '仁愛路1號', NULL, NULL, NULL, NULL, '低潛勢', 4000),
        ('新北市', '板橋區', '文化路1號', '0.3-0.5', '新莊斷層', NULL, NULL, NULL, 800))
        t(county, town, base, flood500, fault, landslide, debris, liquefaction, fire_station_m)""")
    stats.build(con)
    return con


def test_price_levels_and_filters():
    con = _con()
    q = "SELECT n, p50 FROM stats_price WHERE county = ? AND town = ? AND grp = ? AND per = 'Y' AND t = '2024-01-01'"
    n, p50 = con.execute(q, ["臺北市", "大安區", "大樓"]).fetchall()[0]
    assert n == 5  # 特殊交易、租賃都不算
    assert p50 == round(320_000 * stats.M2_PER_PING)  # 元／坪
    assert con.execute(q, ["臺北市", "大安區", "全部"]).fetchall()[0][0] == 5
    assert con.execute(q, ["臺北市", "", "全部"]).fetchall()[0][0] == 6  # 鄉鎮不明的透天只算進縣市
    assert con.execute(q, ["全國", "", "透天"]).fetchall()[0][0] == 1
    assert not con.execute("SELECT count(*) FROM stats_price WHERE grp IS NULL OR town IS NULL").fetchall()[0][0]


def test_bldg_one_row_per_door():
    con = _con()
    got = dict(con.execute("SELECT bucket, n FROM stats_bldg WHERE county = '臺北市' AND town = '' AND dim = 'type'").fetchall())
    assert got == {"大樓": 5, "透天": 1}  # 每個門牌算一次；只有土地（其他）的門牌不算
    ages = dict(con.execute("SELECT bucket, n FROM stats_bldg WHERE county = '臺北市' AND town = '' AND dim = 'age'").fetchall())
    assert ages["40 年以上"] == 1 and sum(ages.values()) == 6
    assert {b for (b,) in con.execute("SELECT DISTINCT bucket FROM stats_bldg").fetchall()} <= {
        b for v in stats.BUCKETS.values() for b in v}


def test_risk_counts():
    con = _con()
    r = con.execute("SELECT doors, flood, flood_deep, fault, liq_high, liq_low, fire_far FROM stats_risk "
                    "WHERE county = '臺北市' AND town = '大安區'").fetchall()[0]
    assert r == (2, 1, 1, 0, 1, 1, 1)
    assert con.execute("SELECT doors, fault FROM stats_risk WHERE county = '全國'").fetchall()[0] == (3, 1)
