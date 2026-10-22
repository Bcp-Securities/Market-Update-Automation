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
    Verifica se já existe registro consolidado na tabela informada para a date_id dada.
    """
    cursor = conn.cursor()
    if tabela == 'fact_pricing':
        # Considera como preenchido apenas se não for um dado parcial
        cursor.execute(f"SELECT COUNT(*) FROM {tabela} WHERE date_id = ? AND partial = 0", (date_id,))
    else:
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
# BDH -> fact_pricing (preco/yield/z_spread, serie temporal)
# ==========================================

FIELDS_PRICING = ['PX_MID', 'YLD_YTM_MID', 'z_sprd_mid']


def _buscar_pricing_bbg(tickers, reference_date, usar_bval):
    """
    Busca PX_MID / YLD_YTM_MID / z_sprd_mid na Bloomberg para uma única data.
    Retorna DataFrame wide (uma linha por ticker) com o ticker ORIGINAL,
    ou None se nada voltou.
    """
    mapa_reverso = {}
    for t in tickers:
        if usar_bval:
            query = t if t.upper().endswith("@BVAL CORP") else f"{t}@BVAL CORP"
        else:
            query = t
        mapa_reverso[query] = t

    try:
        df = blp.bdh(
            tickers=list(mapa_reverso.keys()),
            flds=FIELDS_PRICING,
            start_date=reference_date, end_date=reference_date,
            Per='D', Fill='P'
        )
    except Exception as e:
        logger.warning(f"[BDH] Erro ao buscar ({'BVAL' if usar_bval else 'ORIGINAL'}): {e}")
        return None

    if df is None or df.empty:
        return None

    df = df.copy()
    if 'date' in df.columns:
        df = df.drop(columns=['date'])
    if 'field' in df.columns and 'value' in df.columns:
        df = df.pivot(index='ticker', columns='field', values='value').reset_index()

    # Volta para o ID original
    df['ticker'] = df['ticker'].map(mapa_reverso).fillna(df['ticker'])

    for col in FIELDS_PRICING:
        if col not in df.columns:
            df[col] = None
        df[col] = pd.to_numeric(df[col], errors='coerce')

    # Ticker que veio só com NaN conta como "não retornou" (aciona o fallback)
    df = df.dropna(subset=FIELDS_PRICING, how='all')

    return df if not df.empty else None

def buscar_bb_composite(tickers, data_referencia):
    """
    Busca o rating BB_COMPOSITE dos ativos para uma data específica.

    Retorna um DataFrame com:
        ticker, bb_composite
    """
    if not tickers:
        return pd.DataFrame(columns=["ticker", "bb_composite"])

    data_override = pd.Timestamp(data_referencia).strftime("%Y%m%d")

    df_rating = blp.bdp(
        tickers=tickers,
        flds=["BB_COMPOSITE"],
        RATING_AS_OF_DATE_OVERRIDE=data_override
    )

    if df_rating is None or df_rating.empty:
        return pd.DataFrame(columns=["ticker", "bb_composite"])

    df_rating = df_rating.reset_index()

    # O retorno pode ter ticker/field como colunas ou no índice.
    df_rating = df_rating.rename(columns={
        "value": "bb_composite"
    })

    if "ticker" not in df_rating.columns:
        return pd.DataFrame(columns=["ticker", "bb_composite"])

    if "bb_composite" not in df_rating.columns:
        return pd.DataFrame(columns=["ticker", "bb_composite"])

    return df_rating[["ticker", "bb_composite"]].drop_duplicates(
        subset=["ticker"]
    )

def atualizar_fact_pricing(conn, tickers, reference_date, date_id, is_partial=0):
    tickers_solicitados = set(tickers)
    pendentes = set(tickers)
    frames = []

    # 1. BVAL  2. Fallback sem sufixo
    for estrategia, usar_bval in [('BVAL', True), ('ORIGINAL', False)]:
        if not pendentes:
            break

        logger.info(
            f"[BDH] ({estrategia}) Buscando PX_MID/YLD_YTM_MID/Z_SPREAD "
            f"para {len(pendentes)} ticker(s) em {reference_date}..."
        )

        df_slice = _buscar_pricing_bbg(sorted(pendentes), reference_date, usar_bval)

        if df_slice is None:
            logger.warning(f"[BDH] ({estrategia}) Nenhum dado retornado.")
            continue

        # Garante que só entram tickers que ainda estavam pendentes
        df_slice = df_slice[df_slice['ticker'].isin(pendentes)]
        frames.append(df_slice)
        pendentes -= set(df_slice['ticker'].unique())

        logger.info(f"[BDH] ({estrategia}) Retornados: {len(df_slice)} | Ainda pendentes: {len(pendentes)}")

    if not frames:
        logger.warning("[BDH] Retorno vazio da Bloomberg (BVAL e ORIGINAL). Nada a gravar em fact_pricing.")
        return 0

    df_bdh = pd.concat(frames, ignore_index=True)

    tickers_retornados = set(df_bdh['ticker'].unique())
    tickers_faltantes = sorted(tickers_solicitados - tickers_retornados)

    logger.info(
        f"[BDH] Solicitados: {len(tickers_solicitados)} | "
        f"Retornados: {len(tickers_retornados)} | "
        f"Faltantes: {len(tickers_faltantes)}"
    )

    if tickers_faltantes:
        logger.warning(
            f"[BDH] {len(tickers_faltantes)} ticker(s) não retornaram da Bloomberg "
            f"mesmo após fallback (sem BVAL):"
        )
        for ticker in tickers_faltantes:
            logger.warning(f"    {ticker}")

    df_bdh.rename(columns={
        'PX_MID': 'price_mid', 'YLD_YTM_MID': 'ytm_mid',
        'ticker': 'bbg_id', 'z_sprd_mid': 'z_spread'
    }, inplace=True)

    # Busca o BB_COMPOSITE para a data de referência
    df_bdh['bb_composite'] = None

    try:
        tickers_rating = df_bdh['bbg_id'].dropna().unique().tolist()

        logger.info(
            f"[BDP] Buscando BB_COMPOSITE para "
            f"{len(tickers_rating)} ticker(s) em {reference_date}..."
        )

        df_rating = buscar_bb_composite(
            tickers=tickers_rating,
            data_referencia=reference_date
        )

        if df_rating is not None and not df_rating.empty:
            # Normaliza os tickers retornados pela consulta de rating
            df_rating['ticker'] = (
                df_rating['ticker'].str.split('@').str[0]
            )

            rating_map = (
                df_rating
                .drop_duplicates(subset=['ticker'])
                .set_index('ticker')['bb_composite']
            )

            df_bdh['bb_composite'] = (
                df_bdh['bbg_id'].map(rating_map)
            )

            logger.info(
                f"[BDP] Ratings retornados: "
                f"{df_rating['bb_composite'].notna().sum()}"
            )
        else:
            logger.warning(
                f"[BDP] Nenhum BB_COMPOSITE retornado para {reference_date}."
            )

    except Exception as e:
        # A falha do rating não impede a gravação de preço e yield
        logger.exception(
            f"[BDP] Erro ao buscar BB_COMPOSITE em {reference_date}: {e}"
        )

    df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)
    df_fact = df_bdh.merge(df_assets, on='bbg_id', how='inner')
    df_fact['date_id'] = date_id
    df_fact['partial'] = is_partial

    n_sem_match = len(df_bdh) - len(df_fact)
    if n_sem_match > 0:
        logger.warning(f"[BDH] {n_sem_match} ticker(s) retornado(s) pela Bloomberg nao encontrados em dim_security.")

    cols = ['asset_id', 'date_id', 'price_mid', 'ytm_mid', 'z_spread', 'bb_composite', 'partial']
    df_fact[cols].to_sql('fact_pricing_temp', conn, if_exists='replace', index=False)

    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO fact_pricing (asset_id, date_id, price_mid, ytm_mid, z_spread, bb_composite, partial)
        SELECT asset_id, date_id, price_mid, ytm_mid, z_spread, bb_composite, partial FROM fact_pricing_temp
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

    campos_bdp = ['AMT_OUTSTANDING', 'RTG_MOODY', 'RTG_SP_LONG', 'RTG_FITCH', 'MTY_DUR_MID', 'BB_COMPOSITE', 'NXT_CALL_DT', 'YLD_YTC_MID', 'NXT_CALL_PX']
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

    df_bdp.rename(columns={
        'ticker': 'bbg_id', 'AMT_OUTSTANDING': 'amt_outstanding', 'MTY_DUR_MID': 'duration_mid',
        'RTG_MOODY': 'rating_moody', 'RTG_SP_LONG': 'rating_sp', 'RTG_FITCH': 'rating_fitch', 'BB_COMPOSITE': 'bb_composite',
        'NXT_CALL_DT': 'next_call_dt', 'YLD_YTC_MID': 'next_call_yield', 'NXT_CALL_PX': 'next_call_price'
    }, inplace=True)

    df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)
    df_fact = df_bdp.merge(df_assets, on='bbg_id', how='inner')
    df_fact['date_id'] = date_id
    df_fact['collected_at'] = collected_at

    n_sem_match = len(df_bdp) - len(df_fact)
    if n_sem_match > 0:
        logger.warning(f"[BDP] {n_sem_match} ticker(s) retornado(s) pela Bloomberg nao encontrados em dim_security.")

    cols = ['asset_id', 'date_id', 'collected_at', 'duration_mid', 'amt_outstanding',
            'rating_moody', 'rating_sp', 'rating_fitch', 'bb_composite', 'next_call_dt', 'next_call_yield', 'next_call_price']
    df_fact[cols].to_sql('fact_bdp_temp', conn, if_exists='replace', index=False)

    cursor = conn.cursor()
    cursor.execute('''
        INSERT OR REPLACE INTO fact_bdp
        (asset_id, date_id, collected_at, duration_mid, amt_outstanding, rating_moody, rating_sp, rating_fitch, bb_composite, next_call_dt, next_call_yield, next_call_price)
        SELECT asset_id, date_id, collected_at, duration_mid, amt_outstanding, rating_moody, rating_sp, rating_fitch, bb_composite, next_call_dt, next_call_yield, next_call_price
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
                date_id_hoje,
                is_partial=1
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
        else:
            # CONSOLIDAR DADOS PARCIAIS PENDENTES
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT date_id FROM fact_pricing WHERE partial = 1")
            dias_pendentes = cursor.fetchall()

            for (d_id,) in dias_pendentes:
                d_str = f"{str(d_id)[:4]}-{str(d_id)[4:6]}-{str(d_id)[6:]}"
                data_pendente = datetime.strptime(d_str, '%Y-%m-%d').date()

                if data_pendente < hoje:
                    # Busca exclusivamente os tickers (bbg_id) que estão parciais neste dia específico
                    cursor.execute("""
                        SELECT d.bbg_id 
                        FROM fact_pricing f
                        JOIN dim_security d ON f.asset_id = d.asset_id
                        WHERE f.date_id = ? AND f.partial = 1
                    """, (d_id,))
                    tickers_parciais = [row[0] for row in cursor.fetchall()]

                    if tickers_parciais:
                        logger.info(f"[BDH][AUTO] Consolidando {len(tickers_parciais)} ativo(s) alvo(s) do dia {d_str} que estavam marcados como partial...")
                        n_pricing = atualizar_fact_pricing(
                            conn,
                            tickers_parciais,  # <-- Passa apenas a lista filtrada
                            d_str,      
                            d_id,       
                            is_partial=0 
                        )
                        logger.info(f"[BDH][AUTO] {n_pricing} registro(s) consolidados para {d_str}.")

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