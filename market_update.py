import os
import sqlite3
import datetime
import logging
import time
import pandas as pd
import numpy as np
import urllib.request
from pathlib import Path
from logging.handlers import RotatingFileHandler
from xbbg import blp
import xbbg
xbbg.set_backend("pandas")

# ==========================================
# CONFIGURAÇÕES DE DIRETÓRIO E LOGS
# ==========================================
BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "market_update.db"
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(exist_ok=True)
log_file = LOG_DIR / "atualizador_dados.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        # RotatingFileHandler evita que o log cresça indefinidamente
        RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5, encoding='utf-8'),
        # logging.StreamHandler()
    ]
)

logger = logging.getLogger("atualizador")

# ==========================================
# MAPEAMENTO DA BLOOMBERG
# ==========================================
MAPEAMENTO_BBG = {
    'px_last': {
        'fld': 'px_last',
        'kwargs': {'Per': 'D', 'Fill': 'P'}
    },
    'yield': {
        'fld': 'YLD_YTM_MID',
        'kwargs': {'Per': 'D', 'Fill': 'P', 'PRICING_SOURCE': 'BVAL'}
    }
}

# ==========================================
# FUNÇÕES DE BANCO DE DADOS
# ==========================================
def conectar_banco():
    conn = sqlite3.connect(DB_PATH)
    logger.debug(f"Conectado ao banco em {DB_PATH}")
    return conn

def pegar_metadados(conn, source):
    """Busca as séries cadastradas no banco filtrando pela fonte (BBG ou FRED)."""
    query = """
        SELECT series_code, ticker, bbg_field 
        FROM series_meta 
        WHERE source = ? AND ticker IS NOT NULL
    """
    df = pd.read_sql_query(query, conn, params=(source,))
    logger.info(f"[{source}] {len(df)} série(s) cadastrada(s) encontrada(s) no banco.")
    return df

def descobrir_data_inicial(conn, series_code, data_padrao=datetime.date(2019, 1, 1)):
    """Retorna o dia seguinte ao último dado salvo."""
    cursor = conn.cursor()
    cursor.execute("SELECT MAX(obs_date) FROM time_series WHERE series_code=?", (series_code,))
    resultado = cursor.fetchone()[0]

    if resultado:
        ultima_data = datetime.datetime.strptime(resultado, "%Y-%m-%d").date()
        return ultima_data + datetime.timedelta(days=1)
    return data_padrao

def pegar_ultimo_valor_conhecido(conn, series_code):
    """Usado pelo FRED para preencher buracos (finais de semana) caso a atualização comece num sábado."""
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM time_series WHERE series_code=? ORDER BY obs_date DESC LIMIT 1", (series_code,))
    resultado = cursor.fetchone()
    return resultado[0] if resultado else None

def salvar_no_banco(conn, registros, fonte):
    """Executa o INSERT OR IGNORE no banco de dados."""
    if registros:
        logger.info(f"[{fonte}] Inserindo {len(registros)} novo(s) ponto(s) no banco...")
        cursor = conn.cursor()
        cursor.executemany(
            "INSERT OR IGNORE INTO time_series (series_code, obs_date, value) VALUES (?,?,?)",
            registros
        )
        conn.commit()
        # cursor.rowcount após executemany com INSERT OR IGNORE não é confiável em todos
        # os drivers para refletir quantas linhas realmente foram inseridas (vs ignoradas
        # por já existirem). Se precisar saber exatamente quantas foram novas de fato,
        # dá pra comparar o total de linhas da tabela antes/depois do commit.
        logger.info(f"[{fonte}] Commit realizado com sucesso ({len(registros)} registro(s) enviados).")
    else:
        logger.info(f"[{fonte}] Nenhum dado novo para inserir — base já está atualizada.")

