from datetime import datetime, date, timedelta
import pandas as pd
import logging
from flask import Flask, render_template, request, jsonify
import sqlite3
from pathlib import Path
import xbbg
from xbbg import blp
xbbg.set_backend("pandas")

PROJECT_ROOT = Path(__file__).resolve().parent
DB_PATH = PROJECT_ROOT / "market_update.db"

app = Flask(__name__)
app.secret_key = 'chave'


# =====================================================================
# ======================  CAMADA DE BACK-END  =========================
# =====================================================================

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

def bloomberg_fill_zero(
    df,
    date_col="date",
    value_col="value",
    group_cols=("ticker", "field"),
    freq="D",
):
    """
    Reproduz o shape contínuo de datas, mas preenche dias sem negociação com 0.
    """
    df = df.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    resultado = []

    for chave, grupo in df.groupby(list(group_cols)):
        grupo = grupo.sort_values(date_col)
        if grupo.empty: continue
        
        idx = pd.date_range(grupo[date_col].min(), grupo[date_col].max(), freq=freq)
        g = grupo.set_index(date_col).reindex(idx)
        
        if not isinstance(chave, tuple):
            chave = (chave,)
        for col, valor in zip(group_cols, chave):
            g[col] = valor

        # Preenche os volumes faltantes com 0
        g[value_col] = g[value_col].fillna(0)
        g = g.reset_index().rename(columns={"index": date_col})
        resultado.append(g)

    if not resultado:
        return pd.DataFrame(columns=list(group_cols) + [date_col, value_col])

    return pd.concat(resultado, ignore_index=True)[list(group_cols) + [date_col, value_col]]

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

def garantir_dim_date_range(conn, datas):
    ids = {}
    for dt_obj in datas:
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
        ids[dt_obj.strftime('%Y-%m-%d')] = date_id
    return ids

def bloomberg_consultar_ativo(bloomberg_id):
    """
    Consulta um ID na Bloomberg e retorna os campos que serão exibidos
    para o usuário confirmar antes de inserir no banco (Aba 1).

    Parâmetros:
        bloomberg_id (str): ID/ticker informado pelo usuário.

    Retorno esperado (dict) ou None se o ativo não for encontrado:
        {
            "bbg_id": str,
            "isin": str,
            "ticker": str,
            "coupon": float,
            "maturity": str,
            "issue_date": str,
            "industry_group": str,
            "cntry_of_risk": str,
            "issuer": str,
            "currency": str,
            "collateral": str,
            "amt_issuance": float,
            "min_piece": float,
        }
    """
    if not bloomberg_id:
        return None

    campos_estaticos = ['TICKER', 'CPN', 'MATURITY', 'issue_dt', 'BICS_LEVEL_2_INDUSTRY_GROUP_NAME', 'ISSUER', 'ID_ISIN', 'CRNCY', 'PAYMENT_RANK', 'AMT_ISSUED', 'MIN_PIECE', 'cntry_of_risk']
    
    try:
        df_bdp = blp.bdp([bloomberg_id], flds=campos_estaticos)

        if df_bdp.empty:
            return None

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
            'MIN_PIECE': 'min_piece',
            'cntry_of_risk': 'cntry_of_risk'
        }, inplace=True)

        def obter_valor(df, col):
            if col in df.columns and not df[col].isnull().all():
                return df[col].iloc[0]
            return None
        
        return {
            "bbg_id": bloomberg_id,
            "isin": obter_valor(df_bdp, 'isin'),
            "ticker": obter_valor(df_bdp, 'ticker'),
            "coupon": obter_valor(df_bdp, 'coupon'),
            "maturity": obter_valor(df_bdp, 'maturity'),
            "issue_date": obter_valor(df_bdp, 'issue_date'),
            "industry_group": obter_valor(df_bdp, 'industry_group'),
            "cntry_of_risk": obter_valor(df_bdp, 'cntry_of_risk'),
            "issuer": obter_valor(df_bdp, 'issuer'),
            "currency": obter_valor(df_bdp, 'currency'),
            "collateral": obter_valor(df_bdp, 'collateral'),
            "amt_issuance": obter_valor(df_bdp, 'amt_issuance'),
            "min_piece": obter_valor(df_bdp, 'min_piece'),
        }
    except Exception as e:
        print(f"Erro ao consultar Bloomberg para ID {bloomberg_id}: {e}")
        return None


def db_inserir_ativo(dados):
    """
    Insere um novo ativo confirmado pelo usuário no banco de dados (Aba 1).

    Parâmetros:
        dados (dict): mesmo formato retornado por bloomberg_consultar_ativo.

    Retorno esperado (dict):
        {"sucesso": bool, "mensagem": str}
    """
    if not dados.get('bbg_id'):
        return {"sucesso": False, "mensagem": "Dados inválidos. Informe um ID Bloomberg."}

    conn = sqlite3.connect(DB_PATH)

    try:
        df = pd.read_sql("SELECT 1 FROM dim_security WHERE bbg_id = ?", conn, params=(dados['bbg_id'],))
        if not df.empty:
            return {"sucesso": False, "mensagem": f"Ativo {dados.get('bbg_id')} já está cadastrado."}

        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO dim_security (bbg_id, ticker, coupon, maturity, issue_date, industry_group, issuer, isin, currency, collateral, amt_issuance, min_piece, cntry_of_risk, bbg_id_regs, bbg_id_144a)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (dados['bbg_id'], dados['ticker'], dados['coupon'], dados['maturity'], dados['issue_date'], dados['industry_group'], dados['issuer'], dados['isin'], dados['currency'], dados['collateral'], dados['amt_issuance'], dados['min_piece'], dados['cntry_of_risk'], dados.get('bbg_id_regs'), dados.get('bbg_id_144a')))

        conn.commit()
    finally:
        conn.close()
    
    return {"sucesso": True, "mensagem": f"Ativo {dados.get('bbg_id')} salvo com sucesso."}


def db_listar_ativos_resumo():
    """
    Retorna a lista de ativos já cadastrados no banco, no formato mínimo
    necessário para popular os dropdowns pesquisáveis (Abas 2 e 3).

    Retorno esperado (list[dict]):
        [{"bbg_id": str, "label": str}, ...]
    """

    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql("SELECT bbg_id, ticker, coupon, maturity, issuer FROM dim_security", conn)
    except Exception as e:
        print(f"Erro ao listar ativos do banco: {e}")
        return []
    finally:
        conn.close()

    resumo = []
    for _, row in df.iterrows():
        bbg_id = row['bbg_id']
        ticker = row['ticker'] or ''
        coupon = row['coupon'] or ''
        maturity = row['maturity'] or ''
        issuer = row['issuer'] or ''

        if maturity and isinstance(maturity, str):
            try:
                maturity = pd.to_datetime(maturity).strftime('%d/%m/%Y')
            except Exception:
                pass

        label = f"{ticker} {coupon} {maturity} - {issuer}" if ticker else bbg_id
        resumo.append({"bbg_id": bbg_id, "label": label})
    
    return resumo


