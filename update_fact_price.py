import pandas as pd
import sqlite3
from datetime import datetime, timedelta
from xbbg import blp
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
import xbbg
xbbg.set_backend("pandas")

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "market_update.db"
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "atualizador_fact_price.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # RotatingFileHandler evita que o log cresça indefinidamente
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding='utf-8'),
        # logging.StreamHandler()
    ]
)

logger = logging.getLogger("atualizador_fact_price")

def update_fact_prices(reference_date, db_path=DB_PATH):
    logger.info(f"Iniciando atualização de cotações para {reference_date}...")
    
    try:
        conn = sqlite3.connect(db_path)
        
        # 1. Busca os ativos cadastrados
        df_assets = pd.read_sql("SELECT asset_id, isin FROM dim_asset", conn)
        if df_assets.empty:
            logger.warning("Nenhum ativo encontrado na dim_asset. Abortando.")
            return
            
        tickers = df_assets['isin'].tolist()
        logger.info(f"{len(tickers)} ativos carregados do banco.")

        # 2. Garante a data na dim_date
        dt_obj = datetime.strptime(reference_date, '%Y-%m-%d')
        date_id = int(dt_obj.strftime('%Y%m%d'))
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO dim_date (date_id, full_date, year, month, quarter, day_of_week)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (date_id, reference_date, dt_obj.year, dt_obj.month, (dt_obj.month - 1) // 3 + 1, dt_obj.weekday() + 1))
        conn.commit()

        # 3. Extração Bloomberg
        campos_bdp = ['AMT_OUTSTANDING', 'RTG_MOODY', 'RTG_SP_LONG', 'RTG_FITCH', 'MTY_DUR_MID']
        logger.info(f"Buscando dados BDP para {len(tickers)} tickers...")
        df_bdp = blp.bdp(tickers, flds=campos_bdp)
        logger.info(f"Dados obtidos com sucesso")

        if 'field' in df_bdp.columns and 'value' in df_bdp.columns:
            df_bdp = df_bdp.pivot(index='ticker', columns='field', values='value').reset_index()
        else:
            df_bdp = df_bdp.reset_index().rename(columns={'index': 'ticker'})
            
        for col in campos_bdp:
            if col not in df_bdp.columns:
                df_bdp[col] = None
        
        campos_bdh = ['PX_MID', 'YLD_YTM_MID']
        logger.info(f"Buscando dados BDH para {len(tickers)} tickers...")
        df_bdh = blp.bdh(
            tickers=tickers, flds=campos_bdh, 
            start_date=reference_date, end_date=reference_date, Per='D', Fill='P'
        )
        logger.info(f"Dados obtidos com sucesso")
        
        if not df_bdh.empty:
            if 'date' in df_bdh.columns:
                df_bdh = df_bdh.drop(columns=['date'])
            if 'field' in df_bdh.columns and 'value' in df_bdh.columns:
                df_bdh = df_bdh.pivot(index='ticker', columns='field', values='value').reset_index()
        else:
            df_bdh = pd.DataFrame(columns=['ticker', 'PX_MID', 'YLD_YTM_MID'])

        # 4. Transformação
        df_raw = df_bdp.merge(df_bdh[['ticker', 'PX_MID', 'YLD_YTM_MID']], on='ticker', how='left')
        df_raw['PX_MID'] = pd.to_numeric(df_raw['PX_MID'], errors='coerce')
        df_raw['YLD_YTM_MID'] = pd.to_numeric(df_raw['YLD_YTM_MID'], errors='coerce')
        df_raw['MTY_DUR_MID'] = pd.to_numeric(df_raw['MTY_DUR_MID'], errors='coerce')
        df_raw['AMT_OUTSTANDING'] = pd.to_numeric(df_raw['AMT_OUTSTANDING'], errors='coerce')
        df_raw['AMT_OUTSTANDING'] = df_raw['AMT_OUTSTANDING'] / 1_000_000
        
        df_raw.rename(columns={
            'PX_MID': 'price_mid', 'YLD_YTM_MID': 'yield_mid', 'MTY_DUR_MID': 'duration_mid',
            'AMT_OUTSTANDING': 'amt_outstanding', 'RTG_MOODY': 'rating_moody',
            'RTG_SP_LONG': 'rating_sp', 'RTG_FITCH': 'rating_fitch', 'ticker': 'isin'
        }, inplace=True)

        # 5. Merge com os IDs do banco e Carga (Staging Table Trick)
        df_fact = df_raw.merge(df_assets, on='isin', how='inner')
        df_fact['date_id'] = date_id

        cols_fato = ['asset_id', 'date_id', 'price_mid', 'yield_mid', 'duration_mid', 
                     'amt_outstanding', 'rating_moody', 'rating_sp', 'rating_fitch']
        
        # A magia do UPSERT rápido:
        df_fact[cols_fato].to_sql('fact_price_temp', conn, if_exists='replace', index=False)
        cursor.execute('''
            INSERT OR REPLACE INTO fact_price 
            (asset_id, date_id, price_mid, yield_mid, duration_mid, amt_outstanding, rating_moody, rating_sp, rating_fitch)
            SELECT asset_id, date_id, price_mid, yield_mid, duration_mid, amt_outstanding, rating_moody, rating_sp, rating_fitch
            FROM fact_price_temp
        ''')
        cursor.execute('DROP TABLE fact_price_temp')
        conn.commit()
        
        logger.info("Fato atualizada com sucesso.")
        
    except Exception as e:
        logger.error(f"Erro na atualização da fato: {e}", exc_info=True)
    finally:
        if 'conn' in locals():
            conn.close()

def checar_atualizar(hoje):
    """
    Função que checa se a fato já foi atualizada para a última data necessária (sempre uma segunda-feira).
    Se não, chama a função de atualização.
    """
    if hoje.weekday() != 0:  # 0 = Monday
        logger.info(f"Hoje não é segunda-feira. Buscando última segunda...")
        # Encontra a última segunda-feira
        ultima_segunda = hoje - timedelta(days=hoje.weekday())
        hoje = ultima_segunda

    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM fact_price WHERE date_id = ?", (int(hoje.strftime('%Y%m%d')),))
        count = cursor.fetchone()[0]
        
        if count > 0:
            logger.info(f"Fato já atualizada para {hoje}. Nenhuma ação necessária.")
        else:
            logger.info(f"Fato não encontrada para {hoje}. Iniciando atualização...")
            update_fact_prices(hoje.strftime('%Y-%m-%d'), db_path=DB_PATH)
            
    except Exception as e:
        logger.error(f"Erro ao checar/atualizar fato: {e}", exc_info=True)
    finally:
        conn.close()

def main():
    logger.info("=" * 60)
    logger.info("INICIANDO ATUALIZAÇÃO AUTOMÁTICA DA FACT_PRICE")
    logger.info("=" * 60)

    hoje = datetime.now().date()
    checar_atualizar(hoje)

    logger.info("=" * 60)
    logger.info("ATUALIZAÇÃO DA FACT_PRICE CONCLUÍDA")
    logger.info("=" * 60)

if __name__ == "__main__":
    main()