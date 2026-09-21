import pandas as pd
import sqlite3
from datetime import datetime, timedelta
from xbbg import blp
import logging
from pathlib import Path
from logging.handlers import RotatingFileHandler
import xbbg
xbbg.set_backend("pandas")

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "market_update.db"
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "atualizador_fact_pricing.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding='utf-8'),
    ]
)

logger = logging.getLogger("atualizador_fact_pricing")


# ==========================================
# CALENDARIO / RESOLUCAO DA DATA DE REFERENCIA
# ==========================================

def descobrir_segunda_da_semana(hoje):
    """Retorna a segunda-feira da semana de 'hoje' (podendo ser hoje mesmo)."""
    return hoje - timedelta(days=hoje.weekday())


def tabela_ja_preenchida(conn, tabela, date_id):
    """
    Verifica se ja existe QUALQUER registro na tabela informada para a date_id dada.
    """
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) FROM {tabela} WHERE date_id = ?", (date_id,))
    return cursor.fetchone()[0] > 0


def tabela_ja_preenchida_na_semana(conn, tabela, data_segunda):
    """
    Verifica se ja existe registro na tabela para qualquer dia da semana (segunda a domingo).
    """
    # Define o range da semana: da segunda-feira até domingo (6 dias depois)
    date_id_inicio = int(data_segunda.strftime('%Y%m%d'))
    date_id_fim = int((data_segunda + timedelta(days=6)).strftime('%Y%m%d'))
    
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT COUNT(*) FROM {tabela} WHERE date_id >= ? AND date_id <= ?", 
        (date_id_inicio, date_id_fim)
    )
    return cursor.fetchone()[0] > 0


def resolver_data_referencia(tickers_amostra, segunda):
    """
    Tenta encontrar dado de fechamento (BDH) para a segunda-feira da semana. Se a segunda não teve dados (feriado), recua dia a dia (até 5 dias úteis) até encontrar a última data com dado disponível.

    Usa uma amostra pequena de tickers so para testar se o dia teve dados, evitando bater na Bloomberg com a lista inteira repetidamente.
    """
    data_candidata = segunda
    tentativas = 0
    max_tentativas = 5  # nao recua mais que uma semana util

    while tentativas < max_tentativas:
        data_str = data_candidata.strftime('%Y-%m-%d')
        logger.info(f"Testando disponibilidade em {data_str}...")

        try:
            df_teste = blp.bdh(
                tickers=tickers_amostra, flds=['PX_MID'],
                start_date=data_str, end_date=data_str, Per='D', Fill='P'
            )
        except Exception as e:
            logger.warning(f"Falha ao testar {data_str}: {e}")
            df_teste = pd.DataFrame()

        if not df_teste.empty:
            logger.info(f"Dados confirmados em {data_str}.")
            return data_candidata

        logger.info(f"Sem dado em {data_str} (provavel feriado). Recuando um dia util.")
        data_candidata -= timedelta(days=1)
        # pula fim de semana ao recuar
        while data_candidata.weekday() > 4:
            data_candidata -= timedelta(days=1)
        tentativas += 1

    logger.error(
        f"Nao foi possivel encontrar dados nos {max_tentativas} dias uteis "
        f"anteriores a {segunda}. Abortando atualizacao desta semana."
    )
    return None


def garantir_dim_date(conn, dt_obj):
    date_id = int(dt_obj.strftime('%Y%m%d'))
    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR IGNORE INTO dim_date (date_id, full_date, year, month, quarter, day_of_week)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (
        date_id, dt_obj.strftime('%Y-%m-%d'), dt_obj.year, dt_obj.month,
        (dt_obj.month - 1) // 3 + 1, dt_obj.weekday() + 1
    ))
    conn.commit()
    return date_id


# ==========================================
# BDH -> fact_pricing (preco/yield, serie temporal)
# ==========================================

