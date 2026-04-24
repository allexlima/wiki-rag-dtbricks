# 💰 Relatório de Estimativa de Custos — RAG Agent (Databricks)

**Projeto:** Wiki RAG Agent · **Moeda:** USD · **Período base:** Mensal (30 dias) · **Data:** 23/04/2026
**Tier de referência:** Azure Databricks Premium · **Região:** `eastus` · **DBU rates:** list price sem descontos

> 📌 Esta estimativa usa **preços de lista públicos** (sem descontos contratuais) e **tokens medidos diretamente no código do agente** (`src/rag.py` + `src/prompts.py` + `src/config.py`). Para números precisos por SKU aplicáveis ao seu ambiente, use o **Lakemeter** (workspace `fe-vm-lakemeter`) ou **Quicksizer** — ambos disponíveis em [go/sizing](https://go/sizing).

---

## 1. Contexto

Este relatório apresenta três cenários de custo para a operação do agente RAG construído sobre Databricks (Lakebase + Foundation Model API + Model Serving + Serverless Jobs). Os cenários são **dimensionados pelo volume diário de queries** — o cliente escolhe o tier mais próximo do seu uso esperado.

O **caso mediano (Medium)** é calibrado em **510 queries/dia**, valor considerado representativo para uso em produção padrão. O cenário **High** respeita o limite operacional de **8 horas de runtime por dia** (pico).

### 1.1 Premissas de Cálculo (auditadas no código)

| Premissa | Valor | Origem / Confiança |
|---|---|---|
| Tokens input médios por query | **~4.860** | Auditoria do agente (`src/rag.py`) — **alta** |
| Tokens output médios por query | **~460** | Auditoria do agente — **alta** |
| Custo médio por query no Claude Sonnet 4.6 | **~\$0,022** | Derivado dos tokens × list price — **alta** |
| LLM calls por query do usuário | 3 (grade + generate + rewrite opcional) | Código do agente — **alta** |
| Top-K de retrieval | 8 chunks | `src/config.py` — **alta** |
| Chunk size | 512 chars (~128 tokens) | `src/ingestion.py` (RecursiveCharacterTextSplitter) — **alta** |
| Distribuição de paths | 70% happy · 25% 1 rewrite · 5% 2 rewrites | Inferência da lógica de grading — **média** |
| Tokens de embedding por query | ~50 | Query embedding apenas — **alta** |
| Vetorização inicial da wiki | ~1.000 tokens/página | Chunking padrão — **média** |
| LLM Judge (Gemini 2.5 Flash) | ~10% das queries avaliadas | Padrão MLflow GenAI Eval — **alta** |
| Runtime operacional máximo | 8h/dia (cenário High) | Definido pelo cliente — **alta** |
| Scale-to-zero | Ativo em todos os serviços | Configuração default — **alta** |
| Mês-base | 30 dias | Convenção — **alta** |

### 1.2 Tabela de Preços de Referência (Azure Databricks Premium · eastus)

| Serviço / Modelo | Rate | Fonte | Confiança |
|---|---|---|---|
| 💬 **Claude Sonnet 4.6** (FMAPI) | \$3,00 / 1M input · \$15,00 / 1M output | [Anthropic pricing](https://www.anthropic.com/pricing) · passthrough FMAPI (idêntico entre clouds) | **🟢 Alta** |
| 🧩 **Qwen3 Embedding 0.6B** (FMAPI) | ~\$0,13 / 1M tokens | Estimado (GTE Large EN como referência) — passthrough | 🟡 Média |
| ⚖️ **Gemini 2.5 Flash** (External Model ou direto Google) | ~\$0,15 / 1M input · ~\$0,60 / 1M output | [Google AI pricing](https://ai.google.dev/gemini-api/docs/pricing) | **🟢 Alta** |
| 🗄️ **Lakebase Compute** (Autoscale) | \$0,44 / DBU · ~0,21 DBU/CU-hora | [azure.microsoft.com/pricing/details/databricks](https://azure.microsoft.com/en-us/pricing/details/databricks/) | 🟡 Média |
| 🗄️ **Lakebase Storage** | \$0,115 / GB-mês | Public pricing | 🟡 Média |
| 🤖 **Model Serving** (Agent CPU, serverless) | \$0,082 / DBU · ~1 DBU/hora (small scale-out) | Azure Databricks Premium pricing | 🟡 Média |
| ⚙️ **Serverless Jobs** (Python) | \$0,40 / DBU · ~2–4 DBU/hora | Azure Databricks Premium pricing | 🟡 Média |

> **Nota Azure vs AWS:** DBU rates no Azure Premium são ~10–15% superiores ao AWS Premium (Model Serving \$0,082 vs \$0,07 · Jobs Serverless \$0,40 vs \$0,35 · Lakebase \$0,44 vs \$0,40). Custos de FMAPI (Claude, Gemini, Qwen) são **idênticos** nos dois clouds (passthrough ao provider). Como >92% do custo está no LLM, o impacto agregado do switch AWS→Azure é **~+1–2%** no total.

---

## 2. Cenários

### 🌱 Low — Uso Leve
**Perfil:** deploys pequenos, ambientes de validação ou uso esporádico.

- 💬 **50 queries/dia** (~1.500/mês)
- 📄 **~200 páginas** na base de conhecimento (~0,05 GB)
- ⏱️ Lakebase 0.5 CU · 3h/dia · Endpoint ~3h ativo/dia
- ⚙️ Jobs de ingestão: **1x/semana** (~5 min)

### ⚡ Medium — Uso Padrão *(caso mediano)*
**Perfil:** operação em produção com adoção consolidada.

- 💬 **510 queries/dia** (~15.300/mês) ← *anchor do relatório*
- 📄 **~1.000 páginas** na base de conhecimento (~0,25 GB)
- ⏱️ Lakebase 0.5 CU · 6h/dia · Endpoint ~6h ativo/dia
- ⚙️ Jobs de ingestão: **3x/semana** (~15 min)

### 🔥 High — Uso Intenso
**Perfil:** adoção ampla, wiki grande, ingestão diária.

- 💬 **2.500 queries/dia** (~75.000/mês)
- 📄 **~5.000 páginas** na base de conhecimento (~1,25 GB)
- ⏱️ Lakebase 1.0 CU · **8h/dia** (cap operacional) · Endpoint ~8h ativo/dia
- ⚙️ Jobs de ingestão: **1x/dia** (~30 min)

---

## 3. Detalhamento por Serviço — **Código Atual (As-Is)**

| Serviço Databricks | Finalidade | 🌱 Low /mês<br>50 q/dia | ⚡ Medium /mês<br>510 q/dia | 🔥 High /mês<br>2.500 q/dia |
|---|---|---:|---:|---:|
| **FMAPI — Claude Sonnet 4.6** | Geração, grading, rewrite de queries | **\$33,00**<br><sub>token passthrough</sub> | **\$336,60**<br><sub>token passthrough</sub> | **\$1.650,00**<br><sub>token passthrough</sub> |
| **FMAPI — Gemini 2.5 Flash** | LLM Judge (MLflow GenAI Evaluation) | **\$0,35** | **\$3,00** | **\$9,00** |
| **FMAPI — Qwen3 Embedding** | Vetorização de chunks + queries | **\$0,07** | **\$0,70** | **\$3,50** |
| **Lakebase Compute** | PostgreSQL gerenciado (wiki, pgvector, histórico) | **\$4,18**<br><sub>10 DBU · 0,5 CU × 3h</sub> | **\$8,36**<br><sub>19 DBU · 0,5 CU × 6h</sub> | **\$22,00**<br><sub>50 DBU · 1,0 CU × 8h</sub> |
| **Lakebase Storage** | Armazenamento da base (≤1,25 GB) | **\$0,01**<br><sub>~50 MB</sub> | **\$0,03**<br><sub>~250 MB</sub> | **\$0,14**<br><sub>~1,25 GB</sub> |
| **Model Serving (Agent CPU)** | Endpoint serverless do ResponsesAgent + LangGraph | **\$7,38**<br><sub>90 DBU</sub> | **\$14,76**<br><sub>180 DBU</sub> | **\$39,36**<br><sub>480 DBU</sub> |
| **Serverless Jobs** | Ingestão: leitura, chunking, embedding, captioning | **\$0,80**<br><sub>2 DBU · 1x/semana</sub> | **\$5,20**<br><sub>13 DBU · 3x/semana</sub> | **\$18,00**<br><sub>45 DBU · 1x/dia</sub> |
| **🔹 TOTAL MENSAL** | | **\$45,89** | **\$368,65** | **\$1.742,00** |
| **🔹 TOTAL ANUAL** | | **~\$551** | **~\$4.424** | **~\$20.904** |

---

## 4. Auditoria do Código e Redução vs. Estimativa Inicial

A estimativa original (antes da auditoria) usava premissas pessimistas de **7.500 tokens input + 1.300 tokens output por query**. A inspeção direta do código revelou comportamento real mais eficiente:

### 4.1 Breakdown das chamadas LLM (medido no código)

| Chamada | Modelo | Input | Output | Max Output | Caching |
|---|---|---:|---:|---:|:---:|
| **Grade Documents** | Sonnet 4.6 | 2.536 | 24 | 24 | ❌ |
| **Rewrite Query** (condicional, ~25–30%) | Sonnet 4.6 | 157 | 50–80 | 128 | ❌ |
| **Generate Answer** | Sonnet 4.6 | 1.346 | 300–500 | 1.024 | ❌ |

### 4.2 Paths por query (distribuição inferida)

| Path | % | Tokens totais |
|---|---:|---:|
| Happy path (1 grade + 1 generate) | 70% | ~4.463 |
| 1 rewrite (grade + rewrite + grade + generate) | 25% | ~7.093 |
| 2 rewrites (worst case) | 5% | ~8.084 |
| **Média ponderada** | | **~5.100** |

### 4.3 Impacto da correção

| Métrica | Estimativa pessimista | Auditoria real | Variação |
|---|---:|---:|---:|
| Tokens/query | 8.800 | ~5.320 | **-40%** |
| Custo/query no Claude | \$0,042 | \$0,022 | **-48%** |
| **Medium /mês (total)** | \$676,65 | **\$368,65** | **-46%** |

---

## 5. Cenário Otimizado — Recomendações Não Aplicadas

O código tem **quatro oportunidades de otimização** não implementadas. Impacto estimado acumulado:

| # | Otimização | Como aplicar | Impacto no custo/query |
|---|---|---|---:|
| 1 | **Prompt caching** (Anthropic) | Adicionar `cache_control` nos system prompts | -5% |
| 2 | **Haiku 4 para grading** | `model="databricks-claude-haiku-4"` no nó de grading | -15% a -20% |
| 3 | **Haiku 4 para rewrite** | Mesma mudança no nó de rewrite | -3% (conditional) |
| 4 | **Reduzir top_k de 8→6** | `TOP_K=6` em `config.py` | -6% |
| **Combinado** | | | **~-35%** |

### 5.1 Comparação: Código Atual vs Otimizado

| Cenário | As-Is /mês | Otimizado /mês | Economia anual |
|---|---:|---:|---:|
| 🌱 Low | \$45,89 | ~\$30 | ~\$191 |
| ⚡ Medium | \$368,65 | **~\$240** | **~\$1.544** |
| 🔥 High | \$1.742,00 | ~\$1.132 | ~\$7.320 |

> **Esforço estimado**: 1–2 horas de desenvolvimento + testes. ROI extremamente alto no cenário Medium/High.

---

## 6. Fórmulas de Custo (recalcule para seu cenário)

```
custo_claude_mensal = queries_dia × 30 × custo_por_query
                      custo_por_query ≈ $0,022 (medido no código atual)
                                       ≈ $0,014 (com otimizações 1-4 acima)

custo_gemini_judge  = queries_dia × 30 × 0,10 × custo_judge_query
                      custo_judge_query ≈ $0,002 (avg)

custo_embedding     = queries_dia × 30 × 50_tokens × $0,13/1M
                    + páginas × 1.000_tokens × $0,13/1M       # vetorização inicial

custo_lakebase      = CU_size × horas_dia × 30 × 0,21 DBU/CU-h × $0,44/DBU   # Azure
                    + GB_armazenados × $0,115

custo_serving       = horas_ativas_dia × 30 × DBUs_por_hora × $0,082           # Azure
                      DBUs/h: 1 (small) a 2 (medium concorrência)

custo_jobs          = freq_semana × 4,3 × duração_min / 60 × DBUs_por_hora × $0,40   # Azure
                      DBUs/h: 3-4 em serverless Python
```

---

## 7. Análise de Sensibilidade (±20% nos drivers-chave · Medium)

| Driver | Impacto no TOTAL | Observação |
|---|---:|---|
| Tokens/query (input) | **±\$54** (±15%) | Maior alavanca. Otimize prompt/context. |
| Tokens/query (output) | **±\$35** (±10%) | Resposta curta = economia direta. |
| queries/dia | **±\$73** (±20%) | Linear no LLM; sub-linear na infra. |
| Runtime horas/dia | **±\$4** (±1%) | Quase neutro. |
| DBU rate Lakebase | **±\$1,5** (<1%) | Irrelevante. |
| Taxa de rewrite (25%→40%) | **±\$50** (±14%) | Afeta se retrieval é ruim. |

> 92% do custo está no **LLM de geração**. Prompt caching, prompt slimming, e sampling de Judge têm ROI muito maior que qualquer tuning de infra.

---

## 8. Análise Complementar

### 8.1 Distribuição do Custo (Medium — As-Is · Azure)

| Serviço | Valor | % do total |
|---|---:|---:|
| Claude Sonnet 4.6 | \$336,60 | **91,3%** |
| Model Serving | \$14,76 | 4,0% |
| Lakebase (Compute + Storage) | \$8,39 | 2,3% |
| Jobs | \$5,20 | 1,4% |
| Gemini Judge | \$3,00 | 0,8% |
| Qwen3 Embedding | \$0,70 | 0,2% |

### 8.2 Custo por Query (Total vs. Claude)

| Cenário | Queries/mês | Total/query | Claude/query |
|---|---:|---:|---:|
| 🌱 Low | 1.500 | \$0,031 | \$0,022 |
| ⚡ Medium | 15.300 | \$0,024 | \$0,022 |
| 🔥 High | 75.000 | \$0,023 | \$0,022 |

---

## 9. Como Escalar Além do High

Para volumes > 2.500 queries/dia, extrapole o custo variável (LLM) linearmente:

**Exemplo — 10.000 queries/dia (~300K/mês):**

| Item | Fórmula | Valor |
|---|---|---:|
| Claude (linear, as-is) | 10K × 30 × \$0,022 | **\$6.600** |
| Gemini Judge | ~4× High | ~\$36 |
| Embedding | ~4× High | ~\$14 |
| Lakebase | 2,0 CU × 8h/d × 30 × 0,21 × \$0,44 | ~\$44 |
| Model Serving | ~8h × 30 × 3 DBU/h × \$0,082 | ~\$59 |
| Jobs | 1x/dia × 45min × 30 × 4 DBU/h × \$0,40 | ~\$36 |
| **Total mensal estimado (as-is)** | | **~\$6.789** |
| **Total mensal estimado (otimizado, -35%)** | | **~\$4.413** |
| **Total anual estimado (otimizado)** | | **~\$52,9K** |

---

## 10. Metodologia e Limites da Estimativa

### 10.1 Fontes Usadas
- **Auditoria direta do código** (`src/rag.py`, `src/prompts.py`, `src/config.py`, `src/ingestion.py`)
- **Anthropic pricing** (Claude Sonnet 4.6): list price público
- **Google AI pricing** (Gemini 2.5 Flash): list price público
- **Databricks public pricing** (Lakebase, Model Serving, Jobs): páginas públicas

### 10.2 Incertezas Principais
1. **Distribuição de paths (happy vs rewrite)** — inferida; monitorar via MLflow traces em produção pode calibrar melhor.
2. **DBU rates exatos de Lakebase por CU-size** — Lakemeter daria precisão ±5%.
3. **Model Serving DBU/hora** — depende do scale-out real observado; valor pode dobrar se medium scale for necessário.
4. **Cold starts** — não modelados; normalmente <2% do total.
5. **Descontos contratuais** — não aplicados (Enterprise típico tem 15–30% off em DBUs).

### 10.3 Como Obter Estimativa Mais Precisa
1. **Lakemeter** (SSO `fe-vm-lakemeter`): cálculo exato por SKU.
2. **Quicksizer**: sizing conversacional multi-workload.
3. **MLflow traces**: habilitar telemetria para medir tokens reais em produção.
4. **Account team**: descontos, CDP, cenários enterprise.

---

## 11. Disclaimer

> ⚠️ **Nota importante:** Estes valores são **estimativas** baseadas em **preços de lista públicos do Azure Databricks Premium** aplicados ao comportamento real do código. Variações podem ocorrer em função de:
> - Região Azure (eastus vs outras: ±10–15%)
> - Tier (Premium vs Standard: ±10–20%)
> - Descontos negociados (MACC, volume, multi-ano, Azure Consumption Commitment)
> - Updates de pricing
> - Padrões reais de tráfego (distribuição real de happy/rewrite paths, tamanho efetivo de contexto)
> - Cloud provider: AWS Premium roda ~1–2% mais barato no total (DBU rates 10–15% menores, mas FMAPI é idêntico)
>
> 🔸 Para decisões de budget, **consulte o time de contas da Databricks**.

---

*Relatório gerado a partir dos slides `Appendix A` e `Appendix B` de `docs/index.html`. Cenários, cálculos e totais sincronizados entre as duas visualizações. Números atualizados após auditoria direta do código do agente (2026-04-23).*
