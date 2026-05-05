# 👥 Esforço de Implantação da PoC em Produção

## 1. O Ponto de Partida

A PoC entregue **já resolve as decisões difíceis**: arquitetura, design do agente, prompts, schema, automação, metodologia de avaliação. O cliente recebe um **blueprint funcional pronto**, não um problema a resolver.

### 1.1 O que o cliente reaproveita da PoC (✅ pronto)

| Categoria | Artefatos prontos |
|---|---|
| 🏗️ **Arquitetura** | Decisões de Lakebase + FMAPI + LangGraph + Model Serving validadas |
| 💻 **Código** | `src/rag.py`, `src/pipeline.py`, `src/ingestion.py`, `src/config.py`, `src/prompts.py` (~1.100 LoC) |
| 📓 **Notebooks** | 4 notebooks orquestrados (`00_setup_lakebase`, `01_deploy_serving`, `02_ingest`, `03_evaluation`) |
| ⚙️ **Automação** | `databricks.yml` (DAB), `resources/jobs.yml`, `Makefile` com 15+ targets |
| 🧪 **Testes** | Suíte pytest com >80% cobertura em `src/` |
| ⚖️ **Avaliação** | Metodologia MLflow GenAI + 4 scorers (incluindo PT-BR Quality custom) |
| 📚 **Documentação** | README, arquitetura de referência, runbooks base |

### 1.2 O que é específico do cliente (🆕 esforço necessário)

| Categoria | Por que precisa de trabalho |
|---|---|
| 🗄️ **Migração de dados** | A wiki MySQL é única do cliente — schema, conteúdo, imagens e usuários |
| ☁️ **Provisão no workspace** | Lakebase, UC, secrets, DAB targets precisam ser criados no ambiente do cliente |
| 🎯 **Customização ao domínio** | Prompts, top-K, chunk size podem precisar ajuste para o conteúdo específico |
| 📊 **Ground truth real** | Perguntas representativas do uso real precisam ser coletadas e curadas |
| ⚙️ **CI/CD interno** | Adaptação ao stack do cliente (GitLab, Azure DevOps, Jenkins, GitHub) |
| 🛡️ **Hardening** | Compliance específico (LGPD, retenção, PII), observabilidade no padrão do cliente |
| ✅ **UAT + transferência** | Validação com usuários reais e capacitação do time operacional |

---

## 2. Time Recomendado

### 2.1 Composição — 7 papéis · 4 pessoas core + 3 de suporte

| # | Papel | Pessoas | Alocação | Duração | Quando atua | Foco principal |
|:---:|---|:---:|:---:|:---:|---|---|
| 1 | 🧠 **Tech Lead / SA** | 1 | **50%** | **10 sem** | Sprints 1–5 (contínuo) | Coordenação, code review, decisões técnicas, alinhamento com Databricks |
| 2 | 🗄️ **DBA / Data Engineer** | 1 | **100%** | **4 sem** | Sprints 1–2 | Migração MySQL → Lakebase, cutover, validação de integridade |
| 3 | 🛠️ **ML / Data Engineer** | 1 | **100%** | **7 sem** | Sprints 2–4 | Pipeline de ingestão, embeddings, integração com Lakebase |
| 4 | 🤖 **AI / Agent Engineer** | 1 | **100%** | **6 sem** | Sprints 2.5–5 | Re-deploy do agente, customização de prompts, Model Serving |
| 5 | ☁️ **DevOps / Cloud** | 1 | **30%** | **6 sem** | Sprint 1 + 4–5 | CI/CD adaptado, secrets, cutover de rede, observabilidade |
| 6 | 📊 **ML Quality Engineer** | 1 | **50%** | **4 sem** | Sprints 3–4 | Ground truth, scorers, tuning de qualidade |
| 7 | 📋 **Product Owner / PM** | 1 | **20%** | **10 sem** | Sprints 1–5 (contínuo) | Priorização, UAT, comunicação com áreas de negócio |

> 🎯 **Resposta direta:** **7 pessoas envolvidas**, com **pico de 4–5 trabalhando em paralelo** durante as Sprints 3–4. Apenas **2 pessoas (Tech Lead + ML/Data Eng)** estão na maior parte do projeto; as outras entram pontualmente.

