"""
Atualizador de séries MENSAIS orientado por metadados.

- series_meta_monthly  -> diz PARA CADA série mensal onde/como buscar (source, ticker, value_field)
- time_series_monthly  -> guarda os valores em formato longo (series_code, obs_date, value)

Para adicionar uma série mensal nova (ex: uma outra série do BCB, ou do
FRED), basta inserir uma linha em series_meta_monthly. NÃO é preciso
alterar este script, a menos que a nova série venha de uma fonte
totalmente diferente das já suportadas (nesse caso, adicionar um novo
fetcher — ver seção FETCHERS POR FONTE).
"""

import logging
import re
import unicodedata
from datetime import datetime
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3

import pandas as pd
import requests


# ==========================================
# CONFIGURAÇÕES
# ==========================================

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "market_update.db"
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "atualizador_dados_mensais.log"

URL_CPI_FRBSF = "https://www.frbsf.org/wp-content/uploads/cpi-contributors-data.xlsx"
SHEET_NAME_FRBSF = "chart2_headlineCPI_contr_YoY"

BCB_SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados?formato=json"


# ==========================================
# LOG
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(LOG_FILE, maxBytes=5_000_000, backupCount=5, encoding="utf-8"),
    ],
)
logger = logging.getLogger("atualizador_mensal")


# ==========================================
# HELPERS DE TEXTO
# ==========================================

