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
        df_bdh = blp.bdh(
            tickers=lista_ativo_ids, flds=['PX_MID', 'YLD_YTM_MID'],
            start_date=data_inicial, end_date=data_final, Per='D', Fill='P'
        )
        df_bdh = bloomberg_fill_prev(df_bdh)

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


def preencher_tipo2(lista_ativo_ids, data_inicial, data_final):
    """
    Preenchimento em lote — Tipo 2 (Holders).

    Parâmetros e retorno: mesmo formato de preencher_tipo1.
    """
    # --- PLACEHOLDER ---
    print(f"[tipo2] ativos={lista_ativo_ids} inicio={data_inicial} fim={data_final}")
    detalhes = [
        {"id": ativo_id, "status": "ok", "mensagem": "Holders preenchidos."}
        for ativo_id in lista_ativo_ids
    ]
    return {
        "sucesso": True,
        "mensagem": f"Holders concluído para {len(lista_ativo_ids)} ativo(s).",
        "detalhes": detalhes,
    }


def preencher_tipo3(lista_ativo_ids, data_inicial, data_final):
    """
    Preenchimento em lote — Tipo 3 (Volume).

    Parâmetros e retorno: mesmo formato de preencher_tipo1.
    """
    # --- PLACEHOLDER ---
    print(f"[tipo3] ativos={lista_ativo_ids} inicio={data_inicial} fim={data_final}")
    detalhes = [
        {"id": ativo_id, "status": "ok", "mensagem": "Volume preenchido."}
        for ativo_id in lista_ativo_ids
    ]
    return {
        "sucesso": True,
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


# Mapa usado pela orquestradora para rotear cada "tipo_preenchimento"
# recebido do front para a função placeholder correspondente.
# Para adicionar um novo tipo de preenchimento: crie a função acima e
# registre-a aqui — a rota e o restante do fluxo não precisam mudar.
FUNCOES_PREENCHIMENTO = {
    "tipo1": lambda ids, di, df: preencher_tipo1(ids, di, df),
    "tipo2": lambda ids, di, df: preencher_tipo2(ids, di, df),
    "tipo3": lambda ids, di, df: preencher_tipo3(ids, di, df),
    "bdp": lambda ids, di, df: preencher_bdp(ids),  # BDP ignora datas
}


def preencher_tabelas_sob_demanda(tipo_preenchimento, lista_ativo_ids, data_inicial, data_final):
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

        for sub_tipo in ("tipo1", "tipo2", "tipo3"):
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

    if not ids:
        return jsonify({"sucesso": False, "mensagem": "Selecione ao menos um ativo."}), 400

    resultado = preencher_tabelas_sob_demanda(tipo, ids, data_inicial, data_final)
    return jsonify(resultado)


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