### 2.2 Linha do Tempo (Gantt) — Cenário Produção · 5 sprints / 10 semanas

```
                    Sprint 1     Sprint 2     Sprint 3     Sprint 4     Sprint 5
                    Sem 1–2      Sem 3–4      Sem 5–6      Sem 7–8      Sem 9–10
                    ──────────   ──────────   ──────────   ──────────   ──────────
🧠 Tech Lead 50%    ██████████   ██████████   ██████████   ██████████   ██████████
🗄️ DBA 100%         ██████████   ██████████   ░░░░░░░░░░   ░░░░░░░░░░   ░░░░░░░░░░
🛠️ ML/Data E. 100%  ░░░░░░░░░░   ██████████   ██████████   ██████████   ░░░░░░░░░░
🤖 AI/Agent  100%   ░░░░░░░░░░   ░░░░░██████   ██████████   ██████████   ██████████
☁️ DevOps     30%   ████░░░░░░   ░░░░░░░░░░   ░░░░░░░░░░   ████░░░░░░   ████░░░░░░
📊 ML Quality 50%   ░░░░░░░░░░   ░░░░░░░░░░   █████░░░░░   █████░░░░░   ░░░░░░░░░░
📋 PO         20%   ██░░░░░░░░   ██░░░░░░░░   ██░░░░░░░░   ████░░░░░░   ████░░░░░░

                    Setup +      Migração     Re-deploy    Hardening    UAT +
                    Migração     Cutover      RAG +        + CI/CD      Transfer.
                    início                    Eval
```

| Sprint | Foco | Pessoas ativas |
|:---:|---|:---:|
| **S1 (sem 1–2)** | Onboarding, provisão de Lakebase/UC/DAB, início da migração | **3** (Tech Lead, DBA, DevOps) |
| **S2 (sem 3–4)** | Cutover MySQL → Lakebase, início do re-deploy do RAG | **4** (Tech Lead, DBA, ML/Data Eng, AI/Agent Eng) |
| **S3 (sem 5–6)** | Customização de prompts, ground truth, avaliação | **5** (Tech Lead, ML/Data Eng, AI/Agent Eng, ML Quality, PO) |
| **S4 (sem 7–8)** | CI/CD interno, hardening, início do UAT | **5** (Tech Lead, ML/Data Eng, AI/Agent Eng, DevOps, ML Quality) |
| **S5 (sem 9–10)** | UAT completo, fine-tuning, transferência ao operacional | **4** (Tech Lead, AI/Agent Eng, DevOps, PO) |

### 2.3 Tamanho Mínimo Viável (MVP de Time)

Se o cliente **não tiver os 7 papéis disponíveis**, é possível fundir responsabilidades:

| Time minimal | 3 pessoas | Como funde |
|---|---|---|
| 🧠 **Tech Lead / SA** | 1 (50–80%) | Acumula PO e parte de DevOps |
| 🛠️ **Full-stack Eng** | 1 (100%) | Acumula DBA + ML/Data Eng (migração + pipeline) |
| 🤖 **AI / Agent Eng** | 1 (100%) | Acumula ML Quality (avaliação + tuning) |

Risco: **calendário estende para ~14 semanas** (vs 10) e o Tech Lead vira gargalo. **Não recomendado** se a wiki tem >5K páginas ou compliance complexo.

---

## 3. Cenários de Escopo

### 3.1 Três Caminhos — Mesmo Time, Calendário Diferente

| Cenário | Time core | Suporte | Calendário | O que entrega |
|---|:---:|:---:|:---:|---|
| 🚀 **Lift & Shift** | 3 pessoas | DevOps part-time | **~6 sem** | Migração + re-deploy 1:1 da PoC + smoke tests. **Sem** customização ao domínio, **sem** hardening, **sem** UAT formal. |
| ⚖️ **Adoção MVP** | 4 pessoas | DevOps + ML Quality | **~8 sem** | Lift & Shift + customização de prompts + ground truth real + UAT light. **Sem** hardening avançado nem CI/CD interno completo. |
| 🏭 **Produção completa** ⭐ | 4 pessoas | DevOps + ML Quality + PO | **~10 sem** | Tudo: customização, hardening, observabilidade, CI/CD interno, UAT completo, transferência ao operacional. **Recomendado.** |

