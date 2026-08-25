import sqlite3
import datetime
import pandas as pd
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "market_update.db"


def calcular_d_menos_1():
    """Mesma referência usada pelo ETL principal."""
    return datetime.date.today() - datetime.timedelta(days=1)


def main():
    print("=== ESTATÍSTICAS DO BANCO DE DADOS ===")
    try:
        conn = sqlite3.connect(DB_PATH)
    except Exception as e:
        print(f"Erro ao conectar no banco: {e}")
        return

    data_referencia = calcular_d_menos_1()
    print(f"Data de referência (D-1): {data_referencia}")

    # ==========================================
    # 1. Resumo geral (visão rápida no topo)
    # ==========================================
    total_series = pd.read_sql_query("SELECT COUNT(*) as n FROM series_meta", conn)["n"].iloc[0]
    total_pontos_geral = pd.read_sql_query("SELECT COUNT(*) as n FROM time_series", conn)["n"].iloc[0]

    print("\n[0] Resumo Geral:")
    print(f"  Séries cadastradas : {total_series}")
    print(f"  Pontos no banco     : {total_pontos_geral}")

    # ==========================================
    # 2. Séries Cadastradas por Fonte
    # ==========================================
    meta = pd.read_sql_query(
        "SELECT source, COUNT(*) as qtd_series FROM series_meta GROUP BY source", conn
    )
    print("\n[1] Séries Cadastradas por Fonte:")
    if not meta.empty:
        print(meta.to_string(index=False))
    else:
        print("  Nenhuma série cadastrada.")

    # ==========================================
    # 3. Séries cadastradas SEM NENHUM dado
    #    (join necessário: series_meta pode ter linha sem correspondência
    #     nenhuma em time_series, o que a query antiga nunca detectava)
    # ==========================================
    query_sem_dado = """
        SELECT sm.series_code, sm.description, sm.source, sm.ticker
        FROM series_meta sm
        LEFT JOIN time_series ts ON ts.series_code = sm.series_code
        WHERE ts.series_code IS NULL
        ORDER BY sm.source, sm.series_code
    """
    sem_dado = pd.read_sql_query(query_sem_dado, conn)
    print(f"\n[2] Séries Cadastradas SEM Nenhum Dado Coletado ({len(sem_dado)}):")
    if not sem_dado.empty:
        print(sem_dado.to_string(index=False))
    else:
        print("  Nenhuma — todas as séries cadastradas têm ao menos 1 ponto.")

    # ==========================================
    # 4. Detalhes das Séries Temporais
    #    (agora com source/ticker via join, e sinalizando desatualizadas)
    # ==========================================
    query_stats = """
        SELECT
            sm.series_code,
            sm.source,
            sm.ticker,
            COUNT(ts.obs_date)  as total_pontos,
            MIN(ts.obs_date)    as primeira_data,
            MAX(ts.obs_date)    as ultima_data
        FROM time_series ts
        JOIN series_meta sm ON sm.series_code = ts.series_code
        GROUP BY sm.series_code, sm.source, sm.ticker
        ORDER BY ultima_data DESC, sm.series_code
    """
    stats = pd.read_sql_query(query_stats, conn)

    if not stats.empty:
        stats["dias_desatualizada"] = stats["ultima_data"].apply(
            lambda d: (data_referencia - datetime.datetime.strptime(d, "%Y-%m-%d").date()).days
        )
        stats["status"] = stats["dias_desatualizada"].apply(
            lambda n: "OK" if n <= 0 else f"ATRASADA ({n}d)"
        )

        print(f"\n[3] Detalhes das Séries Temporais (Total de registros no banco: {stats['total_pontos'].sum()}):")
        pd.set_option('display.max_rows', None)
        pd.set_option('display.width', 1000)
        colunas_exibir = ["series_code", "source", "ticker", "total_pontos",
                          "primeira_data", "ultima_data", "status"]
        print(stats[colunas_exibir].to_string(index=False))

        # ==========================================
        # 5. Destaque: séries desatualizadas em relação a D-1
        # ==========================================
        atrasadas = stats[stats["dias_desatualizada"] > 0].sort_values(
            "dias_desatualizada", ascending=False
        )
        print(f"\n[4] Séries Desatualizadas em Relação a D-1 ({len(atrasadas)}):")
        if not atrasadas.empty:
            print(atrasadas[["series_code", "source", "ultima_data", "dias_desatualizada"]].to_string(index=False))
        else:
            print("  Nenhuma — todas as séries estão em dia até D-1.")

    else:
        print("\n[3] Nenhuma observação encontrada em time_series.")

    # ==========================================
    # 6. Cobertura por fonte (pontos totais + intervalo de datas por source)
    # ==========================================
    query_cobertura = """
        SELECT
            sm.source,
            COUNT(ts.obs_date) as total_pontos,
            MIN(ts.obs_date)   as data_mais_antiga,
            MAX(ts.obs_date)   as data_mais_recente
        FROM time_series ts
        JOIN series_meta sm ON sm.series_code = ts.series_code
        GROUP BY sm.source
    """
    cobertura = pd.read_sql_query(query_cobertura, conn)
    print("\n[5] Cobertura por Fonte:")
    if not cobertura.empty:
        print(cobertura.to_string(index=False))
    else:
        print("  Sem dados.")

    conn.close()


if __name__ == "__main__":
    main()