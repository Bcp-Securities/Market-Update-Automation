"""
Popula (ou atualiza) series_meta_monthly com as séries mensais conhecidas.

Rode uma vez (ou sempre que quiser cadastrar uma série nova):
    python seed_series_meta_monthly.py

Para adicionar uma série nova no futuro, basta acrescentar uma tupla
na lista SERIES abaixo e rodar este script de novo — não precisa
mexer no monthly_update.py.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "market_update.db"

# (series_code, description, source, ticker, value_field)
#
# source='FRBSF'  -> usa fetch_frbsf_cpi; value_field = coluna na planilha
# source='BCB'    -> usa fetch_bcb_sgs;   ticker = código da série no SGS
SERIES = [
    ("cpi_headline_yoy", "CPI headline YoY (FRBSF)",        "FRBSF", None,    "total_headline_cpi_yoy"),
    ("cpi_food",          "CPI food contribution (FRBSF)",   "FRBSF", None,    "food"),
    ("cpi_energy",        "CPI energy contribution (FRBSF)", "FRBSF", None,    "energy"),
    ("cpi_core_goods",    "CPI core goods (FRBSF)",          "FRBSF", None,    "core_goods"),
    ("cpi_core_services", "CPI core services (FRBSF)",       "FRBSF", None,    "core_services"),
    ("cpi_shelter",       "CPI shelter (FRBSF)",             "FRBSF", None,    "shelter"),
    ("ipca_12",          "IPCA acumulado 12 meses (BCB/SGS)", "BCB", "13522", None),
]

UPSERT_SQL = """
INSERT INTO series_meta_monthly (series_code, description, source, ticker, value_field)
VALUES (?, ?, ?, ?, ?)
ON CONFLICT(series_code) DO UPDATE SET
    description = excluded.description,
    source      = excluded.source,
    ticker      = excluded.ticker,
    value_field = excluded.value_field
"""

def main():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executemany(UPSERT_SQL, SERIES)
    conn.commit()

    print("series_meta_monthly atualizada:")
    for row in conn.execute(
        "SELECT series_code, source, ticker, value_field FROM series_meta_monthly ORDER BY series_code"
    ):
        print(" ", row)

    conn.close()


if __name__ == "__main__":
    main()