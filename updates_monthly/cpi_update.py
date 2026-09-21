import logging
import re
import unicodedata
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

LOG_FILE = LOG_DIR / "atualizador_dados_cpi.log"

URL_CPI = "https://www.frbsf.org/wp-content/uploads/cpi-contributors-data.xlsx"
SHEET_NAME = "chart2_headlineCPI_contr_YoY"


# ==========================================
# CONFIGURAÇÃO DE LOG
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)

logger = logging.getLogger("atualizador_cpi")


# ==========================================
# FUNÇÕES DE TEXTO
# ==========================================

def normalizar_texto(text: str) -> str:
    """
    Normaliza nomes de colunas.

    Remove acentos, converte para minúsculas,
    substitui caracteres inválidos por underscore
    e remove underscores nas extremidades.
    """
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9]+", "_", text)

    return text.strip("_")


# ==========================================
# DOWNLOAD
# ==========================================

def baixar_arquivo(url: str) -> bytes:
    """
    Faz o download do arquivo Excel e retorna seu conteúdo em bytes.
    """
    logger.info("Iniciando download do arquivo CPI: %s", url)

    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()

        logger.info(
            "Download concluído com sucesso. Tamanho: %.2f KB",
            len(response.content) / 1024,
        )

        return response.content

    except requests.RequestException:
        logger.exception("Erro ao baixar o arquivo CPI.")
        raise


# ==========================================
# LEITURA DO EXCEL
# ==========================================

def ler_excel(content: bytes, sheet_name: str) -> pd.DataFrame:
    """
    Lê o conteúdo do Excel e retorna um DataFrame.
    """
    logger.info("Lendo planilha: %s", sheet_name)

    try:
        df = pd.read_excel(
            BytesIO(content),
            sheet_name=sheet_name,
        )

    except Exception:
        logger.exception(
            "Erro ao ler a planilha '%s'.",
            sheet_name,
        )
        raise

    if df.empty:
        logger.error("A planilha '%s' retornou um DataFrame vazio.", sheet_name)
        raise ValueError("DataFrame veio vazio.")

    logger.info(
        "Planilha lida com sucesso. Linhas: %d | Colunas: %d",
        len(df),
        len(df.columns),
    )

    return df


# ==========================================
# NORMALIZAÇÃO DAS COLUNAS
# ==========================================

def normalizar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normaliza os nomes das colunas do DataFrame.
    """
    logger.info("Normalizando nomes das colunas.")

    colunas_originais = list(df.columns)

    df.columns = [
        normalizar_texto(coluna)
        for coluna in df.columns
    ]

    logger.info(
        "Colunas normalizadas: %s",
        df.columns.tolist(),
    )

    logger.debug(
        "Mapeamento de colunas: %s",
        dict(zip(colunas_originais, df.columns)),
    )

    return df


# ==========================================
# VALIDAÇÃO
# ==========================================

def validar_dataframe(df: pd.DataFrame) -> None:
    """
    Valida se as colunas obrigatórias existem.
    """
    logger.info("Validando estrutura do DataFrame.")

    if "date" not in df.columns:
        logger.error("Coluna obrigatória 'date' não encontrada.")
        raise ValueError("Coluna 'date' faltando.")

    logger.info("Validação concluída com sucesso.")


# ==========================================
# LIMPEZA DAS COLUNAS
# ==========================================

def limpar_colunas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove colunas desnecessárias e renomeia colunas
    para os nomes utilizados no banco de dados.
    """
    logger.info("Iniciando limpeza das colunas.")

    colunas_removidas = []

    colunas_para_remover = [
        "notes",
        "food_and_energy",
    ]

    for coluna in colunas_para_remover:
        if coluna in df.columns:
            df.drop(columns=[coluna], inplace=True)
            colunas_removidas.append(coluna)

    if colunas_removidas:
        logger.info(
            "Colunas removidas: %s",
            colunas_removidas,
        )
    else:
        logger.info("Nenhuma coluna desnecessária encontrada.")

    for coluna in df.columns:
        if "core_services" in str(coluna):
            coluna_original = str(coluna)
        else:
            continue

        df.rename(
            columns={
                coluna_original: "core_services"
            },
            inplace=True,
        )

        logger.info(
            "Coluna '%s' renomeada para '%s'.",
            coluna_original,
            "core_services"
        )

    return df


# ==========================================
# CONVERSÃO DOS DADOS
# ==========================================

