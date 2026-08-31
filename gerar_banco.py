import sqlite3
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "market_update.db"

SERIES_META_DDL = """
CREATE TABLE IF NOT EXISTS series_meta (
    series_code      TEXT PRIMARY KEY,
    description      TEXT,
    source           TEXT,             -- 'BBG' ou 'FRED'
    ticker           TEXT,             -- ticker BBG (BBG) ou codigo/id FRED (FRED)
    bbg_field        TEXT,             -- campo BBG (px_last/yield/...); null p/ FRED
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS series_meta_monthly (
    series_code TEXT PRIMARY KEY,
    description TEXT,
    source      TEXT,             -- 'FRBSF' ou 'BCB' (extensível pra outras fontes no futuro)
    ticker      TEXT,             -- codigo/id na fonte (ex: 13522 = IPCA 12m no SGS do BCB)
    value_field TEXT,             -- nome da coluna de valor na fonte (usado pela fonte FRBSF); null p/ BCB
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dim_asset (
    asset_id INTEGER PRIMARY KEY AUTOINCREMENT,
    isin TEXT UNIQUE,
    ticker TEXT,
    coupon REAL,
    maturity DATE,
    issue_date DATE,
    industry_group TEXT
);

CREATE TABLE IF NOT EXISTS dim_date (
    date_id INTEGER PRIMARY KEY,
    full_date DATE,
    year INTEGER,
    month INTEGER,
    quarter INTEGER,
    day_of_week INTEGER
);
"""

SCHEMA = SERIES_META_DDL + """
CREATE TABLE IF NOT EXISTS time_series (
    series_code TEXT NOT NULL,
    obs_date    TEXT NOT NULL,          -- ISO YYYY-MM-DD
    value       REAL,
    PRIMARY KEY (series_code, obs_date),
    FOREIGN KEY (series_code) REFERENCES series_meta(series_code)
);
CREATE INDEX IF NOT EXISTS ix_ts_date ON time_series(obs_date);

CREATE TABLE IF NOT EXISTS time_series_monthly (
    series_code TEXT NOT NULL,
    obs_date    TEXT NOT NULL,          -- ISO YYYY-MM-01 (sempre primeiro dia do mes)
    value       REAL,
    PRIMARY KEY (series_code, obs_date),
    FOREIGN KEY (series_code) REFERENCES series_meta_monthly(series_code)
);
CREATE INDEX IF NOT EXISTS ix_ts_monthly_date ON time_series_monthly(obs_date);

CREATE TABLE IF NOT EXISTS fact_price (
    asset_id INTEGER,
    date_id INTEGER,
    price_mid REAL,
    yield_mid REAL,
    duration_mid REAL,
    amt_outstanding REAL,
    rating_moody TEXT,
    rating_sp TEXT,
    rating_fitch TEXT,
    PRIMARY KEY (asset_id, date_id),
    FOREIGN KEY (asset_id) REFERENCES dim_asset(asset_id),
    FOREIGN KEY (date_id) REFERENCES dim_date(date_id)
);
"""
# CREATE TABLE IF NOT EXISTS cpi (
#     id                     INTEGER PRIMARY KEY,
#     date                   DATE NOT NULL UNIQUE,
#     total_headline_cpi_yoy REAL,
#     food                   REAL,
#     energy                 REAL,
#     core_goods             REAL,
#     core_services          REAL,
#     shelter                REAL
# );

def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


if __name__ == "__main__":
    with connect() as c:
        init_schema(c)
        print(f"Schema criado em {DB_PATH}")
        for (name,) in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ):
            print("  tabela:", name)