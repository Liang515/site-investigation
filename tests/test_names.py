import duckdb
import pandas as pd

from sitefloor import names


def _con():
    con = duckdb.connect()
    rows = [  # key, name, county, town, base, source, n
        ("臺北之星", "台北之星", "臺北市", "大安區", "忠孝東路三段252號", "alias", 0),
        ("臺北之星", "台北之星", "臺北市", "大同區", "民樂街1號", "osm", 3),
        ("遠雄首府", "遠雄首府", "臺北市", "中山區", "林森北路587號", "osm", 40),
        ("遠雄新都", "遠雄新都", "新北市", "汐止區", "大同路1號", "presale", 5),
        ("體育館", "體育館", "新竹縣", "竹北市", "成功八路99號", "osm", 1),
        ("體育館", "體育館", "基隆市", "安樂區", "安一路360號", "osm", 2),
    ]
    con.register("r", pd.DataFrame(rows, columns=["key", "name", "county", "town", "base", "source", "n"]))
    con.execute("CREATE TABLE bldg_names AS SELECT * FROM r")
    return con


def test_search_orders_exact_alias_and_trades():
    con = _con()
    got = names.search(con, "台北 之星")  # 正規化：去空白、台→臺
    assert [x["addr"] for x in got] == ["臺北市大安區忠孝東路三段252號", "臺北市大同區民樂街1號"]  # 人工別名優先
    assert [x["name"] for x in names.search(con, "遠雄")] == ["遠雄首府", "遠雄新都"]  # 實價筆數多的優先
    assert names.search(con, "遠") == []  # 太短不找


def test_resolve_only_when_unambiguous():
    con = _con()
    assert names.resolve(con, "台北之星") == ("台北之星", "臺北市大安區忠孝東路三段252號")  # 同名時人工別名優先
    assert names.resolve(con, "遠雄首府") == ("遠雄首府", "臺北市中山區林森北路587號")
    assert names.resolve(con, "體育館") is None  # 同名好幾棟：不猜
    assert names.resolve(con, "遠雄") is None  # 只有部分相符：不算
    assert names.resolve(duckdb.connect(), "遠雄首府") is None  # 沒有名稱表


def test_presale_address_cleanup():
    assert names.MULTI_NO.sub(r"\1號", "臺北市大同區寧夏路144、146、148號") == "臺北市大同區寧夏路144號"
    assert names.ODD_BASE.search("臺泥段14地號") and names.ODD_BASE.search("泰安二街與茄苳路一段轉角171號")
    assert not names.ODD_BASE.search("忠孝東路三段252號")