# ==========================================
# FUNÇÕES DO FRED
# ==========================================
def baixar_dados_fred(fred_id, start_date, end_date):
    """Faz a requisição HTTP para o CSV do FRED e retorna um dicionário {data: valor}."""
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={fred_id}&cosd={start_date}&coed={end_date}"
    req = urllib.request.Request(url, headers={"User-Agent": "market-update/3.0"})

    dados = {}
    linhas_invalidas = 0
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            texto = resp.read().decode("utf-8")

        linhas = texto.splitlines()[1:]  # Pula o cabeçalho
        for ln in linhas:
            if not ln.strip():
                continue
            parts = ln.split(",")
            if len(parts) < 2:
                linhas_invalidas += 1
                continue

            iso, raw = parts[0].strip(), parts[1].strip()
            # O FRED usa '.' para dias sem negociação (feriados)
            if raw in ("", "."):
                continue

            dados[iso] = float(raw)

        logger.debug(f"[FRED - {fred_id}] {len(dados)} ponto(s) válido(s) recebido(s), "
                     f"{linhas_invalidas} linha(s) mal formatada(s) ignorada(s).")
    except Exception as e:
        logger.error(f"[FRED - {fred_id}] Erro ao baixar/parsear CSV: {e}", exc_info=True)

    return dados

def preencher_gaps_calendario(dados_reais, data_inicio, data_fim, valor_anterior=None):
    # Filtra o dataset bruto (que pode ter anos de histórico) só para a
    # janela que realmente importa nesta execução.
    dados_na_janela = {
        iso: valor for iso, valor in dados_reais.items()
        if data_inicio <= datetime.datetime.strptime(iso, "%Y-%m-%d").date() <= data_fim
    }

    if not dados_na_janela:
        # Nada publicado ainda dentro da janela pedida (ex.: delay total,
        # ou fim de semana sem nenhum dia útil no meio).
        return []

    datas_publicadas = sorted(
        datetime.datetime.strptime(iso, "%Y-%m-%d").date() for iso in dados_na_janela
    )
    ultima_data_publicada = datas_publicadas[-1]

    preenchido = []
    valor_atual = valor_anterior
    d = data_inicio
    dias_preenchidos_por_gap = 0

    # Para no último dado REAL dentro da janela, não em data_fim — é isso
    # que evita inventar valor para dias ainda não divulgados pelo FRED.
    while d <= ultima_data_publicada:
        iso = d.strftime("%Y-%m-%d")
        if iso in dados_na_janela:
            valor_atual = dados_na_janela[iso]
        else:
            dias_preenchidos_por_gap += 1

        if valor_atual is not None:
            preenchido.append((iso, valor_atual))

        d += datetime.timedelta(days=1)

    if dias_preenchidos_por_gap:
        logger.info(f"{dias_preenchidos_por_gap} dia(s) de gap de calendário preenchido(s) "
                    f"por repetição do valor anterior (fins de semana/feriados).")

    if ultima_data_publicada < data_fim:
        dias_pendentes = (data_fim - ultima_data_publicada).days
        logger.info(f"Última publicação real do FRED dentro da janela: {ultima_data_publicada}. "
                    f"{dias_pendentes} dia(s) até {data_fim} ficaram pendentes para a próxima execução "
                    f"(provável delay de divulgação).")

    return preenchido

# ==========================================
# HELPERS
# ==========================================
def to_date_safe(valor):
    """Converte string/datetime/date/Timestamp/datetime64 para datetime.date de forma explícita.
    Levanta ValueError com contexto claro se o tipo não for reconhecido, em vez de
    deixar a comparação de datas falhar (ou pior, comparar silenciosamente errado)."""
    if isinstance(valor, str):
        return datetime.datetime.strptime(valor, '%Y-%m-%d').date()
    if isinstance(valor, pd.Timestamp):
        return valor.date()
    if isinstance(valor, datetime.datetime):
        return valor.date()
    if isinstance(valor, datetime.date):
        return valor
    if isinstance(valor, np.datetime64):
        return pd.Timestamp(valor).date()
    raise ValueError(f"Tipo de data não reconhecido: {type(valor)} (valor={valor!r})")