def atualizar_fact_pricing(conn, tickers, reference_date, date_id):
    logger.info(f"[BDH] Buscando PX_MID/YLD_YTM_MID para {len(tickers)} ticker(s) em {reference_date}...")

    df_bdh = blp.bdh(
        tickers=tickers, flds=['PX_MID', 'YLD_YTM_MID'],
        start_date=reference_date, end_date=reference_date, Per='D', Fill='P'
    )

    if df_bdh.empty:
        logger.warning("[BDH] Retorno vazio da Bloomberg. Nada a gravar em fact_pricing.")
        return 0

    if 'date' in df_bdh.columns:
        df_bdh = df_bdh.drop(columns=['date'])
    if 'field' in df_bdh.columns and 'value' in df_bdh.columns:
        df_bdh = df_bdh.pivot(index='ticker', columns='field', values='value').reset_index()

    for col in ['PX_MID', 'YLD_YTM_MID']:
        if col not in df_bdh.columns:
            df_bdh[col] = None
        df_bdh[col] = pd.to_numeric(df_bdh[col], errors='coerce')

    tickers_solicitados = set(tickers)
    tickers_retornados = set(df_bdh['ticker'].unique())

    tickers_faltantes = sorted(
        tickers_solicitados - tickers_retornados
    )

    logger.info(
        f"[BDH] Solicitados: {len(tickers_solicitados)} | "
        f"Retornados: {len(tickers_retornados)} | "
        f"Faltantes: {len(tickers_faltantes)}"
    )

    if tickers_faltantes:
        logger.warning(
            f"[BDH] {len(tickers_faltantes)} ticker(s) não retornaram da Bloomberg:"
        )

        for ticker in tickers_faltantes:
            logger.warning(f"    {ticker}")


    df_bdh.rename(columns={
        'PX_MID': 'price_mid', 'YLD_YTM_MID': 'ytm_mid', 'ticker': 'bbg_id'
    }, inplace=True)

    df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)
    df_fact = df_bdh.merge(df_assets, on='bbg_id', how='inner')
    df_fact['date_id'] = date_id

    n_sem_match = len(df_bdh) - len(df_fact)
    if n_sem_match > 0:
        logger.warning(f"[BDH] {n_sem_match} ticker(s) retornado(s) pela Bloomberg nao encontrados em dim_security.")

    cols = ['asset_id', 'date_id', 'price_mid', 'ytm_mid']
    df_fact[cols].to_sql('fact_pricing_temp', conn, if_exists='replace', index=False)

    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO fact_pricing (asset_id, date_id, price_mid, ytm_mid)
        SELECT asset_id, date_id, price_mid, ytm_mid FROM fact_pricing_temp
    ''')
    cursor.execute('DROP TABLE fact_pricing_temp')
    conn.commit()

    logger.info(f"[BDH] {len(df_fact)} registro(s) gravado(s) em fact_pricing.")
    return len(df_fact)


# ==========================================
# BDP -> fact_bdp (rating/duration/amt_outstanding, snapshot da coleta)
# ==========================================

def atualizar_fact_bdp(conn, tickers, date_id, collected_at):
    logger.info(f"[BDP] Buscando atributos semanais para {len(tickers)} ticker(s)...")

    campos_bdp = ['AMT_OUTSTANDING', 'RTG_MOODY', 'RTG_SP_LONG', 'RTG_FITCH', 'MTY_DUR_MID', 'BB_COMPOSITE', 'NXT_CALL_DT', 'YLD_YTC_MID', 'NXT_CALL_PX', 'z_sprd_mid']
    df_bdp = blp.bdp(tickers, flds=campos_bdp)

    if df_bdp.empty:
        logger.warning("[BDP] Retorno vazio da Bloomberg. Nada a gravar em fact_bdp.")
        return 0

    if 'field' in df_bdp.columns and 'value' in df_bdp.columns:
        df_bdp = df_bdp.pivot(index='ticker', columns='field', values='value').reset_index()
    else:
        df_bdp = df_bdp.reset_index().rename(columns={'index': 'ticker'})

    for col in campos_bdp:
        if col not in df_bdp.columns:
            df_bdp[col] = None

    df_bdp['AMT_OUTSTANDING'] = pd.to_numeric(df_bdp['AMT_OUTSTANDING'], errors='coerce')
    df_bdp['MTY_DUR_MID'] = pd.to_numeric(df_bdp['MTY_DUR_MID'], errors='coerce')
    df_bdp['YLD_YTC_MID'] = pd.to_numeric(df_bdp['YLD_YTC_MID'], errors='coerce')
    df_bdp['NXT_CALL_PX'] = pd.to_numeric(df_bdp['NXT_CALL_PX'], errors='coerce')
    df_bdp['z_sprd_mid'] = pd.to_numeric(df_bdp['z_sprd_mid'], errors='coerce')

    df_bdp.rename(columns={
        'ticker': 'bbg_id', 'AMT_OUTSTANDING': 'amt_outstanding', 'MTY_DUR_MID': 'duration_mid',
        'RTG_MOODY': 'rating_moody', 'RTG_SP_LONG': 'rating_sp', 'RTG_FITCH': 'rating_fitch', 'BB_COMPOSITE': 'bb_composite',
        'NXT_CALL_DT': 'next_call_dt', 'YLD_YTC_MID': 'next_call_yield', 'NXT_CALL_PX': 'next_call_price', 'z_sprd_mid': 'z_spread'
    }, inplace=True)

    df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)
    df_fact = df_bdp.merge(df_assets, on='bbg_id', how='inner')
    df_fact['date_id'] = date_id
    df_fact['collected_at'] = collected_at

    n_sem_match = len(df_bdp) - len(df_fact)
    if n_sem_match > 0:
        logger.warning(f"[BDP] {n_sem_match} ticker(s) retornado(s) pela Bloomberg nao encontrados em dim_security.")

    cols = ['asset_id', 'date_id', 'collected_at', 'duration_mid', 'amt_outstanding',
            'rating_moody', 'rating_sp', 'rating_fitch', 'bb_composite', 'next_call_dt', 'next_call_yield', 'next_call_price', 'z_spread']
    df_fact[cols].to_sql('fact_bdp_temp', conn, if_exists='replace', index=False)

    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO fact_bdp
        (asset_id, date_id, collected_at, duration_mid, amt_outstanding, rating_moody, rating_sp, rating_fitch, bb_composite, next_call_dt, next_call_yield, next_call_price, z_spread)
        SELECT asset_id, date_id, collected_at, duration_mid, amt_outstanding, rating_moody, rating_sp, rating_fitch, bb_composite, next_call_dt, next_call_yield, next_call_price, z_spread
        FROM fact_bdp_temp
    ''')
    cursor.execute('DROP TABLE fact_bdp_temp')
    conn.commit()

    logger.info(f"[BDP] {len(df_fact)} registro(s) gravado(s) em fact_bdp.")
    return len(df_fact)


