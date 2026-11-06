from sitefloor.suggest import split_base, variants


def test_split_base():
    assert split_base("市政路604巷5號") == ("市政路", "604巷", 5, 0, False)
    assert split_base("工業區一路98巷7弄10之2號") == ("工業區一路", "98巷7弄", 10, 2, False)
    assert split_base("中山北路四段1巷1弄臨11號") == ("中山北路四段", "1巷1弄", 11, 0, True)
    assert split_base("北屯段259-72地號") is None


def test_variants_expand_short_names():
    got = [v for v, _ in variants("台中西屯市政路387號")]
    assert "臺中市西屯區市政路387號" in got


def test_variants_fix_town_typo():
    got = dict(variants("新北市板僑區文化路一段188號"))
    assert got.get("新北市板橋區文化路一段188號") == "區名相近：板僑區→板橋區"


def test_variants_ambiguous_county_not_guessed():
    # 新竹有市有縣，不猜
    assert all(not v.startswith(("新竹市", "新竹縣")) for v, _ in variants("新竹光復路1號"))


def test_exact_door_beyond_top_roads():
    # 同名路很多個區都有時，門牌少的區（臺北市中正區仁愛路一段）排在前 5 名之外，完全相符的門牌仍要列出
    import duckdb

    from sitefloor.suggest import Suggester

    con = duckdb.connect()
    con.execute("CREATE TABLE addresses (county VARCHAR, town VARCHAR, base VARCHAR)")
    con.execute("CREATE TABLE door_trade (county VARCHAR, town VARCHAR, base VARCHAR)")
    towns = [("新北市", f"甲{i}區", 100 - i) for i in range(6)] + [("臺北市", "中正區", 10)]
    rows = [(c, t, f"仁愛路一段{n}號") for c, t, k in towns for n in range(1, k + 1) if n != 17]
    rows.append(("臺北市", "中正區", "仁愛路一段17號"))
    con.executemany("INSERT INTO addresses VALUES (?, ?, ?)", rows)
    con.execute(
        "CREATE TABLE roads AS SELECT county, town, '仁愛路一段' AS road, count(*) AS n FROM addresses GROUP BY ALL"
    )
    got = [x["text"] for x in Suggester(con).suggest(con, "仁愛路1段17號")]
    assert "臺北市中正區仁愛路一段17號" in got