def bloomberg_fill_prev(
    df,
    date_col="date",
    value_col="value",
    group_cols=("ticker", "field"),
    freq="D",
):
    """
    Reproduz o comportamento do BQL fill=PREV.

    Parâmetros
    ----------
    df : DataFrame
        DataFrame no formato longo.
    date_col : str
        Nome da coluna de datas.
    value_col : str
        Nome da coluna de valores.
    group_cols : tuple
        Colunas que identificam cada série.
    freq : str
        Frequência do calendário ('D', 'B', etc.)

    Retorna
    -------
    DataFrame
        Mesmo formato do original, porém com as datas faltantes inseridas
        e preenchidas pelo último valor disponível.
    """

    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])

    resultado = []

    for chave, grupo in df.groupby(list(group_cols)):
        grupo = grupo.sort_values(date_col)

        idx = pd.date_range(
            grupo[date_col].min(),
            grupo[date_col].max(),
            freq=freq
        )

        g = (
            grupo
            .set_index(date_col)
            .reindex(idx)
        )

        # recoloca as colunas de agrupamento
        if not isinstance(chave, tuple):
            chave = (chave,)

        for col, valor in zip(group_cols, chave):
            g[col] = valor

        g[value_col] = g[value_col].ffill()

        g = (
            g
            .reset_index()
            .rename(columns={"index": date_col})
        )

        resultado.append(g)

    return (
        pd.concat(resultado, ignore_index=True)
        [list(group_cols) + [date_col, value_col]]
    )