def db_obter_schema_ativo():
    """
    Descreve as colunas editáveis da tabela de ativos (Aba 2), para que o
    front-end construa o formulário de edição DINAMICAMENTE. Sempre que
    uma coluna for criada, removida ou tiver o tipo alterado no banco,
    basta ajustar esta lista — nenhum HTML/JS precisa ser tocado.
 
    Retorno esperado (list[dict]), na ordem em que os campos devem
    aparecer no formulário:
        [
            {
                "nome": str,        # chave do campo (bate com a coluna no banco)
                "label": str,       # texto exibido acima do campo
                "tipo": str,        # ver tipos suportados abaixo
                "somente_leitura": bool,   # opcional, default False
                "opcoes": [str, ...],      # obrigatório apenas se tipo == "select"
            },
            ...
        ]
 
    Tipos suportados pelo front-end (static/app.js -> criarCampo):
        "text"      -> <input type="text">
        "number"    -> <input type="number">
        "date"      -> <input type="date">  (espera valor "YYYY-MM-DD")
        "textarea"  -> <textarea>
        "select"    -> <select> preenchido a partir de "opcoes"
        "boolean"   -> <input type="checkbox">
 
    Qualquer tipo não reconhecido cai no padrão "text".
    """

    return [
        { "nome": "asset_id", "label": "ID do ativo", "tipo": "number", "somente_leitura": True, }, 
        { "nome": "bbg_id", "label": "Bloomberg ID", "tipo": "text", "somente_leitura": True, }, 
        { "nome": "bbg_id_regs", "label": "Bloomberg ID Reg S", "tipo": "text", }, 
        { "nome": "bbg_id_144a", "label": "Bloomberg ID 144A", "tipo": "text", }, 
        { "nome": "isin", "label": "ISIN", "tipo": "text", }, 
        { "nome": "ticker", "label": "Ticker", "tipo": "text", }, 
        { "nome": "coupon", "label": "Coupon", "tipo": "number", }, 
        { "nome": "maturity", "label": "Maturity", "tipo": "date", }, 
        { "nome": "issue_date", "label": "Issue Date", "tipo": "date", }, 
        { "nome": "industry_group", "label": "Industry Group", "tipo": "text", }, 
        { "nome": "cntry_of_risk", "label": "Country of Risk", "tipo": "text", }, 
        { "nome": "issuer", "label": "Issuer", "tipo": "text", }, 
        { "nome": "currency", "label": "Currency", "tipo": "text", }, 
        { "nome": "collateral", "label": "Collateral", "tipo": "text", }, 
        { "nome": "amt_issuance", "label": "Amount Issuance", "tipo": "number", }, 
        { "nome": "min_piece", "label": "Min. Piece", "tipo": "number", }, 
    ]


def db_obter_ativo(ativo_id):
    """
    Retorna os dados completos e atuais de um ativo específico do banco,
    para exibição e edição (Aba 2).

    Parâmetros:
        ativo_id (str): ID interno do ativo.

    Retorno esperado (dict) ou None se não encontrado:
        {
            "asset_id": int,
            "bbg_id": str,
            "bbg_id_regs": str,
            "bbg_id_144a": str,
            "isin": str,
            "ticker": str,
            "coupon": float,
            "maturity": str,
            "issue_date": str,
            "industry_group": str,
            "cntry_of_risk": str,
            "issuer": str,
            "currency": str,
            "collateral": str,
            "amt_issuance": float,
            "min_piece": float,
        }
    """
    conn = sqlite3.connect(DB_PATH)

    try:
        df = pd.read_sql("SELECT * FROM dim_security WHERE bbg_id = ?", conn, params=(ativo_id,))
    except Exception as e:
        print(f"Erro ao obter dados do ativo {ativo_id} no banco: {e}")
    finally:
        conn.close()

    return df.to_dict(orient='records')[0] if not df.empty else None


