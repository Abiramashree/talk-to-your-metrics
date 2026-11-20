"""Download the Olist dataset (if missing) and load it into DuckDB as schema `raw`.

Data: Brazilian E-Commerce Public Dataset by Olist, CC BY-NC-SA 4.0
https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce
"""

import pathlib
import sys
import urllib.request
import zipfile

import duckdb

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "olist.duckdb"
URL = "https://www.kaggle.com/api/v1/datasets/download/olistbr/brazilian-ecommerce"

# Published row counts; a mismatch means a different or corrupted download.
EXPECTED = {
    "customers": 99441,
    "geolocation": 1000163,
    "order_items": 112650,
    "order_payments": 103886,
    "order_reviews": 99224,
    "orders": 99441,
    "products": 32951,
    "sellers": 3095,
    "product_category_name_translation": 71,
}


def table_name(path: pathlib.Path) -> str:
    return path.stem.removeprefix("olist_").removesuffix("_dataset")


def download() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    archive = RAW / "olist.zip"
    print(f"downloading {URL}")
    urllib.request.urlretrieve(URL, archive)
    with zipfile.ZipFile(archive) as z:
        for member in z.namelist():
            name = pathlib.PurePosixPath(member)
            if name.is_absolute() or ".." in name.parts or name.suffix != ".csv":
                sys.exit(f"unexpected archive member: {member}")
        z.extractall(RAW)
    archive.unlink()


def main() -> None:
    if not set(EXPECTED) <= {table_name(f) for f in RAW.glob("*.csv")}:
        download()
    DB.unlink(missing_ok=True)
    con = duckdb.connect(str(DB))
    con.execute("create schema raw")
    for f in sorted(RAW.glob("*.csv")):
        name = table_name(f)
        if name not in EXPECTED:
            continue
        con.execute(f"create table raw.{name} as select * from read_csv(?, header = true)", [str(f)])
        rows = con.execute(f"select count(*) from raw.{name}").fetchone()[0]
        if rows != EXPECTED[name]:
            sys.exit(f"{name}: {rows} rows, expected {EXPECTED[name]}")
        print(f"{name:<36}{rows:>9,}")
    missing = set(EXPECTED) - {r[0] for r in con.execute("select table_name from information_schema.tables where table_schema = 'raw'").fetchall()}
    if missing:
        sys.exit(f"missing tables: {sorted(missing)}")
    con.close()
    print(f"loaded {DB.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