def converter_colunas_numericas(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converte todas as colunas, exceto 'date', para valores numéricos.
    Valores que não puderem ser convertidos tornam-se NaN.
    """
    logger.info("Convertendo colunas numéricas.")

    for coluna in df.columns:
        if coluna == "date":
            continue

        valores_invalidos_antes = df[coluna].isna().sum()

        df[coluna] = pd.to_numeric(
            df[coluna],
            errors="coerce",
        )

        valores_invalidos_depois = df[coluna].isna().sum()

        novos_nulos = (
            valores_invalidos_depois
            - valores_invalidos_antes
        )

        if novos_nulos > 0:
            logger.warning(
                "Coluna '%s': %d valores não puderam ser "
                "convertidos para número.",
                coluna,
                novos_nulos,
            )

    logger.info("Conversão numérica concluída.")

    return df

def preparar_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converte a coluna 'date' para datetime e valida as datas.
    """
    logger.info("Preparando coluna 'date'.")

    df["date"] = pd.to_datetime(
        df["date"],
        errors="coerce",
    )

    datas_invalidas = df["date"].isna().sum()

    if datas_invalidas > 0:
        logger.error(
            "Foram encontradas %d data(s) inválida(s).",
            datas_invalidas,
        )
        raise ValueError(
            f"Foram encontradas {datas_invalidas} data(s) inválida(s)."
        )

    # Normaliza todas as datas para o primeiro dia do mês.
    df["date"] = df["date"].dt.to_period("M").dt.to_timestamp()

    logger.info(
        "Datas válidas: %d | Primeira data: %s | Última data: %s",
        len(df),
        df["date"].min().date(),
        df["date"].max().date(),
    )

    return df

# ==========================================
# PIPELINE DE PREPARAÇÃO
# ==========================================

def preparar_dataframe(content: bytes) -> pd.DataFrame:
    """
    Executa todo o processo de leitura, normalização,
    validação e limpeza do DataFrame.
    """
    logger.info("Iniciando preparação do DataFrame CPI.")

    df = ler_excel(
        content=content,
        sheet_name=SHEET_NAME,
    )

    df = normalizar_colunas(df)

    validar_dataframe(df)

    df = limpar_colunas(df)

    df = converter_colunas_numericas(df)

    df = preparar_data(df)

    logger.info(
        "DataFrame preparado com sucesso. "
        "Linhas: %d | Colunas: %d",
        len(df),
        len(df.columns),
    )

    return df


# ==========================================
# CONEXÃO COM O BANCO
# ==========================================

def conectar_banco(db_path: Path) -> sqlite3.Connection:
    """
    Abre uma conexão com o banco SQLite.
    """
    logger.info("Conectando ao banco: %s", db_path)

    try:
        conn = sqlite3.connect(db_path)

        logger.info("Conexão com o banco estabelecida.")

        return conn

    except sqlite3.Error:
        logger.exception("Erro ao conectar ao banco.")
        raise

def obter_ultima_data_banco(conn: sqlite3.Connection):
    """
    Retorna a data mais recente disponível na tabela CPI.

    Retorna None caso a tabela esteja vazia.
    """
    logger.info("Consultando última data disponível no banco.")

    cursor = conn.execute(
        "SELECT MAX(date) FROM cpi"
    )

    resultado = cursor.fetchone()[0]

    if resultado is None:
        logger.info("Tabela 'cpi' está vazia.")
        return None

    ultima_data = pd.to_datetime(resultado)

    logger.info(
        "Última data disponível no banco: %s",
        ultima_data.date(),
    )

    return ultima_data

def salvar_no_banco(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
) -> None:
    """
    Insere no banco somente as observações novas.
    """
    if df.empty:
        logger.info(
            "Nenhum dado novo para inserir — base já está atualizada."
        )
        return

    logger.info(
        "Inserindo %d nova(s) observação(ões) no banco.",
        len(df),
    )

    def valor_sql(valor):
        if pd.isna(valor):
            return None
        return valor


    registros = [
        (
            row["date"].strftime("%Y-%m-%d"),
            valor_sql(row["total_headline_cpi_yoy"]),
            valor_sql(row["food"]),
            valor_sql(row["energy"]),
            valor_sql(row["core_goods"]),
            valor_sql(row["core_services"]),
            valor_sql(row["shelter"]),
        )
        for _, row in df.iterrows()
    ]


    sql = """
        INSERT INTO cpi (
            date,
            total_headline_cpi_yoy,
            food,
            energy,
            core_goods,
            core_services,
            shelter
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """

    try:
        conn.executemany(sql, registros)
        conn.commit()

        logger.info(
            "Commit realizado com sucesso. "
            "%d nova(s) observação(ões) inserida(s).",
            len(registros),
        )

    except sqlite3.Error:
        conn.rollback()

        logger.exception(
            "Erro ao inserir dados no banco. "
            "Rollback realizado."
        )

        raise

def atualizar_banco(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
) -> None:
    """
    Compara a última data da web com a última data do banco
    e insere somente as observações novas.
    """
    logger.info("Iniciando verificação de atualização do banco.")

    data_web = df["date"].max()

    logger.info(
        "Última data disponível na web: %s",
        data_web.date(),
    )

    data_banco = obter_ultima_data_banco(conn)

    # Banco vazio
    if data_banco is None:
        logger.info(
            "Banco vazio. Todas as %d observações serão inseridas.",
            len(df),
        )

        salvar_no_banco(conn, df)
        return

    # Banco já atualizado
    if data_web <= data_banco:
        logger.info(
            "Banco já está atualizado. "
            "Web: %s | Banco: %s",
            data_web.date(),
            data_banco.date(),
        )
        return

    # Existem dados novos
    df_novos = df[df["date"] > data_banco].copy()

    logger.info(
        "Foram encontradas %d nova(s) observação(ões).",
        len(df_novos),
    )

    logger.info(
        "Período novo: %s até %s",
        df_novos["date"].min().date(),
        df_novos["date"].max().date(),
    )

    salvar_no_banco(conn, df_novos)

# ==========================================
# MAIN
# ==========================================

def main() -> None:
    """
    Executa o processo completo de atualização do CPI.
    """
    logger.info("=" * 60)
    logger.info("INÍCIO DA ATUALIZAÇÃO DO CPI")
    logger.info("=" * 60)

    conn = None

    try:
        # Download
        content = baixar_arquivo(URL_CPI)

        # Preparação dos dados
        df = preparar_dataframe(content)

        # Banco
        conn = conectar_banco(DB_PATH)

        # Atualização incremental
        atualizar_banco(conn, df)

        logger.info("Atualização do CPI concluída com sucesso.")

    except Exception:
        logger.exception(
            "Falha durante a atualização dos dados CPI."
        )
        raise

    finally:
        if conn is not None:
            conn.close()
            logger.info("Conexão com o banco encerrada.")

        logger.info("=" * 60)
        logger.info("FIM DA ATUALIZAÇÃO DO CPI")
        logger.info("=" * 60)


if __name__ == "__main__":
    main()