def normalizar_texto(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


# ==========================================
# METADADOS
# ==========================================

def carregar_series_meta(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Lê series_meta_monthly e retorna um DataFrame com as séries a atualizar.
    """
    logger.info("Carregando metadados de series_meta_monthly.")

    df = pd.read_sql_query(
        "SELECT series_code, description, source, ticker, value_field "
        "FROM series_meta_monthly",
        conn,
    )

    if df.empty:
        logger.warning("Nenhuma série cadastrada em series_meta_monthly.")

    logger.info("Séries encontradas: %s", df["series_code"].tolist())

    return df


# ==========================================
# FETCHERS POR FONTE
#
# Cada fetcher recebe a linha de metadados da série (um dict com
# series_code / ticker / value_field) e devolve um DataFrame padronizado
# com colunas: series_code, obs_date (datetime, dia 1 do mês), value (float).
#
# Para suportar uma fonte nova, escreva um fetcher com essa mesma
# assinatura e registre-o em FETCHERS abaixo.
# ==========================================

def fetch_frbsf_cpi(meta_row: dict) -> pd.DataFrame:
    """
    Busca os componentes do CPI headline (FRBSF). Cada componente vira
    uma série própria (series_code definido em series_meta_monthly),
    e o campo a extrair da planilha é indicado por value_field.
    """
    series_code = meta_row["series_code"]
    value_field = meta_row["value_field"]

    if not value_field:
        raise ValueError(
            f"Série '{series_code}' com source=FRBSF precisa de value_field preenchido."
        )

    logger.info("Baixando planilha FRBSF para série '%s'.", series_code)

    response = requests.get(URL_CPI_FRBSF, timeout=30)
    response.raise_for_status()

    df = pd.read_excel(BytesIO(response.content), sheet_name=SHEET_NAME_FRBSF)
    df.columns = [normalizar_texto(c) for c in df.columns]

    if "date" not in df.columns:
        raise ValueError("Coluna 'date' não encontrada na planilha FRBSF.")

    for coluna in list(df.columns):
        if "core_services" in coluna:
            df.rename(columns={coluna: "core_services"}, inplace=True)

    if value_field not in df.columns:
        raise ValueError(
            f"Campo '{value_field}' não encontrado na planilha FRBSF. "
            f"Colunas disponíveis: {df.columns.tolist()}"
        )

    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = df.dropna(subset=["date"])
    df["obs_date"] = df["date"].dt.to_period("M").dt.to_timestamp()

    df["value"] = pd.to_numeric(df[value_field], errors="coerce")
    df["series_code"] = series_code

    return df[["series_code", "obs_date", "value"]].dropna(subset=["obs_date"])


def fetch_bcb_sgs(meta_row: dict) -> pd.DataFrame:
    """
    Busca uma série do SGS/BCB (ex: IPCA). O ticker é o código da série
    no SGS (ex: 13522 = IPCA acumulado 12 meses, 433 = IPCA variação mensal).
    """
    series_code = meta_row["series_code"]
    ticker = meta_row["ticker"]

    if not ticker:
        raise ValueError(
            f"Série '{series_code}' com source=BCB precisa de ticker (código SGS) preenchido."
        )

    url = BCB_SGS_URL.format(codigo=ticker)
    logger.info("Baixando série BCB/SGS %s para série '%s'.", ticker, series_code)

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    dados_json = response.json()
    df = pd.DataFrame(dados_json)

    if df.empty:
        raise ValueError(f"BCB/SGS retornou vazio para o código {ticker}.")

    df["obs_date"] = pd.to_datetime(df["data"], format="%d/%m/%Y")
    df["obs_date"] = df["obs_date"].dt.to_period("M").dt.to_timestamp()
    df["value"] = pd.to_numeric(df["valor"], errors="coerce")
    df["series_code"] = series_code

    return df[["series_code", "obs_date", "value"]].dropna(subset=["obs_date"])


FETCHERS = {
    "FRBSF": fetch_frbsf_cpi,
    "BCB": fetch_bcb_sgs,
}


# ==========================================
# BANCO
# ==========================================

def conectar_banco(db_path: Path) -> sqlite3.Connection:
    logger.info("Conectando ao banco: %s", db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def obter_ultima_data(conn: sqlite3.Connection, series_code: str):
    cursor = conn.execute(
        "SELECT MAX(obs_date) FROM time_series_monthly WHERE series_code = ?",
        (series_code,),
    )
    resultado = cursor.fetchone()[0]
    return pd.to_datetime(resultado) if resultado else None


def salvar_serie(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """
    Faz upsert (INSERT ... ON CONFLICT) dos pontos novos/atualizados de
    uma série em time_series_monthly. Retorna a quantidade de linhas
    processadas.
    """
    if df.empty:
        return 0

    registros = [
        (
            row["series_code"],
            row["obs_date"].strftime("%Y-%m-%d"),
            None if pd.isna(row["value"]) else float(row["value"]),
        )
        for _, row in df.iterrows()
    ]

    sql = """
        INSERT INTO time_series_monthly (series_code, obs_date, value)
        VALUES (?, ?, ?)
        ON CONFLICT(series_code, obs_date) DO UPDATE SET
            value = excluded.value
    """

    try:
        conn.executemany(sql, registros)
        conn.commit()
        return len(registros)
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Erro ao salvar série no banco. Rollback realizado.")
        raise


# ==========================================
# ORQUESTRAÇÃO
# ==========================================

def atualizar_series(conn: sqlite3.Connection) -> None:
    meta = carregar_series_meta(conn)

    for _, meta_row in meta.iterrows():
        meta_row = meta_row.to_dict()
        series_code = meta_row["series_code"]
        source = meta_row["source"]

        fetcher = FETCHERS.get(source)
        if fetcher is None:
            logger.error(
                "Série '%s' tem source='%s' sem fetcher registrado. Pulando.",
                series_code, source,
            )
            continue

        try:
            logger.info("Atualizando série '%s' (source=%s).", series_code, source)

            df = fetcher(meta_row)

            if df.empty:
                logger.warning("Fetcher não retornou dados para '%s'.", series_code)
                continue

            data_web = df["obs_date"].max()
            data_banco = obter_ultima_data(conn, series_code)

            if data_banco is not None:
                # Reenvia o último ponto também (pode ter sido revisado)
                # e qualquer coisa nova além dele.
                df = df[df["obs_date"] >= data_banco]

            if df.empty:
                logger.info("Série '%s' já está atualizada (web: %s).", series_code, data_web.date())
                continue

            n = salvar_serie(conn, df)

            logger.info(
                "Série '%s' atualizada: %d ponto(s) processado(s) (até %s).",
                series_code, n, data_web.date(),
            )

        except Exception:
            logger.exception("Falha ao atualizar série '%s'. Continuando com as demais.", series_code)
            continue


# ==========================================
# MAIN
# ==========================================

def main() -> None:
    logger.info("=" * 60)
    logger.info("INÍCIO DA ATUALIZAÇÃO MENSAL")
    logger.info("=" * 60)

    conn = None
    try:
        conn = conectar_banco(DB_PATH)
        atualizar_series(conn)
        logger.info("Atualização mensal concluída.")
    except Exception:
        logger.exception("Falha geral na atualização mensal.")
        raise
    finally:
        if conn is not None:
            conn.close()
            logger.info("Conexão com o banco encerrada.")
        logger.info("=" * 60)
        logger.info("FIM DA ATUALIZAÇÃO MENSAL")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()