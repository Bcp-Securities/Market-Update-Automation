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
    
    campos_estaticos = ['TICKER', 'CPN', 'MATURITY', 'issue_dt', 'BICS_LEVEL_2_INDUSTRY_GROUP_NAME']
    
    try:
        logger.info(f"Buscando dados...")
        df_bdp = blp.bdp(tickers, flds=campos_estaticos)
        logger.info(f"Dados obtidos com sucesso")

        # Verifica se o DataFrame veio no formato longo (ticker, field, value) e faz o pivot
        if 'field' in df_bdp.columns and 'value' in df_bdp.columns:
            df_bdp = df_bdp.pivot(index='ticker', columns='field', values='value').reset_index()
        else:
            df_bdp = df_bdp.reset_index().rename(columns={'index': 'ticker'})
        
        # Garante que todas as colunas existam (mesmo se a Bloomberg retornar vazio para algum campo)
        for col in campos_estaticos:
            if col not in df_bdp.columns:
                df_bdp[col] = None


        # Transformação de tipos para o banco
        df_bdp['MATURITY'] = pd.to_datetime(df_bdp['MATURITY'], errors='coerce').dt.strftime('%Y-%m-%d')
        df_bdp['issue_dt'] = pd.to_datetime(df_bdp['issue_dt'], errors='coerce').dt.strftime('%Y-%m-%d')
        df_bdp.rename(columns={
            'TICKER': 'ticker',
            'ticker': 'isin',
            'CPN': 'coupon',
            'MATURITY': 'maturity',
            'issue_dt': 'issue_date',
            'BICS_LEVEL_2_INDUSTRY_GROUP_NAME': 'industry_group'
        }, inplace=True)
        
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        for _, row in df_bdp.iterrows():
            cursor.execute('''
                INSERT OR IGNORE INTO dim_security (isin, ticker, coupon, maturity, issue_date, industry_group)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (row['isin'], row['ticker'], row['coupon'], row['maturity'], row['issue_date'], row['industry_group']))
        
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
    # Cria uma instância do Excel em segundo plano
    try:
        excel = win32com.client.DispatchEx("Excel.Application")
    except Exception as e:
        logger.error(f"Erro ao iniciar o Excel: {e}", exc_info=True)
        return []
    excel.Visible = False # Mantém invisível
    excel.DisplayAlerts = False # Suprime pop-ups de erro/salvamento

    filepath = os.path.abspath(filepath)
    
    try:
        logger.info(f"Abrindo planilha invisível: {filepath}")
        wb = excel.Workbooks.Open(filepath)
        sheet = wb.Sheets(sheet_name)
        
        excel.CalculateFullRebuild()
        
        logger.info("Aguardando comunicação com o terminal Bloomberg...")
        
        max_retries = 30
        for tentativa in range(max_retries):
            # Permite que o Excel processe a resposta recebida da Bloomberg
            pythoncom.PumpWaitingMessages()
            
            valor_a1 = str(sheet.Range("A1").Value)
            
            if "Requesting" in valor_a1 or "#N/A" in valor_a1 or valor_a1 == 'None':
                time.sleep(1)
                if tentativa == 5 or tentativa == 15:
                    logger.warning("Ainda sem resposta, iniciando recalculo")
                    excel.CalculateFullRebuild()
                continue
            
            logger.info(f"Dados atualizados na tentativa {tentativa + 1}!")
            break
            
        else:
            logger.warning("Tempo esgotado. A Bloomberg não atualizou os dados a tempo.")
            return []

        # Coleta os dados descendo a coluna até achar uma célula vazia
        isins = []
        linha = 2 # Começa da linha 2, ignorando o cabeçalho "id"
        
        while True:
            valor_celula = sheet.Cells(linha, 1).Value # Coluna 1 = A
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
        # Garante que o processo do Excel será fechado da memória
        try:
            wb.Close(SaveChanges=False)
            excel.Quit()
        except:
            pass

def check_and_register_new_issues(filepath, db_path=DB_PATH):
    """
    Compara os ISINs extraídos do Excel com os que já existem no banco
    e cadastra apenas os novos.
    """
    isins_excel = fetch_isins_from_excel_background(filepath)
    
    if not isins_excel:
        logger.warning("Nenhum ISIN retornado. Abortando verificação.")
        return

    # Conecta no banco para ver o que já temos
    conn = sqlite3.connect(db_path)
    try:
        # Pega a lista de tickers já cadastrados na dim_security
        df_db = pd.read_sql("SELECT isin FROM dim_security", conn)
        tickers_db = set(df_db['isin'].tolist())
        
        tickers_excel_set = set(isins_excel)
        
        # Encontra a diferença (o que tem no Excel que NÃO tem no Banco)
        novos_tickers = list(tickers_excel_set - tickers_db)
        
        if novos_tickers:
            logger.info(f"Encontrados {len(novos_tickers)} ativos novos. Iniciando cadastro...")
            logger.info(f"Encontrados: {novos_tickers}")
            register_new_assets(novos_tickers, db_path)
        else:
            logger.info("Nenhum ativo novo. Todos os bonds do SRCH já estão na dim_security.")
            
    finally:
        conn.close()

def main():
    logger.info("=" * 60)
    logger.info("INICIANDO ATUALIZAÇÃO AUTOMÁTICADA dim_security")
    logger.info("=" * 60)

    check_and_register_new_issues(EXCEL_FILE)

    logger.info("=" * 60)
    logger.info("ATUALIZAÇÃO CONCLUÍDA")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()