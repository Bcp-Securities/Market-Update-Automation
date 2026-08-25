import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "market_update.db"

def main():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    print("=== GERENCIADOR DE METADADOS ===")
    print("Dica: Deixe um campo em branco e aperte ENTER para manter o valor atual (na atualização).")
    
    while True:
        codigo = input("\nDigite o código da série (ex: bebgstat) [ou 'sair']: ").strip().lower()
        if codigo == 'sair':
            break
        if not codigo:
            continue
            
        cursor.execute("SELECT ticker, bbg_field, description, source FROM series_meta WHERE series_code = ?", (codigo,))
        existe = cursor.fetchone()
        
        if existe:
            tkr_atual, fld_atual, desc_atual, src_atual = existe
            print(f"\n>> Série '{codigo}' ENCONTRADA.")
            print(f"Atual: Ticker [{tkr_atual}] | Campo [{fld_atual}] | Fonte [{src_atual}]")
            acao = "ATUALIZADA"
        else:
            tkr_atual = fld_atual = desc_atual = ""
            src_atual = "BBG"
            print(f"\n>> Série '{codigo}' NÃO ENCONTRADA. Vamos CRIAR.")
            acao = "INSERIDA"
        
        # Pede os dados. Se for atualização e o usuário der só ENTER, mantém o que já estava.
        ticker = input(f"Ticker na Bloomberg [{tkr_atual}]: ").strip() or tkr_atual
        campo = input(f"Campo na Bloomberg (px_last/yield) [{fld_atual}]: ").strip() or fld_atual
        desc = input(f"Descrição legível [{desc_atual}]: ").strip() or desc_atual
        source = input(f"Fonte (BBG/FRED) [{src_atual}]: ").strip().upper() or src_atual
        
        # O SQLite resolve perfeitamente inserção ou atualização com ON CONFLICT
        query = """
            INSERT INTO series_meta (series_code, description, source, ticker, bbg_field)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(series_code) DO UPDATE SET
                description = excluded.description,
                source = excluded.source,
                ticker = excluded.ticker,
                bbg_field = excluded.bbg_field,
                updated_at = datetime('now')
        """
        try:
            cursor.execute(query, (codigo, desc, source, ticker, campo))
            conn.commit()
            print(f"✅ Série '{codigo}' {acao} com sucesso!")
        except Exception as e:
            print(f"❌ Erro ao salvar: {e}")

    conn.close()

if __name__ == "__main__":
    main()