### 3.2 Multiplicador de Maturidade do Time

| Perfil | Multiplicador no calendário | Por quê |
|---|:---:|---|
| 🟢 **Experiente** — domina Databricks, PostgreSQL, MLflow | **1,0×** | Assimila o blueprint rapidamente |
| 🟡 **Intermediário** — conhece Databricks, novo em LangGraph/Lakebase | **1,3×** | 1–2 semanas extras para curva de aprendizado |
| 🔴 **Iniciante** — primeiro projeto Databricks/GenAI | **1,8×** | Onboarding de plataforma + GenAI simultâneo |

> ✏️ Exemplo: **time intermediário · cenário Produção** → 10 sem × 1,3 = **~13 semanas (~3 meses)**.

---

## 4. Cronograma Detalhado por Sprint (Cenário Produção)

### Sprint 1 (Sem 1–2) — Setup & Início da Migração — **3 pessoas**
- 🧠 Tech Lead (50%): code walkthrough da PoC com Databricks BR, alinhamento de arquitetura
- 🗄️ DBA (100%): inventário do MySQL atual, análise de schema, setup do pipeline de migração
- ☁️ DevOps (30%): provisionamento Lakebase, Unity Catalog, secret scope no workspace cliente
- ✅ **Marco:** ambiente do cliente provisionado + plano de cutover aprovado

### Sprint 2 (Sem 3–4) — Cutover + Início do RAG — **4 pessoas**
- 🗄️ DBA (100%): dry-runs de migração + janela de cutover + validação de integridade
- 🛠️ ML/Data Eng (100%): re-deploy do pipeline de ingestão no workspace cliente
- 🤖 AI/Agent Eng (50% partir da metade): code review do agente, primeiros testes locais
- 🧠 Tech Lead (50%): governança do cutover, code review
- ✅ **Marco:** wiki em produção sobre Lakebase, ingestão rodando, smoke test do agente passa

### Sprint 3 (Sem 5–6) — Customização + Avaliação — **5 pessoas**
- 🤖 AI/Agent Eng (100%): customização de prompts ao domínio, ajuste de top-K, chunk size
- 🛠️ ML/Data Eng (100%): integração refinada com Lakebase, indexação completa do conteúdo real
- 📊 ML Quality (50%): coleta de ground truth (≥30 perguntas reais), execução dos 4 scorers
- 📋 PO (20%): priorização baseada em métricas iniciais
- 🧠 Tech Lead (50%): governança
- ✅ **Marco:** baseline de qualidade publicado (alvo: ≥80% em todos os scorers)

### Sprint 4 (Sem 7–8) — CI/CD + Hardening — **5 pessoas**
- ☁️ DevOps (30%): adaptação do GitHub Actions ao stack interno (GitLab/Azure DevOps/Jenkins)
- 🤖 AI/Agent Eng (100%): hardening de prompts, otimização de custo (Haiku, prompt caching)
- 🛠️ ML/Data Eng (100%): MLflow tracing em produção, alertas de DBU
- 📊 ML Quality (50%): análise de falhas + rodada extra de eval pós-hardening
- 🧠 Tech Lead (50%): code review final, sign-off de segurança
- ✅ **Marco:** sistema "production-grade" com observabilidade

### Sprint 5 (Sem 9–10) — UAT + Transferência — **4 pessoas**
- 📋 PO (20%): coordenação do UAT com 2–3 usuários-piloto da wiki
- 🤖 AI/Agent Eng (100%): fine-tuning baseado em feedback real
- ☁️ DevOps (30%): runbooks operacionais, rotação de secrets, plano de DR
- 🧠 Tech Lead (50%): sessão de transferência (1 dia) + Q&A
- ✅ **Marco:** sign-off do PO + time operacional autônomo

---

## 5. Pré-requisitos do Cliente