# ==========================================
# ORQUESTRACAO
# ==========================================

def checar_atualizar(hoje, db_path=DB_PATH, modo_manual=False):
    """
    Atualiza fact_pricing e fact_bdp.

    modo_manual=False:
        Comportamento normal do ETL:
          - fact_pricing usa a segunda-feira da semana como date_id.
          - Se segunda não teve pregão, usa o último dia útil disponível.
          - fact_bdp usa a data de hoje como date_id.
          - Bloqueia atualização se a semana já estiver preenchida.

    modo_manual=True:
        Ignora os bloqueios da atualização automática e usa HOJE como
        data de referência para ambas as facts.
          - fact_pricing: busca o fechamento de HOJE.
          - fact_bdp: busca o snapshot de HOJE.
          - Permite reexecutar mesmo que já existam dados para hoje.
    """

    # ------------------------------------------------------------------
    # Validação do dia da semana
    # ------------------------------------------------------------------
    if hoje.weekday() == 0 and not modo_manual:
        logger.info(
            "Hoje é segunda-feira. Este ETL roda de terça a sexta; "
            "nada a fazer hoje."
        )
        return

    if hoje.weekday() > 4:
        logger.info("Hoje é fim de semana. Nada a fazer.")
        return

    if modo_manual:
        logger.info(
            "MODO MANUAL ATIVADO: ignorando bloqueios da atualização "
            "automática e usando hoje como referência."
        )

    conn = sqlite3.connect(db_path)

    try:
        df_assets = pd.read_sql(
            "SELECT asset_id, bbg_id FROM dim_security WHERE flag_inactive = 0 AND bbg_id IS NOT NULL",
            conn
        )

        if df_assets.empty:
            logger.warning(
                "Nenhum ativo encontrado em dim_security. Abortando."
            )
            return

        tickers = df_assets['bbg_id'].dropna().tolist()

        # ==============================================================
        # MODO MANUAL
        # ==============================================================
        if modo_manual:

            reference_date_str = hoje.strftime('%Y-%m-%d')
            date_id_hoje = garantir_dim_date(conn, hoje)

            # ----------------------------------------------------------
            # fact_pricing
            # ----------------------------------------------------------
            logger.info(
                f"[BDH][MANUAL] Buscando fechamento de {hoje}."
            )

            n_pricing = atualizar_fact_pricing(
                conn,
                tickers,
                reference_date_str,
                date_id_hoje
            )

            logger.info(
                f"[BDH][MANUAL] Atualizados {n_pricing} registro(s) "
                f"em fact_pricing."
            )

            # ----------------------------------------------------------
            # fact_bdp
            # ----------------------------------------------------------
            agora = datetime.now()
            collected_at = agora.strftime('%Y-%m-%d %H:%M:%S')

            logger.info(
                f"[BDP][MANUAL] Buscando snapshot de {hoje} "
                f"({collected_at})."
            )

            n_bdp = atualizar_fact_bdp(
                conn,
                tickers,
                date_id_hoje,
                collected_at
            )

            logger.info(
                f"[BDP][MANUAL] Atualizados {n_bdp} registro(s) "
                f"em fact_bdp."
            )

            return

        # ==============================================================
        # MODO AUTOMÁTICO
        # ==============================================================
        segunda = descobrir_segunda_da_semana(hoje)
        date_id_segunda = int(segunda.strftime('%Y%m%d'))

        # ---------- fact_pricing (BDH) ----------
        if tabela_ja_preenchida(
            conn,
            'fact_pricing',
            date_id_segunda
        ):
            logger.info(
                f"[BDH] Semana de {segunda} já preenchida "
                f"em fact_pricing. Pulando."
            )
        else:
            amostra = tickers[: min(5, len(tickers))]
            data_ref = resolver_data_referencia(
                amostra,
                segunda
            )

            if data_ref is None:
                logger.error(
                    "[BDH] Não foi possível resolver data de referência. "
                    "fact_pricing não será atualizada."
                )
            else:
                date_id_pricing = garantir_dim_date(
                    conn,
                    segunda
                )

                reference_date_str = data_ref.strftime(
                    '%Y-%m-%d'
                )

                if data_ref != segunda:
                    logger.info(
                        f"[BDH] Segunda ({segunda}) sem dados. "
                        f"Usando fechamento de {data_ref} como referência, "
                        f"gravado sob date_id de {segunda}."
                    )

                n_pricing = atualizar_fact_pricing(
                    conn,
                    tickers,
                    reference_date_str,
                    date_id_pricing
                )

                logger.info(
                    f"[BDH] Semana de {segunda} atualizada: "
                    f"{n_pricing} registro(s) em fact_pricing."
                )

        # ---------- fact_bdp (BDP) ----------
        agora = datetime.now()

        if tabela_ja_preenchida_na_semana(
            conn,
            'fact_bdp',
            segunda
        ):
            logger.info(
                f"[BDP] Snapshot da semana de {segunda} já preenchido "
                f"em fact_bdp. Pulando."
            )
        else:
            date_id_hoje = garantir_dim_date(
                conn,
                hoje
            )

            collected_at = agora.strftime(
                '%Y-%m-%d %H:%M:%S'
            )

            n_bdp = atualizar_fact_bdp(
                conn,
                tickers,
                date_id_hoje,
                collected_at
            )

            logger.info(
                f"[BDP] Snapshot de {hoje} ({collected_at}) atualizado: "
                f"{n_bdp} registro(s) em fact_bdp."
            )

    except Exception as e:
        logger.error(
            f"Erro ao checar/atualizar: {e}",
            exc_info=True
        )

    finally:
        conn.close()


def main():
    logger.info("=" * 60)
    logger.info("INICIANDO ATUALIZACAO (fact_pricing semanal + fact_bdp diario/snapshot)")
    logger.info("=" * 60)

    hoje = datetime.now().date()
    modo_manual = False
    checar_atualizar(hoje, modo_manual=modo_manual)

    logger.info("=" * 60)
    logger.info("ATUALIZACAO CONCLUIDA")
    logger.info("=" * 60)

if __name__ == "__main__":
    main()