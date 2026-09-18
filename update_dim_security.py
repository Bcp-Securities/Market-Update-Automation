import pandas as pd
import win32com.client
import pythoncom
import time
import os
import sqlite3
from xbbg import blp
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
import xbbg
xbbg.set_backend("pandas")

BASE_DIR = Path(__file__).resolve().parent
EXCEL_FILE = BASE_DIR / "new_issues.xlsx"
DB_PATH = BASE_DIR / "market_update.db"
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "atualizador_dim_security.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # RotatingFileHandler evita que o log cresça indefinidamente
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding='utf-8'),
        # logging.StreamHandler()
    ]
)

logger = logging.getLogger("atualizador_dim_security")

def register_new_assets(tickers, db_path=DB_PATH):
    logger.info(f"Cadastrando {len(tickers)} novo(s) ativo(s)...")

    campos_estaticos = ['TICKER', 'CPN', 'MATURITY', 'issue_dt', 'BICS_LEVEL_2_INDUSTRY_GROUP_NAME', 'ISSUER', 'ID_ISIN', 'CRNCY', 'PAYMENT_RANK', 'AMT_ISSUED', 'MIN_PIECE']

    try:
        logger.info(f"Buscando dados...")
        df_bdp = blp.bdp(tickers, flds=campos_estaticos)
        logger.info(f"Dados obtidos com sucesso")

        if 'field' in df_bdp.columns and 'value' in df_bdp.columns:
            df_bdp = df_bdp.pivot(index='ticker', columns='field', values='value').reset_index()
        else:
            df_bdp = df_bdp.reset_index().rename(columns={'index': 'ticker'})

        for col in campos_estaticos:
            if col not in df_bdp.columns:
                df_bdp[col] = None

        # Conversao explicita para YYYY-MM-DD (colunas DATE no banco).
        # Evita gravar timestamp completo (ex: "2032-06-15 00:00:00") quando
        # a Bloomberg retorna Timestamp em vez de string.
        df_bdp['MATURITY'] = pd.to_datetime(df_bdp['MATURITY'], errors='coerce').dt.strftime('%Y-%m-%d')
        df_bdp['issue_dt'] = pd.to_datetime(df_bdp['issue_dt'], errors='coerce').dt.strftime('%Y-%m-%d')

        df_bdp.rename(columns={
            'ticker': 'bbg_id',
            'TICKER': 'ticker',
            'CPN': 'coupon',
            'MATURITY': 'maturity',
            'issue_dt': 'issue_date',
            'BICS_LEVEL_2_INDUSTRY_GROUP_NAME': 'industry_group',
            'ISSUER': 'issuer',
            'ID_ISIN': 'isin',
            'CRNCY': 'currency',
            'PAYMENT_RANK': 'collateral',
            'AMT_ISSUED': 'amt_issuance',
            'MIN_PIECE': 'min_piece'
        }, inplace=True)

        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        for _, row in df_bdp.iterrows():
            cursor.execute('''
                INSERT OR IGNORE INTO dim_security (bbg_id, ticker, coupon, maturity, issue_date, industry_group, issuer, isin, currency, collateral, amt_issuance, min_piece)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (row['bbg_id'], row['ticker'], row['coupon'], row['maturity'], row['issue_date'], row['industry_group'], row['issuer'], row['isin'], row['currency'], row['collateral'], row['amt_issuance'], row['min_piece']))

        conn.commit()
        logger.info("Ativos cadastrados com sucesso na dim_security.")

    except Exception as e:
        logger.error(f"Erro no cadastro: {e}", exc_info=True)
    finally:
        if 'conn' in locals():
            conn.close()

def fetch_isins_from_excel_background(filepath, sheet_name='Sheet1', cell_start='A1'):
    """
    Abre o Excel de forma invisível, espera o BSRCH carregar e retorna a lista de ISINs.
    """
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
    except Exception as e:
        logger.error(f"Erro ao iniciar o Excel: {e}", exc_info=True)
        return []
    excel.Visible = False
    excel.DisplayAlerts = False

    filepath = os.path.abspath(filepath)

    try:
        logger.info(f"Abrindo planilha invisível: {filepath}")
        wb = excel.Workbooks.Open(filepath)
        sheet = wb.Sheets(sheet_name)

        excel.CalculateFullRebuild()

        logger.info("Aguardando comunicação com o terminal Bloomberg...")

        max_retries = 60
        for tentativa in range(max_retries):
            pythoncom.PumpWaitingMessages()

            valor_a1 = str(sheet.Range("A1").Value)

            if "Requesting" in valor_a1 or "#N/A" in valor_a1 or valor_a1 == 'None':
                time.sleep(1)
                if tentativa == 15 or tentativa == 30:
                    logger.warning("Ainda sem resposta, iniciando recalculo")
                    excel.CalculateFullRebuild()
                continue

            logger.info(f"Dados atualizados na tentativa {tentativa + 1}!")
            break

        else:
            logger.warning("Tempo esgotado. A Bloomberg não atualizou os dados a tempo.")
            return []

        isins = []
        linha = 2

        while True:
            valor_celula = sheet.Cells(linha, 1).Value
            if not valor_celula or valor_celula == 'None':
                break
            isins.append(str(valor_celula).strip())
            linha += 1

        logger.info(f"Foram extraídos {len(isins)} ISINs da planilha.")
        return isins

    except Exception as e:
        logger.error(f"Erro ao manipular o Excel: {e}", exc_info=True)
        return []

    finally:
        try:
            wb.Close(SaveChanges=False)
            excel.Quit()
        except Exception as e:
            logger.warning(f"Erro ao fechar o Excel (processo pode ter ficado em memoria): {e}")

def check_and_register_new_issues(filepath, db_path=DB_PATH):
    """
    Compara os ISINs extraídos do Excel com os que já existem no banco
    e cadastra apenas os novos.
    """
    isins_excel = fetch_isins_from_excel_background(filepath)

    if not isins_excel:
        logger.warning("Nenhum ISIN retornado. Abortando verificação.")
        return

    conn = sqlite3.connect(db_path)
    try:
        df_db = pd.read_sql("SELECT isin FROM dim_security", conn)
        tickers_db = set(df_db['isin'].tolist())

        tickers_excel_set = set(isins_excel)

        novos_tickers = list(tickers_excel_set - tickers_db)

        if novos_tickers:
            logger.info(f"Encontrados {len(novos_tickers)} ativos novos. Iniciando cadastro...")
            logger.info(f"Encontrados: {novos_tickers}")
            register_new_assets(novos_tickers, db_path)
        else:
            logger.info("Nenhum ativo novo. Todos os bonds do SRCH já estão na dim_security.")

    finally:
        conn.close()

def check_inactive_flag(db_path=DB_PATH):
    """
    Verifica os critérios de inatividade e atualiza a coluna flag_inactive na dim_security.
    Critérios de inatividade:
    - Ativos com maturity date no passado (maturity < hoje)
    - Ativos com amt_outstanding = 0 (última observação na fact_bdp)
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            UPDATE dim_security
            SET flag_inactive = 1
            WHERE maturity < date('now')
        """)
        conn.execute("""
            UPDATE dim_security
            SET flag_inactive = 1
            WHERE asset_id IN (
                SELECT asset_id
                FROM fact_bdp f
                WHERE date_id = (
                    SELECT MAX(f2.date_id)
                    FROM fact_bdp f2
                    WHERE f2.asset_id = f.asset_id
                )
                AND amt_outstanding = 0
            )
        """)
        conn.commit()
        logger.info("Flag de inatividade atualizada com sucesso na dim_security.")
    except Exception as e:
        logger.error(f"Erro ao verificar flag de inatividade: {e}", exc_info=True)
    finally:
        conn.close()

def main():
    logger.info("=" * 60)
    logger.info("INICIANDO ATUALIZAÇÃO AUTOMATIZADA DIM_SECURITY")
    logger.info("=" * 60)

    check_and_register_new_issues(EXCEL_FILE)
    check_inactive_flag()

    logger.info("=" * 60)
    logger.info("ATUALIZAÇÃO CONCLUÍDA")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()