# ==========================================
# ORQUESTRADOR PRINCIPAL
# ==========================================
def main():
    inicio_execucao = time.monotonic()
    logger.info("=" * 60)
    logger.info("INICIANDO ATUALIZAÇÃO GERAL (BLOOMBERG + FRED)")
    logger.info("=" * 60)

    # 1. VALIDAÇÃO DA BLOOMBERG (O Portão)
    logger.info("Verificando se o terminal Bloomberg está logado...")
    try:
        teste = blp.bdp("SPX Index", "px_last")
        if teste.empty:
            raise ValueError("Retorno vazio da API (terminal provavelmente deslogado).")
        logger.info("Bloomberg respondeu normalmente. Prosseguindo com a atualização.")
    except Exception as e:
        logger.error(f"Terminal Bloomberg não logado ou inacessível. ABORTANDO execução completa. "
                     f"Motivo: {e}")
        return

    conn = conectar_banco()
    try:
        data_hoje = datetime.date.today()
        data_final = data_hoje - datetime.timedelta(days=1)  # D-1
        logger.info(f"Data de referência para atualização (D-1): {data_final}")

        total_inseridos_bbg = 0
        total_inseridos_fred = 0

        # ==========================================
        # 2. ATUALIZAÇÃO DA BLOOMBERG
        # ==========================================
        logger.info("-" * 60)
        logger.info("Iniciando extração Bloomberg")
        logger.info("-" * 60)
        df_meta_bbg = pegar_metadados(conn, 'BBG')
        novos_registros_bbg = []
        series_ja_atualizadas_bbg = []
        series_sem_dado_bbg = []

        if not df_meta_bbg.empty:
            grupos_campos = df_meta_bbg.groupby('bbg_field')

            for campo_banco, grupo in grupos_campos:
                if campo_banco not in MAPEAMENTO_BBG:
                    logger.error(f"[BBG] Campo '{campo_banco}' não está mapeado em MAPEAMENTO_BBG. "
                                 f"{len(grupo)} série(s) deste campo serão PULADAS: "
                                 f"{grupo['series_code'].tolist()}")
                    continue

                config_bbg = MAPEAMENTO_BBG[campo_banco]
                fld_bloomberg, kwargs_bloomberg = config_bbg['fld'], config_bbg['kwargs']
                tickers = grupo['ticker'].tolist()

                datas_iniciais = {row['series_code']: descobrir_data_inicial(conn, row['series_code'])
                                  for _, row in grupo.iterrows()}
                data_inicial_grupo = min(datas_iniciais.values())

                if data_inicial_grupo > data_final:
                    logger.info(f"[BBG - {campo_banco}] {len(grupo)} série(s) já atualizada(s) até {data_final}. "
                                f"Nada a fazer.")
                    series_ja_atualizadas_bbg.extend(grupo['series_code'].tolist())
                    continue

                logger.info(f"[BBG - {campo_banco}] Baixando {len(tickers)} ticker(s) de "
                            f"{data_inicial_grupo} a {data_final}: {tickers}")

                try:
                    df_bbg = blp.bdh(
                        tickers=tickers,
                        flds=[fld_bloomberg],
                        start_date=data_inicial_grupo.strftime('%Y-%m-%d'),
                        end_date=data_final.strftime('%Y-%m-%d'),
                        **kwargs_bloomberg
                    )
                except Exception as e:
                    logger.error(f"[BBG - {campo_banco}] Falha na chamada blp.bdh: {e}", exc_info=True)
                    continue

                if df_bbg.empty:
                    logger.warning(f"[BBG - {campo_banco}] blp.bdh retornou vazio para os tickers {tickers}.")
                    continue

                if not {'ticker', 'date', 'value'}.issubset(df_bbg.columns):
                    logger.error(
                        f"[BBG - {campo_banco}] Formato inesperado no retorno de blp.bdh: colunas "
                        f"encontradas = {list(df_bbg.columns)}. O código espera colunas "
                        f"'ticker'/'date'/'value'. Pulando este grupo — CONFIRME o formato de retorno "
                        f"do xbbg no seu ambiente (pode estar em formato largo/MultiIndex)."
                    )
                    continue

                try:
                    df_bbg = bloomberg_fill_prev(df_bbg)
                except Exception as e:
                    logger.error(f"[BBG - {campo_banco}] Falha ao preencher valores anteriores: {e}", exc_info=True)
                    continue

                for _, row in grupo.iterrows():
                    series_code, ticker_banco = row['series_code'], row['ticker']
                    df_ticker = df_bbg[df_bbg['ticker'] == ticker_banco]

                    if df_ticker.empty:
                        logger.warning(f"[BBG - {campo_banco}] Ticker '{ticker_banco}' (série {series_code}) "
                                       f"não retornou nenhum dado no período solicitado. Verifique se o "
                                       f"ticker está correto/ativo.")
                        series_sem_dado_bbg.append(series_code)
                        continue

                    data_corte_serie = datas_iniciais[series_code]
                    pontos_inseridos_serie = 0
                    pontos_nan_serie = 0
                    pontos_com_erro_serie = 0

                    for _, linha_bbg in df_ticker.iterrows():
                        valor = linha_bbg['value']
                        if pd.isna(valor):
                            pontos_nan_serie += 1
                            continue

                        try:
                            data_nativa = to_date_safe(linha_bbg['date'])
                        except ValueError as e:
                            pontos_com_erro_serie += 1
                            logger.error(f"[BBG - {series_code}] {e} — ponto ignorado.")
                            continue

                        if data_nativa >= data_corte_serie:
                            novos_registros_bbg.append((series_code, data_nativa.strftime('%Y-%m-%d'), float(valor)))
                            pontos_inseridos_serie += 1

                    logger.info(f"[BBG - {series_code}] {pontos_inseridos_serie} ponto(s) novo(s) "
                                f"({pontos_nan_serie} NaN ignorado(s)"
                                + (f", {pontos_com_erro_serie} com erro de data" if pontos_com_erro_serie else "")
                                + ").")

            total_inseridos_bbg = len(novos_registros_bbg)
            salvar_no_banco(conn, novos_registros_bbg, 'Bloomberg')
        else:
            logger.warning("Nenhuma série da Bloomberg cadastrada no banco (series_meta).")

        # ==========================================
        # 3. ATUALIZAÇÃO DO FRED
        # ==========================================
        logger.info("-" * 60)
        logger.info("Iniciando extração FRED")
        logger.info("-" * 60)
        df_meta_fred = pegar_metadados(conn, 'FRED')
        novos_registros_fred = []
        series_ja_atualizadas_fred = []
        series_sem_dado_fred = []

        if not df_meta_fred.empty:
            for _, row in df_meta_fred.iterrows():
                series_code = row['series_code']
                fred_id = row['ticker']  # No banco antigo, o ticker salva o ID do FRED

                try:
                    data_inicial = descobrir_data_inicial(conn, series_code)
                    if data_inicial > data_final:
                        logger.info(f"[FRED - {series_code}] Já atualizada até {data_final}. Nada a fazer.")
                        series_ja_atualizadas_fred.append(series_code)
                        continue

                    logger.info(f"[FRED - {series_code}] Baixando '{fred_id}' de {data_inicial} a {data_final}...")

                    dados_brutos = baixar_dados_fred(fred_id, data_inicial.strftime('%Y-%m-%d'),
                                                      data_final.strftime('%Y-%m-%d'))

                    if dados_brutos:
                        valor_anterior = pegar_ultimo_valor_conhecido(conn, series_code)
                        dados_preenchidos = preencher_gaps_calendario(dados_brutos, data_inicial, data_final, valor_anterior)

                        for iso, valor in dados_preenchidos:
                            novos_registros_fred.append((series_code, iso, valor))

                        logger.info(f"[FRED - {series_code}] {len(dados_brutos)} ponto(s) recebido(s) no total "
                                    f"pela API (histórico completo), {len(dados_preenchidos)} ponto(s) novo(s) "
                                    f"gerado(s) dentro da janela {data_inicial} a {data_final}.")
                    else:
                        logger.warning(f"[FRED - {series_code}] Nenhum dado retornado pela API para '{fred_id}'.")
                        series_sem_dado_fred.append(series_code)

                except Exception as e:
                    logger.error(f"[FRED - {series_code}] Erro inesperado ao processar a série: {e}", exc_info=True)
                    continue

            total_inseridos_fred = len(novos_registros_fred)
            salvar_no_banco(conn, novos_registros_fred, 'FRED')
        else:
            logger.warning("Nenhuma série do FRED cadastrada no banco (series_meta).")

        # ==========================================
        # 4. RESUMO FINAL
        # ==========================================
        duracao = time.monotonic() - inicio_execucao
        logger.info("=" * 60)
        logger.info("RESUMO DA EXECUÇÃO")
        logger.info(f"  Bloomberg : {total_inseridos_bbg} registro(s) inserido(s) | "
                    f"{len(series_ja_atualizadas_bbg)} série(s) já atualizada(s) | "
                    f"{len(series_sem_dado_bbg)} série(s) sem retorno")
        logger.info(f"  FRED      : {total_inseridos_fred} registro(s) inserido(s) | "
                    f"{len(series_ja_atualizadas_fred)} série(s) já atualizada(s) | "
                    f"{len(series_sem_dado_fred)} série(s) sem retorno")
        if series_sem_dado_bbg:
            logger.warning(f"  Séries BBG sem retorno: {series_sem_dado_bbg}")
        if series_sem_dado_fred:
            logger.warning(f"  Séries FRED sem retorno: {series_sem_dado_fred}")
        logger.info(f"  Tempo total: {duracao:.1f}s")
        logger.info("=" * 60)
        logger.info("ATUALIZAÇÃO FINALIZADA")

    finally:
        conn.close()
        logger.info("Conexão com o banco encerrada.")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.critical(f"CRASH FATAL NÃO TRATADO: {e}", exc_info=True)
        raise