# 🤖 Documentação do Agente de Vendas - Mercadinho Queiroz

## Visão Geral

Sistema de atendimento automatizado via WhatsApp que utiliza IA para processar pedidos de clientes, buscar produtos e gerenciar carrinho de compras.

---

## 📐 Arquitetura do Sistema

```
┌─────────────────────────────────────────────────────────────────┐
│                        CLIENTE                                   │
│                     (WhatsApp)                                   │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    SERVIDOR FASTAPI                              │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐       │
│  │   Webhook     │  │    Redis      │  │   Cooldown    │       │
│  │   Handler     │  │   Buffer      │  │   Manager     │       │
│  └───────────────┘  └───────────────┘  └───────────────┘       │
└─────────────────────────┬───────────────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────────────┐
│                    AGENTE IA (LangGraph)                         │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐       │
│  │    Gemini     │  │    Tools      │  │    Prompt     │       │
│  │   2.5 Flash   │  │   (6 funcs)   │  │   Optimized   │       │
│  └───────────────┘  └───────────────┘  └───────────────┘       │
└─────────────────────────┬───────────────────────────────────────┘
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  PostgreSQL │   │    API      │   │  Evolution  │
│  (Híbrido)  │   │  Produtos   │   │    API      │
└─────────────┘   └─────────────┘   └─────────────┘
```

---

## 🔍 Busca Híbrida (RAG)

### Componentes

| Componente | Tecnologia | Função |
|------------|------------|--------|
| **Full-Text Search** | PostgreSQL tsvector + GIN | Busca por palavras-chave exatas |
| **Vetorial** | OpenAI text-embedding-3-small + pgvector | Busca semântica por significado |
| **RRF Scoring** | Reciprocal Rank Fusion | Combina rankings das duas buscas |

### Fluxo da Busca

```
              ┌─────────────────┐
              │   Query Input   │
              │  "tomate kg"    │
              └────────┬────────┘
                       │
          ┌────────────┴────────────┐
          │                         │
          ▼                         ▼
   ┌──────────────┐        ┌──────────────┐
   │ Full-Text    │        │  Vetorial    │
   │ (tsvector)   │        │  (embedding) │
   │              │        │              │
   │ plainto_     │        │ embedding    │
   │ tsquery()    │        │ <=> query    │
   └──────┬───────┘        └──────┬───────┘
          │                       │
          └───────────┬───────────┘
                      │
                      ▼
              ┌──────────────┐
              │ RRF Scoring  │
              │              │
              │ score = Σ    │
              │ 1/(k+rank)   │
              └──────┬───────┘
                     │
                     ▼
              ┌──────────────┐
              │ Setor Boost  │
              │ HORTI: +0.5  │
              │ FRIGO: +0.5  │
              └──────┬───────┘
                     │
                     ▼
              ┌──────────────┐
              │  Resultados  │
              │   Rankeados  │
              └──────────────┘
```

### Função SQL: `hybrid_search_v2`

```sql
-- Parâmetros
hybrid_search_v2(
    query_text text,           -- Texto de busca
    query_embedding vector,    -- Embedding da query
    match_count int,           -- Quantidade de resultados
    full_text_weight float,    -- Peso FTS (padrão: 1.0)
    semantic_weight float,     -- Peso vetorial (padrão: 1.0)
    setor_boost float,         -- Boost HORTI-FRUTI/FRIGORIFICO (padrão: 0.5)
    rrf_k int                  -- Parâmetro RRF (padrão: 50)
)
```

---

## 🛠️ Tools Disponíveis

| Tool | Função | Quando usar |
|------|--------|-------------|
| `search_products_vector` | Busca produtos por nome/descrição | Cliente pede produto |
| `estoque_preco` | Consulta estoque e preço por EAN | Verificar disponibilidade |
| `ean_lookup` | Busca EAN por código de barras | Cliente informa código |
| `get_current_time` | Retorna horário atual | Verificar funcionamento |
| `pedidos` | Cria/atualiza pedido | Fechar pedido |
| `estoque` | Consulta estoque geral | Verificar disponibilidade |

---

## 📊 Banco de Dados

### Tabela: `produtos_vectors_ean`

| Coluna | Tipo | Descrição |
|--------|------|-----------|
| `id` | uuid | Identificador único |
| `text` | text | Texto do produto (nome + setor + categoria) |
| `embedding` | vector(1536) | Embedding OpenAI |
| `metadata` | jsonb | EAN, setor, categoria, subcategoria |
| `fts` | tsvector | Full-text search (gerado automaticamente) |

### Índices

```sql
-- Índice para busca vetorial
CREATE INDEX ON produtos_vectors_ean USING ivfflat (embedding vector_cosine_ops);

-- Índice para Full-Text Search
CREATE INDEX ON produtos_vectors_ean USING gin(fts);
```

---

## 🔄 Traduções Automáticas

O sistema traduz termos comuns para melhorar a busca:

| Cliente digita | Sistema busca |
|----------------|---------------|
| absorvente | abs absorvente |
| achocolatado | achoc |
| refrigerante | refrig |
| cachorro quente | pao hot dog maxpaes |
| creme crack | bolacha cream cracker |
| musarela | queijo mussarela |
| guarana | refrig guarana antarctica |

---

## ⚠️ Configuração Importante: Acentos

> **ATENÇÃO PARA NOVOS CLIENTES:** Se o banco de dados do cliente **NÃO TEM ACENTOS** nos nomes dos produtos (comum em sistemas legados/ERPs), é necessário informar isso no prompt do agente.

### Adicione no prompt:
```markdown
> ⚠️ **BUSCAS SEM ACENTO:** O banco de dados **NÃO TEM ACENTOS**. 
> Sempre busque removendo acentos e cedilhas:
> - açúcar → acucar
> - café → cafe
> - feijão → feijao
> - maçã → maca
```

### Ou adicione traduções no código (`db_vector_search.py`):
```python
TERM_TRANSLATIONS = {
    "açúcar": "acucar cristal",
    "café": "cafe",
    "feijão": "feijao",
    # ...
}
```

## 📈 Métricas e Custos

### Custo por Interação (estimado)
- **Embedding query**: ~$0.00002
- **LLM (Gemini 2.5 Flash)**: ~$0.002/interação
- **Total**: ~$0.002 USD por mensagem (~R$0.012)

### Tokens Médios
- Prompt: ~20.000 tokens
- Completion: ~500-1.000 tokens

---

## 🚀 Deploy

### Variáveis de Ambiente Necessárias

```env
# API Keys
OPENAI_API_KEY=sk-...
GOOGLE_API_KEY=...

# Banco de Dados
VECTOR_DB_CONNECTION_STRING=postgres://user:pass@host:port/db
PRODUCTS_DB_CONNECTION_STRING=postgres://...

# WhatsApp
EVOLUTION_API_URL=https://...
EVOLUTION_API_KEY=...
EVOLUTION_INSTANCE=...

# Redis
REDIS_HOST=...
REDIS_PORT=6379
REDIS_PASSWORD=...
```

### Comandos

```bash
# Vetorizar produtos (apenas quando adicionar novos)
python scripts/vetorize_products_txt.py

# Rodar servidor
uvicorn server:app --host 0.0.0.0 --port 8000
```

---

## 📝 Atualizações Recentes

1. **RAG Híbrido**: Implementado FTS + Vetorial com RRF
2. **Boost de Setores**: +0.5 para HORTI-FRUTI e FRIGORIFICO
3. **Traduções**: Termos comuns traduzidos automaticamente
4. **17.415 produtos** vetorizados

---

*Última atualização: Janeiro 2026*
