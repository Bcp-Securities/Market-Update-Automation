# Documentação Market Update

Sistema de atualização automática de um banco SQLite (`market_update.db`) com séries de mercado (Bloomberg, FRED) e séries macro de frequência mensal (CPI/FRBSF, IPCA/BCB).

## Visão geral

O projeto roda dois ETLs independentes, agendados no Windows Task Scheduler, cada um lendo suas próprias tabelas de metadados para saber **o que** buscar e **onde** buscar:

| ETL | Script | Frequência | Fontes | Tabelas |
|---|---|---|---|---|
| Diário | `market_update.py` | de hora em hora (dias úteis) | Bloomberg (via `xbbg`), FRED | `series_meta` → `time_series` |
| Mensal | `monthly_update.py` | 1x por dia 10:00 (dias úteis) | FRBSF (Excel), BCB/SGS (JSON) | `series_meta_monthly` → `time_series_monthly` |

A ideia central do projeto é: **o código não sabe nada sobre séries específicas — ele só sabe ler metadados e executar a fonte correspondente**. Adicionar uma série nova (Bloomberg, FRED, FRBSF ou BCB) é inserir uma linha na tabela de metadados certa; não é preciso alterar os scripts de ETL.

## Estrutura do banco

### `series_meta` (diário) — o coração da atualização diária

Cada linha descreve **uma série** e diz ao `market_update.py` como buscá-la:

| Coluna | Significado |
|---|---|
| `series_code` | Chave interna da série (PK), usada em `time_series.series_code` |
| `description` | Descrição livre |
| `source` | `'BBG'` ou `'FRED'` — decide qual bloco do ETL processa a série |
| `ticker` | Ticker Bloomberg (source=BBG) ou ID da série no FRED (source=FRED) |
| `bbg_field` | Campo Bloomberg a puxar (`px_last`, `yield`, ...). Nulo para FRED |
| `updated_at` | Timestamp da última atualização do metadado |

O `bbg_field` é mapeado para o campo real da Bloomberg (`px_last`, `YLD_YTM_MID`, etc.) pelo dicionário `MAPEAMENTO_BBG` dentro do próprio `market_update.py`. Se aparecer um `bbg_field` novo que não exista nesse dicionário, o ETL **pula** as séries daquele campo e loga erro — ele não tenta adivinhar.

### `series_meta_monthly` (mensal) — o mesmo papel, para dados mensais

| Coluna | Significado |
|---|---|
| `series_code` | Chave interna da série (PK) |
| `description` | Descrição livre |
| `source` | `'FRBSF'` ou `'BCB'` — decide qual fetcher processa a série |
| `ticker` | Código da série (Ex: usado quando source=BCB) |
| `value_field` | Nome da coluna de valor na planilha da fonte (Ex: usado quando source=FRBSF) |
| `updated_at` | Timestamp da última atualização do metadado |

### `time_series` e `time_series_monthly` — os valores

Ambas seguem o mesmo formato "longo" (uma linha por série + data):

```
series_code | obs_date | value
```

Chave primária composta `(series_code, obs_date)`. `time_series` tem granularidade diária; `time_series_monthly` tem granularidade mensal (sempre dia 1 do mês, `YYYY-MM-01`).

## Como cada ETL decide o que buscar

### Diário — `market_update.py`

1. Testa se o terminal Bloomberg está logado (`blp.bdp`). Se não estiver, **aborta a execução inteira** (não tenta nem BBG nem FRED) — é um "portão" de segurança para não gravar dados incompletos.
2. Lê `series_meta` filtrando `source='BBG'`, agrupa por `bbg_field`, resolve o campo Bloomberg real via `MAPEAMENTO_BBG`, descobre a data inicial de cada série (dia seguinte ao último `obs_date` salvo) e baixa via `blp.bdh`.
3. Lê `series_meta` filtrando `source='FRED'`, baixa o CSV público de cada série a partir da data inicial, preenchendo gaps de fim de semana/feriado com o último valor conhecido (replicando o comportamento `fill=PREV` da Bloomberg).
4. Grava tudo com `INSERT OR IGNORE` em `time_series` — nunca sobrescreve um valor já existente, só adiciona o que é novo.
5. Sempre busca a partir do dia seguinte ao último dado salvo; se já está atualizado até D-1, pula a série sem tocar na rede.

### Mensal — `monthly_update.py`

1. Lê `series_meta_monthly` inteira.
2. Para cada linha, despacha para o fetcher correspondente ao `source` (`fetch_frbsf_cpi` ou `fetch_bcb_sgs`).
3. Cada fetcher devolve um DataFrame padronizado (`series_code`, `obs_date`, `value`).
4. Grava com `INSERT ... ON CONFLICT DO UPDATE` em `time_series_monthly` — diferente do diário, aqui o **último ponto é sempre reenviado e sobrescrito**, porque CPI e IPCA sofrem revisão retroativa com frequência.
5. Falha em uma série não interrompe as demais (loga e continua).

## Adicionando uma série nova

- **Bloomberg/FRED**: inserir uma linha em `series_meta` com `source`, `ticker` e (se BBG) `bbg_field`. Se o `bbg_field` for novo, adicionar o mapeamento correspondente em `MAPEAMENTO_BBG` no `market_update.py`.
- **FRBSF/BCB**: inserir uma linha em `series_meta_monthly` (via `seed_series_meta_monthly.py`) com `source`, e `ticker` (BCB) ou `value_field` (FRBSF).
- **Fonte totalmente nova** (ex: outro provedor de dados mensais): escrever um novo fetcher em `monthly_update.py` com a assinatura `(meta_row: dict) -> DataFrame[series_code, obs_date, value]` e registrá-lo no dicionário `FETCHERS`.

## Automação (Windows Task Scheduler)

Dois `.bat` independentes, cada um com seu próprio lockfile (evita execução concorrente) e log próprio em `logs/`:

| Tarefa | Script | Trigger | Lockfile | Log |
|---|---|---|---|---|
| Diário | `run_daily.bat` | de hora em hora | `run_market_update.lock` | `logs/run_bat.log` |
| Mensal | `run_monthly.bat` | 1x por dia | `run_monthly_update.lock` | `logs/run_bat_monthly.log` |

As duas tarefas são independentes entre si — uma falhar não afeta a outra. Cada script Python também mantém seu próprio log detalhado em `logs/` (`atualizador_dados.log`, `atualizador_dados_mensais.log`).

## Observações importantes

- Todos os ETLs são **idempotentes**: rodar de novo não duplica dados. O diário ignora datas já existentes; o mensal sobrescreve o último ponto (por causa de revisões) mas não duplica linhas.
- O schema inteiro (todas as tabelas) é criado via `CREATE TABLE IF NOT EXISTS`, então rodar o script de criação de novo nunca apaga dados existentes.
- Os metadados (`series_meta` e `series_meta_monthly`) são a única coisa que precisa ser editada para adicionar/remover séries no dia a dia — os scripts de ETL raramente precisam mudar.