def db_atualizar_ativo(dados):
    """
    Salva as edições feitas pelo usuário em um ativo existente (Aba 2).

    Parâmetros:
        dados (dict): mesmo formato retornado por db_obter_ativo, com os
                      campos possivelmente editados.

    Retorno esperado (dict):
        {"sucesso": bool, "mensagem": str}
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()

        dados = {
            chave: None if isinstance(valor, str) and valor.strip() == "" else valor 
            for chave, valor in dados.items()
        }

        cursor.execute("""
            UPDATE dim_security SET 
            bbg_id_regs = ?, 
            bbg_id_144a = ?, 
            isin = ?, 
            ticker = ?, 
            coupon = ?, 
            maturity = ?, 
            issue_date = ?, 
            industry_group = ?, 
            cntry_of_risk = ?, 
            issuer = ?, 
            currency = ?, 
            collateral = ?, 
            amt_issuance = ?, 
            min_piece = ?
            WHERE bbg_id = ? AND asset_id = ?
        """, (
            dados.get('bbg_id_regs'),
            dados.get('bbg_id_144a'),
            dados.get('isin'),
            dados.get('ticker'),
            dados.get('coupon'),
            dados.get('maturity'),
            dados.get('issue_date'),
            dados.get('industry_group'),
            dados.get('cntry_of_risk'),
            dados.get('issuer'),
            dados.get('currency'),
            dados.get('collateral'),
            dados.get('amt_issuance'),
            dados.get('min_piece'),
            dados.get('bbg_id'),
            dados.get('asset_id')
        ))
        conn.commit()
    except Exception as e:
        print(f"Erro ao atualizar ativo {dados.get('bbg_id')}: {e}")
        return {"sucesso": False, "mensagem": f"Erro ao atualizar ativo {dados.get('bbg_id')}"}
    finally:
        conn.close()

    return {"sucesso": True, "mensagem": f"Ativo {dados.get('bbg_id')} atualizado com sucesso."}


def obter_datas_existentes(conn, tabela_fato, bbg_id, data_inicial, data_final, series_type=None, ignore_partial=False):
    """Retorna um set() com as datas (datetime.date) que já existem no banco para o ativo e período."""
    query = f"""
        SELECT d.full_date 
        FROM {tabela_fato} f
        JOIN dim_security s ON f.asset_id = s.asset_id
        JOIN dim_date d ON f.date_id = d.date_id
        WHERE s.bbg_id = ? AND d.full_date BETWEEN ? AND ?
    """
    params = [bbg_id, data_inicial.strftime('%Y-%m-%d'), data_final.strftime('%Y-%m-%d')]
    
    if series_type:
        query += " AND f.series_type = ?"
        params.append(series_type)
        
    # SE ignore_partial FOR TRUE, SÓ CONSIDERA COMO "EXISTENTE" O QUE FOR DEFINITIVO (0)
    if ignore_partial and tabela_fato == 'fact_trading_volume':
        query += " AND f.volume_partial = 0"
        
    df = pd.read_sql(query, conn, params=tuple(params))
    return set(pd.to_datetime(df['full_date']).dt.date)

def calcular_fatias_faltantes(datas_existentes, data_inicial, data_final):
    """Compara o período total com o que existe no banco e retorna fatias (inicio, fim) dos buracos."""
    todas_datas = pd.date_range(start=data_inicial, end=data_final).date
    faltantes = sorted(set(todas_datas) - datas_existentes)

    if not faltantes:
        return []
        
    fatias = []
    inicio = faltantes[0]
    anterior = faltantes[0]
    
    for atual in faltantes[1:]:
        # Se pular mais de 1 dia, quebra a fatia
        if atual != anterior + timedelta(days=1):
            fatias.append((inicio, anterior))
            inicio = atual
        anterior = atual
        
    fatias.append((inicio, anterior))
    return fatias

def preencher_tipo1(lista_ativo_ids, data_inicial, data_final):
    """
    Preenchimento em lote — Tipo 1 (Preço e Yield).

    Parâmetros:
        lista_ativo_ids (list[str])
        data_inicial (str): "YYYY-MM-DD"
        data_final (str): "YYYY-MM-DD"

    Retorno esperado (dict):
        {
            "sucesso": bool,
            "mensagem": str,
            "detalhes": [{"id": str, "status": "ok"/"erro", "mensagem": str}, ...]
        }
    """
    
    if not isinstance(data_inicial, datetime) and not isinstance(data_inicial, date):
        data_inicial = datetime.strptime(data_inicial, "%Y-%m-%d").date()
    if not isinstance(data_final, datetime) and not isinstance(data_final, date):
        data_final = datetime.strptime(data_final, "%Y-%m-%d").date()

    datas = pd.date_range(start=data_inicial, end=data_final).to_pydatetime().tolist()
    conn = sqlite3.connect(DB_PATH)
    ids_map = garantir_dim_date_range(conn, datas)

    try:
        # 1. Mapeia fatias e datas faltantes por ativo
        grupos_fatias = {}
        tickers_solicitados = set(lista_ativo_ids)
        faltantes_por_ativo = {} # Para o filtro final
        
        for ativo_id in lista_ativo_ids:
            existentes = obter_datas_existentes(conn, 'fact_pricing', ativo_id, data_inicial, data_final)
            faltantes = sorted(set(pd.date_range(start=data_inicial, end=data_final).date) - existentes)
            
            if faltantes:
                faltantes_por_ativo[ativo_id] = set(faltantes)
                fatias = tuple(calcular_fatias_faltantes(existentes, data_inicial, data_final))
                grupos_fatias.setdefault(fatias, []).append(ativo_id)
            else:
                tickers_solicitados.discard(ativo_id)

        # 2. Chama a Bloomberg por fatias
        frames_bdh = []
        for fatias, tickers_grupo in grupos_fatias.items():
            for dt_ini, dt_fim in fatias:
                df_slice = blp.bdh(
                    tickers=tickers_grupo, flds=['PX_MID', 'YLD_YTM_MID'],
                    start_date=dt_ini, end_date=dt_fim, Per='D' # <-- Removido Fill='P'
                )
                if not df_slice.empty:
                    frames_bdh.append(df_slice)

        if not frames_bdh:
            conn.close()
            return {"sucesso": True, "mensagem": "Nenhum dado novo precisou ser baixado (banco atualizado).", "detalhes": []}

        # 3. Concatena tudo
        df_bdh_raw = pd.concat(frames_bdh, ignore_index=True)
        
        # 4. Aplica o ffill na série inteira e preenche os gaps 
        df_bdh = bloomberg_fill_prev(df_bdh_raw)

        # 5. Remove as datas que já existiam no banco ("erradas" após o ffill)
        df_bdh['date_date'] = pd.to_datetime(df_bdh['date']).dt.date
        df_bdh['filter_key'] = df_bdh['ticker'] + "_" + df_bdh['date_date'].astype(str)
        
        valid_keys = set(
            f"{tck}_{dt}" 
            for tck, faltantes in faltantes_por_ativo.items() 
            for dt in faltantes
        )
        df_bdh = df_bdh[df_bdh['filter_key'].isin(valid_keys)].drop(columns=['date_date', 'filter_key'])

        if 'field' in df_bdh.columns and 'value' in df_bdh.columns:
            df_bdh = df_bdh.pivot(index=['ticker', 'date'], columns='field', values='value').reset_index()

        for col in ['PX_MID', 'YLD_YTM_MID']:
            if col not in df_bdh.columns:
                df_bdh[col] = None
            df_bdh[col] = pd.to_numeric(df_bdh[col], errors='coerce')

        tickers_solicitados = set(lista_ativo_ids)
        tickers_retornados = set(df_bdh['ticker'].unique())
    
        tickers_faltantes = sorted(
            tickers_solicitados - tickers_retornados
        )

        df_bdh.rename(columns={
            'PX_MID': 'price_mid', 'YLD_YTM_MID': 'ytm_mid', 'ticker': 'bbg_id'
        }, inplace=True)
    
        df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)
        df_fact = df_bdh.merge(df_assets, on='bbg_id', how='inner')
        df_fact['date_id'] = df_fact['date'].apply(lambda x: ids_map[x.strftime("%Y-%m-%d")])
        
        cols = ['asset_id', 'date_id', 'price_mid', 'ytm_mid']
        df_fact[cols].to_sql('fact_pricing_temp', conn, if_exists='replace', index=False)

        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO fact_pricing (asset_id, date_id, price_mid, ytm_mid)
            SELECT asset_id, date_id, price_mid, ytm_mid FROM fact_pricing_temp
        ''')
        cursor.execute('DROP TABLE fact_pricing_temp')
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Erro ao preencher preço e yield: {e}")
        return {
            "sucesso": False,
            "mensagem": f"Erro ao preencher preço e yield: {e}",
            "detalhes": [
                {"id": ativo_id, "status": "erro", "mensagem": str(e)}
                for ativo_id in lista_ativo_ids
            ],
        }
    finally:
        conn.close()

    detalhes = [
        {"id": ativo_id, "status": "ok", "mensagem": "Preço e Yield preenchidos."}
        if ativo_id not in tickers_faltantes else
        {"id": ativo_id, "status": "erro", "mensagem": "Dados faltantes."}
        for ativo_id in lista_ativo_ids
    ]
    return {
        "sucesso": True,
        "mensagem": f"Preço e Yield concluído para {len(lista_ativo_ids)} ativo(s).",
        "detalhes": detalhes,
    }


def preencher_tipo2(lista_ativo_ids):
    """
    Preenchimento em lote, Tipo 2 (Holders). Usa BDS, então não recebe datas.

    Para cada ativo, consulta na Bloomberg os IDs bbg_id_regs e/ou
    bbg_id_144a que estiverem preenchidos (o bbg_id "padrão" NÃO é usado).
    Cada execução grava um novo snapshot em fact_holders, carimbado com o
    date_id do dia da execução — histórico é acumulado, não sobrescrito
    entre dias diferentes (dentro do mesmo dia, reprocessar substitui via
    INSERT OR REPLACE, respeitando a PK da tabela).

    Retorno: mesmo formato dos outros tipos.
    """
    ids_por_ativo = db_obter_ids_holders(lista_ativo_ids)
    detalhes = []

    conn = sqlite3.connect(DB_PATH)
    date_id = garantir_dim_date(conn, datetime.now().date())
    df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)

    colunas_saida = [
        "asset_id", "date_id", "holder_name", "holder_id",
        "position_thousand", "position_change_thousand", "filing_date",
        "filing_source", "insider_status", "percent_outstanding",
        "institution_type", "metro_area", "country", "series_type",
    ]

    try:
        for ativo_id in lista_ativo_ids:
            regs = ids_por_ativo[ativo_id]["bbg_id_regs"]
            a144 = ids_por_ativo[ativo_id]["bbg_id_144a"]
            ids_consulta = {}

            if regs:
                ids_consulta["RegS"] = regs
            if a144:
                ids_consulta["144A"] = a144

            if not ids_consulta:
                detalhes.append({"id": ativo_id, "status": "erro",
                                 "mensagem": "Sem BBG ID RegS/144A cadastrado."})
                continue

            frames_ativo = []
            erro_ativo = None

            for tipo, id_consulta in ids_consulta.items():
                # Formata o ID adicionando @TRAC CORP se não existir
                id_consulta_bbg = id_consulta if id_consulta.upper().endswith("@TRAC CORP") else f"{id_consulta}@TRAC CORP"
                try:
                    print(f"[Holders] {ativo_id} -> consultando {tipo}: {id_consulta}")
                    df = blp.bds(id_consulta_bbg, "ALL_HOLDERS_PUBLIC_FILINGS")
                    df.columns = df.columns.str.strip()

                    if df.empty:
                        continue

                    df = df.rename(columns={
                        "Holder Name": "holder_name",
                        "Holder Id": "holder_id",
                        "Position": "position_thousand",
                        "Position Change": "position_change_thousand",
                        "Filing Date": "filing_date",
                        "Filing Source": "filing_source",
                        "Insider Status": "insider_status",
                        "Percent Outstanding": "percent_outstanding",
                        "Institution Type": "institution_type",
                        "Metro Area": "metro_area",
                        "Country": "country",
                    })
                    df["series_type"] = tipo
                    df["bbg_id"] = ativo_id
                    frames_ativo.append(df)
                except Exception as e:
                    # Falha isolada por sub-id (RegS/144A): não derruba o
                    # outro sub-id do mesmo ativo, nem os demais ativos.
                    print(f"Erro ao consultar Holders ({tipo}) para {ativo_id}: {e}")
                    erro_ativo = str(e)

            if not frames_ativo:
                if erro_ativo:
                    detalhes.append({"id": ativo_id, "status": "erro",
                                     "mensagem": f"Falha ao consultar Holders: {erro_ativo}"})
                else:
                    detalhes.append({"id": ativo_id, "status": "erro",
                                     "mensagem": "Nenhum holder retornado pela Bloomberg."})
                continue

            df_ativo = pd.concat(frames_ativo, ignore_index=True)

            # Garante presença de todas as colunas esperadas antes de tipar
            for col in ["holder_name", "holder_id", "position_thousand",
                        "position_change_thousand", "filing_date", "filing_source",
                        "insider_status", "percent_outstanding", "institution_type",
                        "metro_area", "country"]:
                if col not in df_ativo.columns:
                    df_ativo[col] = None

            df_ativo["position_thousand"] = pd.to_numeric(df_ativo["position_thousand"], errors="coerce")
            df_ativo["position_change_thousand"] = pd.to_numeric(df_ativo["position_change_thousand"], errors="coerce")
            df_ativo["percent_outstanding"] = pd.to_numeric(df_ativo["percent_outstanding"], errors="coerce")
            df_ativo["filing_date"] = pd.to_datetime(df_ativo["filing_date"], errors="coerce").dt.strftime("%Y-%m-%d")

            # holder_id é parte da PK: linha sem holder_id não pode ser gravada
            antes = len(df_ativo)
            df_ativo = df_ativo.dropna(subset=["holder_id"])
            descartadas = antes - len(df_ativo)
            if descartadas:
                print(f"[Holders] {ativo_id}: {descartadas} linha(s) sem holder_id descartada(s).")

            if df_ativo.empty:
                detalhes.append({"id": ativo_id, "status": "erro",
                                 "mensagem": "Holders retornados sem holder_id válido."})
                continue

            df_ativo = df_ativo.merge(df_assets, on="bbg_id", how="inner")
            if df_ativo.empty:
                detalhes.append({"id": ativo_id, "status": "erro",
                                 "mensagem": "Ativo não encontrado em dim_security."})
                continue

            df_ativo["date_id"] = date_id

            df_ativo[colunas_saida].to_sql("fact_holders_temp", conn, if_exists="replace", index=False)
            cursor = conn.cursor()
            cursor.execute(f'''
                INSERT OR REPLACE INTO fact_holders ({", ".join(colunas_saida)})
                SELECT {", ".join(colunas_saida)} FROM fact_holders_temp
            ''')
            cursor.execute("DROP TABLE fact_holders_temp")
            conn.commit()

            detalhes.append({"id": ativo_id, "status": "ok",
                             "mensagem": f"{len(df_ativo)} holder(s) gravado(s) "
                                         f"({len(ids_consulta)} ID(s) consultado(s))."})
    except Exception as e:
        conn.rollback()
        print(f"Erro geral ao preencher holders: {e}")
        return {
            "sucesso": False,
            "mensagem": f"Erro ao preencher holders: {e}",
            "detalhes": detalhes or [
                {"id": ativo_id, "status": "erro", "mensagem": str(e)}
                for ativo_id in lista_ativo_ids
            ],
        }
    finally:
        conn.close()

    sucesso = all(d["status"] == "ok" for d in detalhes)
    return {
        "sucesso": sucesso,
        "mensagem": f"Holders concluído para {len(lista_ativo_ids)} ativo(s).",
        "detalhes": detalhes,
    }


def preencher_tipo3(lista_ativo_ids, data_inicial, data_final):
    """
    Preenchimento em lote — Tipo 3 (Volume).

    Assim como Holders, o volume é buscado usando bbg_id_regs e/ou
    bbg_id_144a de cada ativo (o bbg_id "padrão" NÃO é usado), com uma
    chamada BDH por sub-id. Cada linha recebe volume_partial=1 se sua data
    for hoje (pregão ainda não fechado) e 0 caso contrário; como a PK de
    fact_trading_volume é (asset_id, date_id, series_type), reprocessar uma
    data marcada como parcial em execução futura sobrescreve a linha antiga
    com o valor final e volume_partial=0.

    Parâmetros:
        lista_ativo_ids (list[str])
        data_inicial (str): "YYYY-MM-DD"
        data_final (str): "YYYY-MM-DD"

    Retorno esperado (dict): mesmo formato dos outros tipos.
    """
    if not isinstance(data_inicial, datetime) and not isinstance(data_inicial, date):
        data_inicial = datetime.strptime(data_inicial, "%Y-%m-%d").date()
    if not isinstance(data_final, datetime) and not isinstance(data_final, date):
        data_final = datetime.strptime(data_final, "%Y-%m-%d").date()

    hoje = datetime.now().date()
    datas = pd.date_range(start=data_inicial, end=data_final).to_pydatetime().tolist()

    ids_por_ativo = db_obter_ids_holders(lista_ativo_ids)
    detalhes = []

    conn = sqlite3.connect(DB_PATH)
    ids_map = garantir_dim_date_range(conn, datas)
    df_assets = pd.read_sql("SELECT asset_id, bbg_id FROM dim_security", conn)

    colunas_saida = ['asset_id', 'date_id', 'trading_volume_thousands', 'volume_partial', 'series_type']

    try:
        for ativo_id in lista_ativo_ids:
            regs = ids_por_ativo[ativo_id]["bbg_id_regs"]
            a144 = ids_por_ativo[ativo_id]["bbg_id_144a"]
            ids_consulta = {}

            if regs:
                ids_consulta["RegS"] = regs
            if a144:
                ids_consulta["144A"] = a144

            if not ids_consulta:
                detalhes.append({"id": ativo_id, "status": "erro",
                                 "mensagem": "Sem BBG ID RegS/144A cadastrado."})
                continue

            frames_ativo = []
            erro_ativo = None
            sub_ids_sem_dado = []

            for tipo, id_consulta in ids_consulta.items():
                # Formata o ID adicionando @TRAC CORP se não existir
                id_consulta_bbg = id_consulta if id_consulta.upper().endswith("@TRAC CORP") else f"{id_consulta}@TRAC CORP"
                try:
                    print(f"[Volume] {ativo_id} -> consultando {tipo}: {id_consulta}")
                    
                    existentes = obter_datas_existentes(conn, 'fact_trading_volume', ativo_id, data_inicial, data_final, series_type=tipo, ignore_partial=True)
                    faltantes = sorted(set(pd.date_range(start=data_inicial, end=data_final).date) - existentes)
                    
                    if not faltantes:
                        continue # Pula se não falta nada
                        
                    fatias = calcular_fatias_faltantes(existentes, data_inicial, data_final)
                    frames_fatias = []
                    
                    for dt_ini, dt_fim in fatias:
                        df_slice = blp.bdh(
                            tickers=[id_consulta_bbg], flds=['PX_VOLUME'],
                            start_date=dt_ini, end_date=dt_fim, Per='D'
                        )
                        if not df_slice.empty:
                            frames_fatias.append(df_slice)
                            
                    if not frames_fatias:
                        if tipo not in sub_ids_sem_dado: sub_ids_sem_dado.append(tipo)
                        continue

                    # Concatena as fatias brutas
                    df_raw = pd.concat(frames_fatias, ignore_index=True)
                    
                    # Roda o fill zero em todo o intervalo
                    df = bloomberg_fill_zero(df_raw)
                    
                    # Filtra apenas o que é faltante usando a variável já calculada
                    df['date_date'] = pd.to_datetime(df['date']).dt.date
                    df = df[df['date_date'].isin(faltantes)].drop(columns=['date_date'])
                    
                    if 'field' in df.columns and 'value' in df.columns:
                        df = df.pivot(index=['ticker', 'date'], columns='field', values='value').reset_index()

                    if 'PX_VOLUME' not in df.columns:
                        df['PX_VOLUME'] = None
                    df['PX_VOLUME'] = pd.to_numeric(df['PX_VOLUME'], errors='coerce')

                    df.rename(columns={'PX_VOLUME': 'trading_volume_thousands'}, inplace=True)
                    df['series_type'] = tipo
                    df['bbg_id'] = ativo_id
                    print(df)
                    frames_ativo.append(df)
                except Exception as e:
                    # Falha isolada por sub-id (RegS/144A): não derruba o
                    # outro sub-id do mesmo ativo, nem os demais ativos.
                    print(f"Erro ao consultar Volume ({tipo}) para {ativo_id}: {e}")
                    erro_ativo = str(e)

            if not frames_ativo:
                if erro_ativo:
                    detalhes.append({"id": ativo_id, "status": "erro",
                                     "mensagem": f"Falha ao consultar Volume: {erro_ativo}"})
                else:
                    detalhes.append({"id": ativo_id, "status": "erro",
                                     "mensagem": "Nenhum volume retornado pela Bloomberg."})
                continue

            df_ativo = pd.concat(frames_ativo, ignore_index=True)
            df_ativo = df_ativo.merge(df_assets, on='bbg_id', how='inner')
            if df_ativo.empty:
                detalhes.append({"id": ativo_id, "status": "erro",
                                 "mensagem": "Ativo não encontrado em dim_security."})
                continue

            df_ativo['date_id'] = df_ativo['date'].apply(lambda x: ids_map[x.strftime("%Y-%m-%d")])
            df_ativo['volume_partial'] = (df_ativo['date'].dt.date == hoje).astype(int)

            df_ativo[colunas_saida].to_sql('fact_trading_volume_temp', conn, if_exists='replace', index=False)
            cursor = conn.cursor()
            cursor.execute(f'''
                INSERT OR REPLACE INTO fact_trading_volume ({", ".join(colunas_saida)})
                SELECT {", ".join(colunas_saida)} FROM fact_trading_volume_temp
            ''')
            cursor.execute('DROP TABLE fact_trading_volume_temp')
            conn.commit()

            if sub_ids_sem_dado:
                detalhes.append({"id": ativo_id, "status": "ok",
                                 "mensagem": f"Volume gravado. Sem dados para: {', '.join(sub_ids_sem_dado)}."})
            else:
                detalhes.append({"id": ativo_id, "status": "ok",
                                 "mensagem": f"Volume preenchido ({len(ids_consulta)} ID(s) consultado(s))."})
    except Exception as e:
        conn.rollback()
        print(f"Erro geral ao preencher volume: {e}")
        return {
            "sucesso": False,
            "mensagem": f"Erro ao preencher volume: {e}",
            "detalhes": detalhes or [
                {"id": ativo_id, "status": "erro", "mensagem": str(e)}
                for ativo_id in lista_ativo_ids
            ],
        }
    finally:
        conn.close()

    sucesso = all(d["status"] == "ok" for d in detalhes)
    return {
        "sucesso": sucesso,
        "mensagem": f"Volume concluído para {len(lista_ativo_ids)} ativo(s).",
        "detalhes": detalhes,
    }


def preencher_bdp(lista_ativo_ids):
    """
    Preenchimento em lote — Dados BDP (referência estática da Bloomberg).
    Não usa período: BDP traz o dado "atual", não uma série histórica.

    Parâmetros:
        lista_ativo_ids (list[str])

    Retorno esperado (dict): mesmo formato de preencher_tipo1.
    """
    conn = sqlite3.connect(DB_PATH)
    campos_bdp = ['AMT_OUTSTANDING', 'RTG_MOODY', 'RTG_SP_LONG', 'RTG_FITCH', 'MTY_DUR_MID', 'BB_COMPOSITE', 'NXT_CALL_DT', 'YLD_YTC_MID', 'NXT_CALL_PX', 'z_sprd_mid']
    date_id = garantir_dim_date(conn, datetime.now().date())
    collected_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    try:
        df_bdp = blp.bdp(lista_ativo_ids, flds=campos_bdp)

        if df_bdp.empty:
            print("Retorno vazio da Bloomberg. Nada a gravar em fact_bdp.")
            return {
                "sucesso": False,
                "mensagem": "Retorno vazio da Bloomberg. Nada a gravar em fact_bdp.",
                "detalhes": [
                    {"id": ativo_id, "status": "erro", "mensagem": "Sem dados."}
                    for ativo_id in lista_ativo_ids
                ],
            }

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
    except Exception as e:
        print(f"Erro ao preencher dados BDP: {e}")
        return {
            "sucesso": False,
            "mensagem": f"Erro ao preencher dados BDP: {e}",
            "detalhes": [
                {"id": ativo_id, "status": "erro", "mensagem": str(e)}
                for ativo_id in lista_ativo_ids
            ],
        }
    finally:
        conn.close()

    detalhes = [
        {"id": ativo_id, "status": "ok", "mensagem": "Dados BDP atualizados."}
        for ativo_id in lista_ativo_ids
    ]
    return {
        "sucesso": True,
        "mensagem": f"Dados BDP concluído para {len(lista_ativo_ids)} ativo(s).",
        "detalhes": detalhes,
    }


# Tipos que compõem "todos", na ordem em que serão executados.
SUBTIPOS_TODOS = ("tipo1", "tipo2", "tipo3", "bdp")

# Tipos que precisam de período. Os demais ignoram data_inicial/data_final.
# (Manter em sincronia com TIPOS_COM_DATA no app.js)
TIPOS_QUE_USAM_DATA = {"tipo1", "tipo3"}


def _valor_preenchido(valor):
    """True se for string não vazia. Trata None, NaN e '' como vazio."""
    return isinstance(valor, str) and valor.strip() != ""


def db_obter_ids_holders(lista_ativo_ids):
    """
    Lê bbg_id_regs e bbg_id_144a de cada ativo.

    Retorno (dict): {bbg_id: {"bbg_id_regs": str|None, "bbg_id_144a": str|None}}
    Contém TODOS os ids pedidos; ids inexistentes no banco vêm com ambos None.
    """
    resultado = {a: {"bbg_id_regs": None, "bbg_id_144a": None} for a in lista_ativo_ids}
    if not lista_ativo_ids:
        return resultado

    conn = sqlite3.connect(DB_PATH)
    try:
        marcadores = ",".join("?" for _ in lista_ativo_ids)
        df = pd.read_sql(
            f"SELECT bbg_id, bbg_id_regs, bbg_id_144a FROM dim_security WHERE bbg_id IN ({marcadores})",
            conn,
            params=tuple(lista_ativo_ids),
        )
    finally:
        conn.close()

    for _, row in df.iterrows():
        resultado[row["bbg_id"]] = {
            "bbg_id_regs": row["bbg_id_regs"].strip() if _valor_preenchido(row["bbg_id_regs"]) else None,
            "bbg_id_144a": row["bbg_id_144a"].strip() if _valor_preenchido(row["bbg_id_144a"]) else None,
        }
    return resultado


def holders_avaliar_ids(lista_ativo_ids):
    """
    Classifica cada ativo em um cenário e devolve o status global, onde
    prevalece o cenário mais restritivo entre todos os ativos.

    Cenário por ativo:
        1 -> nenhum dos dois IDs preenchido
        2 -> apenas um preenchido
        3 -> os dois preenchidos

    Status global:
        "bloqueado"  -> algum ativo no cenário 1
        "incompleto" -> nenhum no 1, mas algum no 2
        "ok"         -> todos no cenário 3

    Retorno:
        {
            "status": "ok" | "incompleto" | "bloqueado",
            "ativos": [
                {"bbg_id": str, "bbg_id_regs": str|None,
                 "bbg_id_144a": str|None, "cenario": 1|2|3},
                ...
            ]
        }
    """
    ids_por_ativo = db_obter_ids_holders(lista_ativo_ids)

    ativos = []
    for bbg_id in lista_ativo_ids:
        regs = ids_por_ativo[bbg_id]["bbg_id_regs"]
        a144 = ids_por_ativo[bbg_id]["bbg_id_144a"]
        preenchidos = sum(1 for v in (regs, a144) if v)
        ativos.append({
            "bbg_id": bbg_id,
            "bbg_id_regs": regs,
            "bbg_id_144a": a144,
            "cenario": {0: 1, 1: 2, 2: 3}[preenchidos],
        })

    cenarios = {a["cenario"] for a in ativos}
    if 1 in cenarios:
        status = "bloqueado"
    elif 2 in cenarios:
        status = "incompleto"
    else:
        status = "ok"

    return {"status": status, "ativos": ativos}


def db_atualizar_ids_holders(correcoes):
    """
    Preenche IDs de Holders que estavam vazios (usado pelo formulário inline
    da Aba 3). NUNCA sobrescreve um ID já preenchido; edição de valores
    existentes continua sendo feita pela Aba 2.

    Parâmetros:
        correcoes (list[dict]): [{"bbg_id": str,
                                  "bbg_id_regs": str (opcional),
                                  "bbg_id_144a": str (opcional)}, ...]

    Retorno: {"sucesso": bool, "mensagem": str}
    """
    conn = sqlite3.connect(DB_PATH)
    atualizados = 0
    try:
        cursor = conn.cursor()
        for c in correcoes:
            bbg_id = c.get("bbg_id")
            if not bbg_id:
                continue

            regs = str(c.get("bbg_id_regs") or "").strip()
            a144 = str(c.get("bbg_id_144a") or "").strip()

            if regs:
                cursor.execute(
                    "UPDATE dim_security SET bbg_id_regs = ? "
                    "WHERE bbg_id = ? AND (bbg_id_regs IS NULL OR TRIM(bbg_id_regs) = '')",
                    (regs, bbg_id),
                )
                atualizados += cursor.rowcount
            if a144:
                cursor.execute(
                    "UPDATE dim_security SET bbg_id_144a = ? "
                    "WHERE bbg_id = ? AND (bbg_id_144a IS NULL OR TRIM(bbg_id_144a) = '')",
                    (a144, bbg_id),
                )
                atualizados += cursor.rowcount

        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"Erro ao salvar IDs de Holders: {e}")
        return {"sucesso": False, "mensagem": "Erro ao salvar os IDs no banco."}
    finally:
        conn.close()

    return {"sucesso": True, "mensagem": f"{atualizados} ID(s) salvo(s) com sucesso."}

# Mapa usado pela orquestradora para rotear cada "tipo_preenchimento"
# recebido do front para a função placeholder correspondente.
# Para adicionar um novo tipo de preenchimento: crie a função acima e
# registre-a aqui — a rota e o restante do fluxo não precisam mudar.
FUNCOES_PREENCHIMENTO = {
    "tipo1": lambda ids, di, df: preencher_tipo1(ids, di, df),
    "tipo2": lambda ids, di, df: preencher_tipo2(ids),   # Holders ignora datas
    "tipo3": lambda ids, di, df: preencher_tipo3(ids, di, df),
    "bdp":   lambda ids, di, df: preencher_bdp(ids),     # BDP ignora datas
}


def preencher_tabelas_sob_demanda(tipo_preenchimento, lista_ativo_ids, data_inicial, data_final, subtipos=None):
    """
    Orquestradora do preenchimento em lote (Aba 3).

    Roteia para a função placeholder correta com base em tipo_preenchimento:
        "tipo1", "tipo2", "tipo3" -> preenchem uma tabela cada, individualmente
        "todos"                  -> roda tipo1, tipo2 e tipo3 em sequência,
                                     usando os MESMOS ativos e MESMAS datas.
                                     Se um deles falhar, os outros continuam
                                     rodando normalmente (falha isolada).
        "bdp"                    -> preenche a tabela de dados BDP; não usa
                                     data_inicial/data_final.

    Parâmetros:
        tipo_preenchimento (str): "tipo1" | "tipo2" | "tipo3" | "todos" | "bdp"
        lista_ativo_ids (list[str])
        data_inicial (str ou None): "YYYY-MM-DD"
        data_final (str ou None): "YYYY-MM-DD"

    Retorno esperado (dict):
        {
            "sucesso": bool,
            "mensagem": str,
            "detalhes": [{"id": str, "status": "ok"/"erro", "mensagem": str}, ...]
        }
    """
    if tipo_preenchimento == "todos":
        mensagens = []
        detalhes_agregados = []
        sucesso_geral = True

        # "subtipos" vem do seletor da aba Todos (já validado e ordenado na rota)
        tipos_do_lote = subtipos if subtipos is not None else SUBTIPOS_TODOS
        for sub_tipo in tipos_do_lote:
            try:
                resultado = FUNCOES_PREENCHIMENTO[sub_tipo](lista_ativo_ids, data_inicial, data_final)
                mensagens.append(resultado["mensagem"])
                detalhes_agregados.extend(resultado["detalhes"])
                if not resultado["sucesso"]:
                    sucesso_geral = False
            except Exception as e:
                # Falha isolada: registra o erro deste sub_tipo para cada
                # ativo e segue para o próximo tipo, sem interromper o lote.
                print(f"Erro ao processar '{sub_tipo}': {e}")
                sucesso_geral = False
                mensagens.append(f"{sub_tipo}: falhou ({e})")
                detalhes_agregados.extend([
                    {"id": ativo_id, "status": "erro", "mensagem": f"[{sub_tipo}] Falha: {e}"}
                    for ativo_id in lista_ativo_ids
                ])

        return {
            "sucesso": sucesso_geral,
            "mensagem": " | ".join(mensagens),
            "detalhes": detalhes_agregados,
        }

    funcao = FUNCOES_PREENCHIMENTO.get(tipo_preenchimento)
    if funcao is None:
        return {
            "sucesso": False,
            "mensagem": f"Tipo de preenchimento desconhecido: {tipo_preenchimento}",
            "detalhes": [],
        }

    try:
        return funcao(lista_ativo_ids, data_inicial, data_final)
    except Exception as e:
        print(f"Erro ao processar '{tipo_preenchimento}': {e}")
        return {
            "sucesso": False,
            "mensagem": f"Erro ao processar {tipo_preenchimento}: {e}",
            "detalhes": [
                {"id": ativo_id, "status": "erro", "mensagem": str(e)}
                for ativo_id in lista_ativo_ids
            ],
        }


# =====================================================================
# ==========================  ROTAS (VIEW)  ============================
# =====================================================================

@app.route('/')
def index():
    """Página única com as três abas (SPA simples renderizada pelo Flask)."""
    return render_template('index.html')


# --------------------  Aba 1: Cadastrar novo ID  ----------------------

@app.route('/api/consultar-bloomberg', methods=['POST'])
def api_consultar_bloomberg():
    payload = request.get_json(silent=True) or {}
    bloomberg_id = (payload.get('bloomberg_id') or '').strip()

    if not bloomberg_id:
        return jsonify({"sucesso": False, "mensagem": "Informe um ID válido."}), 400

    dados = bloomberg_consultar_ativo(bloomberg_id)
    if dados is None:
        return jsonify({"sucesso": False, "mensagem": "Ativo não encontrado na Bloomberg."}), 404

    return jsonify({"sucesso": True, "dados": dados})


@app.route('/api/cadastrar-ativo', methods=['POST'])
def api_cadastrar_ativo():
    dados = request.get_json(silent=True) or {}
    if not dados.get('bbg_id'):
        return jsonify({"sucesso": False, "mensagem": "Dados inválidos."}), 400

    resultado = db_inserir_ativo(dados)
    return jsonify(resultado)


# ------------------  Aba 2: Editar ativo existente  -------------------

@app.route('/api/ativos', methods=['GET'])
def api_listar_ativos():
    """Usado para popular os dropdowns pesquisáveis (Choices.js) nas Abas 2 e 3."""
    return jsonify({"sucesso": True, "ativos": db_listar_ativos_resumo()})


@app.route('/api/ativo/<ativo_id>', methods=['GET'])
def api_obter_ativo(ativo_id):
    dados = db_obter_ativo(ativo_id)
    schema = db_obter_schema_ativo()
    if dados is None:
        return jsonify({"sucesso": False, "mensagem": "Ativo não encontrado."}), 404
    return jsonify({
        "sucesso": True,
        "dados": dados,
        "schema": schema,
    })


@app.route('/api/atualizar-ativo', methods=['POST'])
def api_atualizar_ativo():
    dados = request.get_json(silent=True) or {}
    if not dados.get('bbg_id'):
        return jsonify({"sucesso": False, "mensagem": "Dados inválidos."}), 400

    resultado = db_atualizar_ativo(dados)
    return jsonify(resultado)


# --------------  Aba 3: Preenchimento em lote sob demanda  -------------

@app.route('/api/preencher-lote', methods=['POST'])
def api_preencher_lote():
    payload = request.get_json(silent=True) or {}
    tipo = payload.get('tipo_preenchimento') or 'tipo1'
    ids = payload.get('ativo_ids') or []
    data_inicial = payload.get('data_inicial')
    data_final = payload.get('data_final')
    holders_confirmado = bool(payload.get('holders_confirmado'))

    if not ids:
        return jsonify({"sucesso": False, "mensagem": "Selecione ao menos um ativo."}), 400

    # Descobre quais tipos serão de fato executados
    if tipo == 'todos':
        pedidos = payload.get('subtipos') or []
        subtipos = [s for s in SUBTIPOS_TODOS if s in pedidos]  # valida e ordena
        if not subtipos:
            return jsonify({"sucesso": False, "mensagem": "Selecione ao menos um tipo de preenchimento."}), 400
        tipos_a_rodar = subtipos
    else:
        subtipos = None
        tipos_a_rodar = [tipo]

    # Datas só são exigidas se algum tipo a rodar realmente as usa
    if any(t in TIPOS_QUE_USAM_DATA for t in tipos_a_rodar) and not (data_inicial and data_final):
        return jsonify({"sucesso": False, "mensagem": "Informe a data inicial e a data final."}), 400

    # Rede de segurança de RegS/144A: mesmo que o front tenha pulado a
    # verificação, o back-end não deixa passar cenário 1, nem cenário 2 sem
    # confirmação. Holders (tipo2) e Volume (tipo3) dependem desses IDs.
    if 'tipo2' in tipos_a_rodar or 'tipo3' in tipos_a_rodar:
        avaliacao = holders_avaliar_ids(ids)
        if avaliacao['status'] == 'bloqueado':
            return jsonify({"sucesso": False,
                            "mensagem": "Há ativo(s) sem nenhum ID RegS/144A."}), 409
        if avaliacao['status'] == 'incompleto' and not holders_confirmado:
            return jsonify({"sucesso": False,
                            "mensagem": "Há ativo(s) com apenas um ID RegS/144A. Confirmação necessária."}), 409

    resultado = preencher_tabelas_sob_demanda(tipo, ids, data_inicial, data_final, subtipos)
    return jsonify(resultado)


@app.route('/api/holders/verificar', methods=['POST'])
def api_holders_verificar():
    payload = request.get_json(silent=True) or {}
    ids = payload.get('ativo_ids') or []
    if not ids:
        return jsonify({"sucesso": False, "mensagem": "Selecione ao menos um ativo."}), 400
    return jsonify({"sucesso": True, **holders_avaliar_ids(ids)})


@app.route('/api/holders/salvar-ids', methods=['POST'])
def api_holders_salvar_ids():
    payload = request.get_json(silent=True) or {}
    correcoes = payload.get('correcoes') or []
    if not correcoes:
        return jsonify({"sucesso": False, "mensagem": "Nada para salvar."}), 400
    return jsonify(db_atualizar_ids_holders(correcoes))


if __name__ == '__main__':
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)

    print("=======================================================")
    print("                  SISTEMA DE CONSULTA                  ")
    print("=======================================================")
    print(" Status: ONLINE e pronto para uso!")
    print(" Acesso: http://127.0.0.1:5000 (caso a aba não abra)")
    print("")
    print(" [INSTRUÇÃO] Para desligar o sistema após o uso,")
    print(" basta FECHAR ESTA JANELA DO TERMINAL.")
    print("=======================================================\n")

    app.run(debug=False)