### 5.1 Antes da Sprint 1
- ✅ Workspace Databricks **Premium** com Unity Catalog habilitado
- ✅ Permissões para criar Lakebase + secret scope + DAB target
- ✅ Acesso a Foundation Model API (Claude Sonnet 4.6, Qwen3 Embedding, Gemini 2.5 Flash)
- ✅ **Acesso somente-leitura** ao MySQL de produção (ou réplica) para análise inicial
- ✅ `LocalSettings.php` atual + lista de extensions/skins do MediaWiki
- ✅ **Tech Lead** alocado já na Sprint 1 (sem ele, atrasa tudo)

### 5.2 Antes da Sprint 2
- ✅ Permissão para gerar **dump completo** do MySQL na janela de cutover
- ✅ Acesso ao diretório `images/` (filesystem ou bucket) para migração de uploads
- ✅ Janela de manutenção de **~4–6 horas** acordada com negócio

### 5.3 Antes da Sprint 5
- ✅ 2–3 usuários-piloto da wiki disponíveis para UAT
- ✅ Time operacional identificado para receber a transferência

---

## 6. Riscos Principais

| # | Risco | Impacto | Mitigação |
|---:|---|:---:|---|
| 1 | **Tech Lead indisponível na S1** | 🔴 Alto | Contrato vinculante de alocação antes de iniciar |
| 2 | **Extensions MediaWiki MySQL-only** | 🔴 Alto | Inventariar na S1; substituir ou aceitar perda funcional documentada |
| 3 | **Wiki >10K páginas ou >50GB de imagens** | 🟡 Médio | Sizing na S1; reservar 1 sprint extra se necessário |
| 4 | **Janela de cutover insuficiente** | 🔴 Alto | Dry-runs em staging; replicação MySQL→PG para minimizar downtime |
| 5 | **Time iniciante em Databricks** | 🟡 Médio | Aplicar multiplicador 1,8×; reservar 1 PD/sem de pair programming com SA |
| 6 | **Compliance específico não previsto** | 🟡 Médio | Discovery na S1 deve mapear LGPD/retenção/auditoria |

---

## 7. Resumo Executivo

| Pergunta | Resposta direta |
|---|---|
| **Quantas pessoas?** | 7 papéis envolvidos; **pico de 4–5 simultaneamente**; 2 pessoas core no projeto inteiro (Tech Lead + ML/Data Eng) |
| **Quais roles indispensáveis?** | Tech Lead/SA, DBA (migração), ML/Data Eng, AI/Agent Eng |
| **Quais roles parciais?** | DevOps (30%), ML Quality (50%), PO (20%) |
| **Quanto tempo?** | **~10 semanas (≈ 2,5 meses)** para cenário Produção, time intermediário-experiente |
| **Pode reduzir o time?** | Sim — MVP de **3 pessoas** é viável (Tech Lead + Full-stack Eng + AI/Agent Eng), mas calendário vai para ~14 semanas |
| **Pode reduzir o calendário?** | Sim — **Lift & Shift em ~6 sem** com 3 pessoas, sem hardening nem UAT completo |

---

## 8. Disclaimer

> ⚠️ Esta estimativa assume **PoC entregue, validada e finalizada** pelo time da Databricks Brazil, com código, testes, documentação e metodologia transferidos ao cliente. Variações reais dependem de:
> - Senioridade efetiva e disponibilidade do time do cliente
> - Estado da wiki MySQL atual (versão, extensions, tamanho real)
> - Complexidade de compliance / segurança (LGPD avançado, auditoria, etc.)
> - Burocracia interna (aprovações, change management, sign-offs)
> - Maturidade da governança de dados existente
>
> 🔸 **Excluído do escopo:** upgrade do MediaWiki se desatualizado, refactor de extensions MySQL-only, front-end customizado, SSO/SAML adicional, fine-tuning de modelo, multi-region.
>
> 🔸 Para uma **proposta formal com SOW**, considere oficina de scoping de 1 dia com a Databricks Field Engineering.

---

*Documento gerado para apoiar o planejamento de adoção da PoC entregue. Calendários assumem sprints de 2 semanas com cerimônias padrão (planning, daily, review, retro). Sincronizado com `docs/estimativa-custos.md`.*
