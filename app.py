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
# Todas as funções abaixo são PLACEHOLDERS. Substitua o conteúdo delas
# pela lógica real de integração com a Bloomberg (blpapi / xbbg / etc)
# e com o banco de dados (SQLite, Postgres, etc).
#
# O contrato (parâmetros de entrada e formato de retorno) de cada função
# já está pronto para o front-end funcionar. Mantenha os mesmos formatos
# de retorno ao implementar a lógica real, ou ajuste o front-end junto.
# =====================================================================

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
            INSERT OR IGNORE INTO dim_security (bbg_id, ticker, coupon, maturity, issue_date, industry_group, issuer, isin, currency, collateral, amt_issuance, min_piece, cntry_of_risk)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (dados['bbg_id'], dados['ticker'], dados['coupon'], dados['maturity'], dados['issue_date'], dados['industry_group'], dados['issuer'], dados['isin'], dados['currency'], dados['collateral'], dados['amt_issuance'], dados['min_piece'], dados['cntry_of_risk']))

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
        df = pd.read_sql("SELECT bbg_id, ticker FROM dim_security", conn)
    except Exception as e:
        print(f"Erro ao listar ativos do banco: {e}")
        return []
    finally:
        conn.close()

    resumo = []
    for _, row in df.iterrows():
        bbg_id = row['bbg_id']
        ticker = row['ticker'] or ''
        label = f"{bbg_id} - {ticker}" if ticker else bbg_id
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


def preencher_tabelas_sob_demanda(lista_ativo_ids):
    """
    Dispara o preenchimento sob demanda de DUAS tabelas do banco para os
    ativos selecionados (Aba 3). Tipicamente vai buscar dados adicionais
    na Bloomberg para cada ativo e gravar em duas tabelas relacionadas.

    Parâmetros:
        lista_ativo_ids (list[str]): IDs internos dos ativos selecionados.

    Retorno esperado (dict):
        {
            "sucesso": bool,
            "mensagem": str,
            "detalhes": [
                {"id": str, "status": "ok"/"erro", "mensagem": str}, ...
            ]
        }
    """
    # --- PLACEHOLDER ---
    detalhes = [
        {"id": ativo_id, "status": "ok", "mensagem": "Tabelas A e B preenchidas."}
        for ativo_id in lista_ativo_ids
    ]
    return {
        "sucesso": True,
        "mensagem": f"Processamento concluído para {len(lista_ativo_ids)} ativo(s).",
        "detalhes": detalhes,
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
    ids = payload.get('ativo_ids') or []

    if not ids:
        return jsonify({"sucesso": False, "mensagem": "Selecione ao menos um ativo."}), 400

    resultado = preencher_tabelas_sob_demanda(ids)
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