import duckdb
import pytest

from sitefloor import export


def _setup(monkeypatch, tmp_path, tables):
    con = duckdb.connect()
    con.execute("CREATE TABLE coverage AS SELECT '臺北市' AS county, 10 AS doors")
    con.execute("CREATE TABLE trades AS SELECT * FROM (VALUES ('臺北市', '買賣', 100), ('臺北市', '租賃', 2)) t(county, kind, total_price)")
    monkeypatch.setattr(export, "connect", lambda: con)
    monkeypatch.setattr(export, "SERVE_DIR", tmp_path / "serve")
    monkeypatch.setattr(export, "TABLES", tables)
    monkeypatch.setattr(export, "EXPR", {"trades": {"total_price": export.SALE_ONLY.format(c="total_price")}})
    old = tmp_path / "serve" / "old_table"
    old.mkdir(parents=True)
    (old / "x.parquet").write_text("上一版")
    return tmp_path / "serve"


def test_export_replaces_serve_dir(monkeypatch, tmp_path):
    serve = _setup(monkeypatch, tmp_path, {"coverage": (None, "county"), "trades": (None, "county")})
    export.main()
    assert sorted(p.name for p in serve.iterdir()) == ["coverage", "trades"]  # 上一版整個換掉
    assert not (tmp_path / "serve.new").exists() and not (tmp_path / "serve.old").exists()
    prices = duckdb.sql(f"SELECT kind, total_price FROM '{serve}/trades/*.parquet' ORDER BY kind").fetchall()
    assert prices == [("租賃", None), ("買賣", 100)]  # 租賃價格不匯出


def test_export_failure_keeps_previous(monkeypatch, tmp_path):
    serve = _setup(monkeypatch, tmp_path, {"coverage": (None, "county"), "trades": (None, "沒有這個欄位")})
    with pytest.raises(duckdb.Error):
        export.main()
    assert (serve / "old_table" / "x.parquet").read_text() == "上一版"  # 中途失敗：上一版完整保留
    assert not (tmp_path / "serve.new").exists()
