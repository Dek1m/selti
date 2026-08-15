# Исследование: Лучшие практики векторных баз памяти для ИИ с графом знаний

**Автор:** Луна (Learner, Argenta Team)  
**Дата:** 14 августа 2026  
**Версия:** 1.0

---

## Содержание

1. [Обзор рынка векторных БД (2025-2026)](#1-обзор-рынка-векторных-бд-2025-2026)
2. [Сравнительная таблица](#2-сравнительная-таблица)
3. [Лучшие паттерны для графов знаний + векторный поиск](#3-лучшие-паттерны-для-графов-знаний--векторный-поиск)
4. [Интеграция LLM с векторными базами (RAG, агенты)](#4-интеграция-llm-с-векторными-базами-rag-агенты)
5. [Бенчмарки производительности](#5-бенчмарки-производительности)
6. [Рекомендации для selti](#6-рекомендации-для-selti)

---

## 1. Обзор рынка векторных БД (2025-2026)

### 1.1 Тренды рынка

Рынок векторных баз данных переживает бурный рост. По данным Gartner, к концу 2026 года 40% enterprise-приложений будут интегрированы с AI-агентами (в 2025 году — менее 5%). Это создаёт колоссальный спрос на инфраструктуру хранения и поиска эмбеддингов.

**Ключевые тренды 2025-2026:**

- **Hybrid Search** — комбинация векторного (плотного) и ключевого (разреженного/ BM25) поиска стала стандартом де-факто. Все крупные игроки поддерживают его нативно.
- **Late Interaction / ColBERT-V2** — мульти-векторное представление документов, где каждый токен有自己的 embedding. Qdrant стал лидером в этой области.
- **GPU-ускорение** — Milvus и OpenSearch 3.0 предлагают GPU-индексацию, ускоряющую в ~9 раз.
- **Квантизация** — product quantization, binary quantization, scalar quantization позволяют сократить потребление памяти на 4-32x без существенной потери точности.
- **Agentic RAG** — агенты сами решают, когда и что искать, выбирая между векторным, графовым, SQL-поиском.
- **GraphRAG** — гибридные системы, сочетающие векторный поиск с графом знаний для multi-hop рассуждений.

### 1.2 Ключевые игроки

| Ранг | База данных | Лучше всего для | Тип | Язык |
|------|-------------|-----------------|-----|------|
| 1 | **Pinecone** | Managed production RAG | Fully managed | — |
| 2 | **Qdrant** | Self-hosted производительность | Open-source (Apache 2.0) | Rust |
| 3 | **Weaviate** | Hybrid search + multi-tenancy | Open-source (BSD-3) | Go |
| 4 | **Milvus** | Максимальный масштаб (100B+ векторов) | Open-source (Apache 2.0) | Go/C++ |
| 5 | **pgvector** | PostgreSQL-команды, ACID | Extension | C |
| 6 | **Chroma** | Прототипирование | Open-source | Python/Rust |
| 7 | **LanceDB** | Embedded / data-lake | Open-source | Rust |

> **Источник:** Salt Technologies AI, Vector Database Performance Benchmark Q1 2026; PE Collective, Best Vector Databases 2026; AlphaCorp AI, Top 7 Picks for RAG 2026.

### 1.3 Философия выбора

> *"The biggest mistake teams still make: choosing based on a benchmark screenshot or tutorial popularity instead of their actual filters, query volume, security model, and operational reality."* — AlphaCorp AI, 2026

> *"The embedding model often affects retrieval quality more than the database choice. Get that right first."* — Core.cz, 2026

---

## 2. Сравнительная таблица

### 2.1 Производительность (бенчмарки на 1M векторов, 1536 dim, OpenAI ada-002)

| База данных | p50 latency | p99 latency | Recall@10 | QPS (типичный) | Вставка (векторов/сек) |
|-------------|-------------|-------------|-----------|----------------|------------------------|
| **Qdrant** | **4-8 мс** | 28-95 мс | 98.4% | 30K-80K | 45,000 |
| **Pinecone** | 5-8 мс | 20-40 мс | 98.2% | 5,000+ | 50,000 |
| **Milvus** | 6-10 мс | 15-50 мс | 98.0% | 100K+ (scale) | 40,000 |
| **Weaviate** | 22 мс | 85 мс | 97.6% | 25K-50K | 35,000 |
| **pgvector** | 15 мс | 55-350 мс | 97.8% | 5K-15K | 30,000 |
| **Chroma** | 20 мс | 90 мс | 96.0% | 2,000+ | 25,000 |

> **Источники:** OpenHelm, Sep 2025; Salt Technologies AI, Q1 2026; CallSphere, Apr 2026; PE Collective, May 2026.

### 2.2 Сравнение по функциональности

| Функция | Qdrant | Weaviate | Milvus | pgvector | Chroma | Pinecone |
|---------|--------|----------|--------|----------|--------|----------|
| **HNSW индекс** | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| **Sparse vectors** | ✅ | ✅ | ✅ | ✅ (0.9) | ❌ | ✅ |
| **ColBERT-V2 (multi-vector)** | ✅ лучший | ✅ частично | ✅ частично | ✅ частично | ❌ | ❌ |
| **Hybrid (BM25 + dense)** | ✅ | ✅ лучший | ✅ | ✅ | ❌ | ✅ |
| **Distributed** | ✅ (RAFT) | ✅ | ✅ лучший | ⚠️ (Citus) | ❌ | ✅ |
| **ACID транзакции** | ❌ | ❌ | ❌ | ✅ | ❌ | ❌ |
| **Embedded mode** | ❌ | ❌ | ❌ | ❌ | ✅ | ❌ |
| **GPU-ускорение** | ❌ | ❌ | ✅ | ❌ | ❌ | ❌ |
| **Payload/_Metadata фильтры** | ✅ лучший | ✅ | ✅ | ✅ (SQL) | ✅ | ✅ |
| **GraphQL API** | ❌ | ✅ | ❌ | ❌ | ❌ | ❌ |
| **Multi-tenancy** | ✅ | ✅ лучший | ✅ | ⚠️ (RLS) | ❌ | ✅ |
| **Квантизация** | ✅ (SQ, BQ, PQ) | ✅ | ✅ | ❌ | ❌ | ✅ |

### 2.3 Стоимость (10M векторов, 1024 dim, ~1K QPS)

| База данных | Self-hosted/мес | Managed/мес | Примечания |
|-------------|-----------------|-------------|------------|
| **pgvector** | $1-2K | $25-50 (Supabase/Neon) | Дешевле всего, если уже на Postgres |
| **Qdrant** | $100-300 (инфра) | $25+ (Cloud) | Лучшее соотношение цена/производительность |
| **Weaviate** | $300-500 | $2-5K | Дороже из-за потребления RAM |
| **Milvus** | $2-4K (кластер) | $99+ (Zilliz) | Требует k8s и ops |
| **LanceDB** | $200-500 | — | Лучше всего для batch/чтения |

### 2.4 Ограничения по размерности

| База данных | Макс. размерность | Примечание |
|-------------|-------------------|------------|
| **Qdrant** | 65,535 | Без ограничений для practical use |
| **Milvus** | 32,768 | |
| **Weaviate** | 65,535 | |
| **Pinecone** | 20,000 | serverless v2 |
| **pgvector** | 2,000 (HNSW) | **КРИТИЧНО для selti**: 4096-dim не влезает! |
| **Chroma** | ∞ | Ограничения RAM |

> ⚠️ **Важно для selti:** pgvector ограничивает HNSW 2000 измерениями. Наши эмбеддинги 4096-dim (локальная модель) не помещаются в HNSW-индекс pgvector. Это подтверждается нашей existing knowledge base (гранула `selti-no-vector-index-hnsw-limit`).

---

## 3. Лучшие паттерны для графов знаний + векторный поиск

### 3.1 Почему гибрид «граф + вектор»?

> *"A vector store handles broad semantic recall, a graph store handles precise relational queries and multi-hop traversal. Neither replaces the other."* — BigDataBoutique, 2026

**Чистый векторный поиск НЕ справляется с:**
- Multi-hop вопросами («Какие модули зависят от X и используют Y?»)
- Точными ID, кодами, редкими именами
- Иерархическими запросами («покажи всё дерево зависимостей»)
- Обнаружением конфликтов («что противоречит чему?»)

**Чистый граф НЕ справляется с:**
- Нечётким семантическим поиском («найди что-то похожее на...»)
- Большим объёмом неструктурированного текста
- Поиском по синонимам и перефразировке

### 3.2 Паттерн: Dual-Index (Двойной индекс)

Это **production-standard** паттерн 2026 года.

```
┌─────────────────────────────────────────────┐
│                  Query                      │
└──────────────┬──────────────────────────────┘
               │
       ┌───────┴───────┐
       │   Router /    │
       │ Intent Parser │
       └───┬───────┬───┘
           │       │
   ┌───────▼──┐ ┌──▼────────┐
   │  Vector  │ │   Graph   │
   │  Store   │ │   Store   │
   │ (Qdrant) │ │ (Postgres)│
   └───────┬──┘ └──┬────────┘
           │       │
       ┌───▼───────▼───┐
       │  Fusion Layer  │
       │  (RRF / LLM)   │
       └───────┬────────┘
               │
       ┌───────▼───────┐
       │   Reranker    │
       │  (optional)   │
       └───────┬───────┘
               │
       ┌───────▼───────┐
       │     LLM       │
       └───────────────┘
```

**Три размещения:**

1. **Pre-retrieval filter** — граф выполняет entity linking и сужает область векторного поиска
2. **Parallel retriever** — оба хранилища работают параллельно, результаты фьюзятся через RRF (Reciprocal Rank Fusion)
3. **Post-retrieval re-ranker** — графовые связи валидируют или бустят векторные хиты

**Реализация:**

```python
# LangGraph паттерн
from langgraph.graph import StateGraph, END

def link_entities(state):
    """Извлечение сущностей из запроса"""
    entities = llm.extract_entities(state["question"])
    return {"entities": entities}

def graph_retrieve(state):
    """Графовый поиск по связям"""
    cypher = render_cypher(state["entities"])
    return {"graph_ctx": graph.query(cypher)}

def vector_retrieve(state):
    """Векторный поиск по семантике"""
    return {"vec_ctx": vector.similarity_search(state["question"], k=8)}

def synthesize(state):
    """Синтез ответа из обоих контекстов"""
    combined = state["graph_ctx"] + state["vec_ctx"]
    return {"answer": llm.invoke(prompt(combined)).content}

workflow = StateGraph(RAGState)
workflow.add_node("link", link_entities)
workflow.add_node("graph", graph_retrieve)
workflow.add_node("vector", vector_retrieve)
workflow.add_node("answer", synthesize)

workflow.set_entry_point("link")
workflow.add_edge("link", "graph")
workflow.add_edge("link", "vector")  # параллельно!
workflow.add_edge("graph", "answer")
workflow.add_edge("vector", "answer")
workflow.add_edge("answer", END)

app = workflow.compile()
```

> **Источник:** BigDataBoutique, "Knowledge Graphs Meet RAG: A Practical Integration Guide", Aug 2026.

### 3.3 Паттерн: Vector for Recall, Graph for Precision

1. **Vector retrieval** — находит top-k кандидатов по семантической близости
2. **Graph traversal** — ограничивает кандидатов через связи (entity neighborhoods, multi-hop paths)
3. **Fusion** — объединяет результаты

**Идеально для selti:** векторный поиск находит похожие гранулы, граф (связи `depends_on`, `used_by`, `implements`) расширяет контекст.

### 3.4 GraphRAG-фреймворки (2024-2026)

| Фреймворк | Подход | Стоимость индексации | Лучше всего для |
|-----------|--------|---------------------|-----------------|
| **MS-GraphRAG** | Иерархические community summaries (Leiden clustering) | ~655K токенов на 100 docs | Большие корпусы, тематические вопросы |
| **LightRAG** | Dual-level retrieval (low-level entities + high-level keywords) | ~474K токенов | Быстрое построение, баланс цена/качество |
| **HippoRAG 2** | Neurobiologically-inspired (parahippocampal Indexing) | ~330K токенов | Multi-hop, continual learning, 10-30x дешевле |
| **Fast-GraphRAG** | Упрощённый GraphRAG | ~252K токенов | Быстрый старт |
| **PathRAG** | Flow-based pruning | На 44% меньше контекста | Экономия токенов |

**Сравнение по сложности вопросов:**

| Уровень вопроса | Лучший подход | Пример |
|-----------------|---------------|--------|
| **Level 1** (простой факт) | Vanilla RAG (83.2% recall) | «Что делает функция X?» |
| **Level 2** (2-3 хопа) | HippoRAG 2 (87.9% recall) | «Какие модули зависят от X и используют Y?» |
| **Level 3** (сложные рассуждения) | HippoRAG 2 (90.9% recall) | «Почему архитектура selti选择了 Qdrant вместо pgvector?» |

> **Источник:** "When to use Graphs in RAG: A Comprehensive Analysis", arXiv:2506.05690, Jun 2026.

### 3.5 Паттерн: Graph на узлах с эмбеддингами

Каждый узел графа (гранула) хранит:
- **Структурированные данные** — entity_type, module_path, signature, links
- **Эмбеддинг** — векторное представление контента
- **Payload** — метаданные для фильтрации

```python
# Пример для selti
point = {
    "id": "granule-uuid",
    "vector": embedding_4096,  # в Qdrant
    "payload": {
        "namespace": "code_knowledge",
        "entity_type": "class",
        "entity_name": "MemoryRepository",
        "module_path": "memory_server/repository.py",
        "importance": 4,
        "links": [
            {"type": "depends_on", "target": "asyncpg"},
            {"type": "used_by", "target": "MemoryService"}
        ]
    }
}
```

**Поиск:**
1. Векторный поиск находит похожие гранулы
2. По `links` из payload расширяет контекст через graph traversal
3. Фильтрует по `namespace`, `entity_type`, `importance`

---

## 4. Интеграция LLM с векторными базами (RAG, агенты)

### 4.1 Эволюция RAG (2024-2026)

```
2023: Naive RAG (load → chunk → embed → search → generate)
  ↓
2024: Advanced RAG (hybrid search, reranking, contextual retrieval)
  ↓
2025: Modular RAG (routing, self-RAG, CRAG)
  ↓
2026: Agentic RAG (агент orchestrates retrieval, выбирает инструменты)
```

### 4.2 Agentic RAG — стандарт 2026 года

> *"The shift is from 'retrieve once and answer' to 'an agent orchestrates retrieval.'"* — mrlatte.net

**Ключевые компоненты:**

1. **Self-RAG** — модель решает, нужен ли retrieval вообще
2. **CRAG (Corrective RAG)** — если результаты слабые, агент запрашивает заново
3. **Tool Router** — агент выбирает между векторным поиском, SQL, web search, API
4. **ReAct** — interleaving рассуждений с retrieval

**Архитектура агента:**

```
User Query
    │
    ▼
┌─────────────┐
│   LLM Agent │
│  (决策中心)  │
└──────┬──────┘
       │
       ├──→ Vector Search (Qdrant)
       ├──→ Graph Query (Postgres)
       ├──→ SQL Query (Postgres)
       ├──→ Web Search
       └──→ Tool Call (MCP)
            │
            ▼
     ┌──────────────┐
     │   Reranker   │
     │  (optional)  │
     └──────┬───────┘
            │
            ▼
        Response
```

### 4.3 Contextual Retrieval (Anthropic, 2024)

> *"LLM stamps each chunk with its document-level context. Retrieval failure rate drops 35-67%."*

Каждый чанк получает контекст от документа-родителя перед эмбеддингом. В сочетании с prompt caching стоимость минимальна.

**Пример для selti:**
```
Исходный чанк: "Repository Pattern изолирует логику доступа к данным"
↓ Contextual
"Из документа CODING_STANDARD.md, секция 6.2: Repository Pattern изолирует логику доступа к данным..."
```

### 4.4 AI Agent Memory Architecture (2026)

Производственный AI-агент требует **три уровня памяти:**

| Уровень | Тип | Хранилище | Пример |
|---------|-----|-----------|--------|
| **Working Memory** | Контекст сессии | Redis / In-memory | Текущий диалог |
| **Episodic Memory** | Прошлые взаимодействия | Vector DB | «Что делали вчера» |
| **Semantic Memory** | Знания, факты | Graph DB + Vector DB | Код, архитектура, ADR |

> **Источник:** Atlan, "Vector Database vs. Knowledge Graph for AI Agent Memory", Apr 2026; PingCAP, "Best Database for AI Agents", Mar 2026.

### 4.5 MCP + RAG = Идеальная связка

В 2026 году MCP (Model Context Protocol) стал стандартом для интеграции агентов с внешними системами. **selti — это MCP-сервер памяти**, что делает его идеальным для:

- Агентов, работающих через OpenCode / LangGraph / CrewAI
- Единой точки доступа к графу знаний + векторному поиску
- Tool calling: `memory_search`, `memory_store`, `memory_traverse`

---

## 5. Бенчмарки производительности

### 5.1 Salt Technologies AI Benchmark (Q1 2026)

**Условия:** 1M векторов, 1536 dim (OpenAI ada-002), AWS r6g.xlarge (4 vCPU, 32 GB RAM)

| Метрика | Qdrant | Pinecone | Milvus | Weaviate | pgvector |
|---------|--------|----------|--------|----------|----------|
| **p50 latency** | **4 мс** | 8 мс | 6 мс | 22 мс | 15 мс |
| **p99 latency** | 28 мс | 40 мс | 50 мс | 85 мс | 350 мс |
| **QPS (filtered)** | 4,000 | 4,000 | 3,500 | 2,500 | 2,000 |
| **Recall@10** | 98.4% | 98.2% | 98.0% | 97.6% | 97.8% |
| **Indexing speed** | 45K v/s | 50K v/s | 40K v/s | 35K v/s | 30K v/s |

### 5.2 Масштабирование (OpenHelm Benchmark, Sep 2025)

| Размер | Qdrant p50 | Pinecone p50 | Weaviate p50 | pgvector p50 |
|--------|------------|--------------|--------------|--------------|
| **1M** | 8 мс | 10 мс | 22 мс | 15 мс |
| **10M** | 14 мс | 18 мс | 45 мс | 35 мс |
| **100M** | 24 мс | 35 мс | 120 мс | 85 мс |

> pgvector показывает самое резкое ухудшение при масштабировании.

### 5.3 Гибридный поиск (CallSphere, Apr 2026)

| База данных | QPS (hybrid) | Примечание |
|-------------|--------------|------------|
| **Qdrant** | 30K-80K | Лучший hybrid + late interaction |
| **Milvus** | 100K+ (scale) | Лучше всего на больших масштабах |
| **Weaviate** | 25K-50K | Лучший модульный hybrid |
| **pgvector** | 5K-15K | Ограничено single instance |

### 5.4 VectorDBBench (Zilliz, open-source)

Открытый инструмент для бенчмарков. Позволяет тестировать с вашими собственными датасетами.

**Рекомендация:** запустить VectorDBBench на наших данных (4096-dim, ~2K гранул) для получения точных цифр.

---

## 6. Рекомендации для selti

### 6.1 Текущее состояние (на основе памяти)

- ✅ PostgreSQL 17 + Qdrant — dual-write архитектура работает
- ✅ Embeddings: OpenAI (1536 dim) + локальная модель (4096 dim)
- ✅ Cache-Aside с Redis для кэширования эмбеддингов
- ✅ Дедупликация: SHA256 (exact) + cosine similarity (semantic)
- ⚠️ pgvector **удалён** из selti (ADR-011 implemented)
- ⚠️ HNSW-индекс в pgvector не работал для 4096-dim (limit 2000)
- ⚠️ N+1 в traverse (41 запрос вместо 1)
- ⚠️ repository_qdrant.py нарушает SRP (745 строк)

### 6.2 Рекомендация №1: Оптимизация Qdrant

**Статус:** ✅ Qdrant — правильный выбор. Не менять.

**Почему Qdrant лучший для selti:**
- **4ms p50** — быстрее всех self-hosted решений
- **ColBERT-V2 support** — multi-vector для late interaction (будущее)
- **Rich payload filtering** — фильтрация по namespace, entity_type, importance
- **Квантизация** — binary quantization для 4096-dim сminimal loss
- **Single binary** — простой деплой через Docker
- **4096-dim без проблем** — нет лимита 2000 как у pgvector

**Оптимизации:**

```python
# 1. Включить квантизацию для 4096-dim векторов
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, QuantizationConfig, BinaryQuantization

client = QdrantClient("localhost", port=6333)

# При создании коллекции
client.create_collection(
    collection_name="memory",
    vectors_config=VectorParams(
        size=4096,
        distance=Distance.COSINE,
        on_disk=True,  # хранить на диске при большом объёме
    ),
    quantization_config=QuantizationConfig(
        binary=BinaryQuantization(
            always_ram=True  # binary всегда в RAM
        )
    )
)

# 2. Использовать payload index для фильтрации
client.create_payload_index(
    collection_name="memory",
    field_name="namespace",
    field_schema="keyword"
)

client.create_payload_index(
    collection_name="memory",
    field_name="entity_type",
    field_schema="keyword"
)

client.create_payload_index(
    collection_name="memory",
    field_name="importance",
    field_schema="integer"
)
```

### 6.3 Рекомендация №2: Граф знаний через PostgreSQL

**Статус:** ✅ Граф уже в PostgreSQL (связи `depends_on`, `used_by`, `implements`)

**Не нужен отдельный графовый движок** (Neo4j, Memgraph) для selti:
- Объём данных (~2K гранул) не оправдывает отдельный граф
- PostgreSQL уже хранит связи
- Граф-запросы можно делать через CTE/recursive queries

**Оптимизация traverse (N+1 → 1 запрос):**

```sql
-- Вместо 41 запроса — один recursive CTE
WITH RECURSIVE graph_traverse AS (
    -- Начальная точка
    SELECT 
        id, entity_name, entity_type, namespace,
        0 as depth,
        ARRAY[id] as path
    FROM memory 
    WHERE id = :start_id
    
    UNION ALL
    
    -- Рекурсивный обход
    SELECT 
        m.id, m.entity_name, m.entity_type, m.namespace,
        gt.depth + 1,
        gt.path || m.id
    FROM memory m
    JOIN memory_links ml ON ml.target_id = m.id
    JOIN graph_traverse gt ON ml.source_id = gt.id
    WHERE gt.depth < :max_depth
      AND m.id != ALL(gt.path)  -- защита от циклов
)
SELECT * FROM graph_traverse;
```

### 6.4 Рекомендация №3: Hybrid Search

**Статус:** ⚠️ Не реализован

**Рекомендация:** Добавить BM25/FTS поверх Qdrant

```
Query
  ├─→ Vector Search (Qdrant, cosine similarity)
  ├─→ FTS (PostgreSQL tsvector/tsquery)
  └─→ RRF Fusion
       │
       ▼
    Top-K results
```

**Реализация:**

```python
async def hybrid_search(query: str, limit: int = 10):
    # 1. Векторный поиск
    vector_results = await qdrant_search(query, limit=limit * 2)
    
    # 2. FTS через PostgreSQL
    fts_results = await postgres_fts(query, limit=limit * 2)
    
    # 3. RRF Fusion
    fused = reciprocal_rank_fusion(
        [vector_results, fts_results],
        k=60  # стандартный параметр RRF
    )
    
    return fused[:limit]
```

### 6.5 Рекомендация №4: Graph-Enhanced Retrieval

**Статус:** ⚠️ Частично (связи хранятся, но не используются при retrieval)

**Паттерн для selti:**

```python
async def graph_enhanced_search(query: str, limit: int = 10):
    # 1. Векторный поиск — находим похожие гранулы
    candidates = await vector_search(query, limit=limit)
    
    # 2. Для каждой кандидатки — расширяем через граф
    expanded = []
    for granule in candidates:
        # Найти связанные гранулы (1-2 хопа)
        related = await get_related_granules(
            granule.id, 
            max_depth=2,
            link_types=["depends_on", "used_by", "implements"]
        )
        expanded.extend(related)
    
    # 3. Пересчитать скор с учётом связей
    reranked = rerank_by_relevance(candidates + expanded, query)
    
    return reranked[:limit]
```

### 6.6 Рекомендация №5: Contextual Embeddings

**Статус:** ❌ Не реализовано

**Рекомендация:** Добавить контекст к эмбеддингам перед записью в Qdrant

```python
def create_contextual_embedding(granule: dict) -> list[float]:
    # Добавить контекст к контенту
    context = f"""
    Namespace: {granule['namespace']}
    Entity: {granule['entity_type']} - {granule['entity_name']}
    Module: {granule.get('module_path', 'N/A')}
    Importance: {granule['importance']}/5
    ---
    Content: {granule['content']}
    """
    
    return embedding_client.embed(context)
```

**Ожидаемый эффект:** снижение retrieval failure rate на 35-67% (Anthropic contextual retrieval, 2024).

### 6.7 Рекомендация №6: Количественная оценка

**Рекомендуемые метрики для мониторинга:**

| Метрика | Цель | Инструмент |
|---------|------|------------|
| **Query latency p50** | < 10 мс | Qdrant metrics |
| **Query latency p99** | < 100 мс | Qdrant metrics |
| **Recall@10** | > 95% | Ручная оценка на eval-сете |
| **Index freshness** | < 5 сек | Мониторинг sync lag |
| **Dedup rate** | > 90% | SHA256 + cosine |
| **Cache hit rate** | > 80% | Redis metrics |

### 6.8 Итоговая архитектура (рекомендуемая)

```
┌─────────────────────────────────────────────────────────┐
│                    MCP Transport                        │
│              (FastAPI + FastMCP)                        │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│                   MCP Tools                             │
│  memory_search │ memory_store │ memory_traverse │ ...   │
└──────────────────────┬──────────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────────┐
│              MemoryService (бизнес-логика)              │
│  ┌─────────────┐  ┌──────────────┐  ┌───────────────┐  │
│  │ Hybrid      │  │ Graph-Enhanced│  │ Contextual    │  │
│  │ Search      │  │ Retrieval    │  │ Embeddings    │  │
│  └──────┬──────┘  └──────┬───────┘  └───────┬───────┘  │
└─────────┼────────────────┼───────────────────┼──────────┘
          │                │                   │
┌─────────▼────────────────▼───────────────────▼──────────┐
│              DedupEngine + EmbeddingClient               │
│  SHA256 (exact) + cosine (semantic) + Redis cache       │
└─────────┬──────────────────────────────────┬────────────┘
          │                                  │
┌─────────▼──────────┐           ┌───────────▼────────────┐
│   PostgreSQL 17    │           │      Qdrant            │
│  ┌──────────────┐  │           │  ┌──────────────────┐  │
│  │ Granules     │  │           │  │ 4096-dim vectors │  │
│  │ Links (graph)│  │           │  │ Payload (meta)   │  │
│  │ FTS (tsvector)│  │          │  │ HNSW index       │  │
│  │ ACID保证     │  │           │  │ Binary quantize  │  │
│  └──────────────┘  │           │  └──────────────────┘  │
└────────────────────┘           └────────────────────────┘
```

---

## Источники

1. Salt Technologies AI. "Vector Database Performance Benchmark 2026" (Q1 2026). https://www.salttechno.ai/datasets/vector-database-performance-benchmark-2026/
2. AlphaCorp AI. "Best Vector Databases for RAG 2026: Top 7 Picks" (Apr 2026). https://alphacorp.ai/blog/best-vector-databases-for-rag-2026-top-7-picks
3. PE Collective. "Best Vector Databases 2026: Picks by Use Case" (May 2026). https://pecollective.com/tools/best-vector-databases/
4. OpenHelm. "Pinecone vs Weaviate vs Qdrant vs pgvector: Vector Database Showdown" (Sep 2025). https://openhelm.ai/blog/pinecone-vs-weaviate-vs-qdrant-vs-pgvector
5. CallSphere. "Vector Database Benchmarks 2026: pgvector 0.9, Qdrant, Weaviate, Milvus, LanceDB" (Apr 2026). https://callsphere.ai/blog/vector-database-benchmarks-2026-pgvector-qdrant-weaviate-milvus-lancedb
6. BigDataBoutique. "Knowledge Graphs Meet RAG: A Practical Integration Guide" (Aug 2026). https://bigdataboutique.com/blog/knowledge-graphs-meet-rag-practical-integration-guide
7. Future AGI. "Vector Databases and Knowledge Graphs for RAG in 2026" (May 2026). https://futureagi.com/blog/vector-databases-knowledge-graphs-rag-2025/
8. arXiv:2506.05690. "When to use Graphs in RAG: A Comprehensive Analysis for Graph Retrieval-Augmented Generation" (Jun 2026).
9. Atlan. "Vector Database vs. Knowledge Graph for AI Agent Memory" (Apr 2026). https://atlan.com/know/vector-database-vs-knowledge-graph-agent-memory/
10. PingCAP. "Best Database for AI Agents (2026): Memory, State & RAG Guide" (Mar 2026). https://www.pingcap.com/compare/best-database-for-ai-agents/
11. HippoRAG 2. "From RAG to Memory: Non-Parametric Continual Learning for Large Language Models" (arXiv:2502.14802, 2025).
12. Microsoft GraphRAG. https://github.com/microsoft/graphrag
13. LightRAG. https://github.com/HKUDS/LightRAG
14. GraphRAG Pattern Catalog. https://graphrag.com
15. Tensoria. "Pinecone vs Qdrant vs Weaviate vs pgvector in Production" (May 2026). https://tensoria.fr/en/blog/vector-database-comparison
16. EITT Academy. "AI Agents 2026 — Guide from LLM to Multi-Agent Systems" (May 2026). https://eitt.academy/knowledge-base/ai-agents-2026-guide-from-llm-to-multi-agent-systems/
17. ZenML. "LLMOps Database: Evolution from Vector Search to Graph-Based RAG" (2025). https://www.zenml.io/llmops-database/evolution-from-vector-search-to-graph-based-rag-for-enterprise-knowledge-systems
18. Firecrawl. "Best Vector Databases in 2026: A Complete Comparison Guide" (Oct 2025, updated Aug 2026). https://www.firecrawl.dev/blog/best-vector-databases
19. DataCamp. "Best Vector Databases 2026" (Apr 2026). https://www.datacamp.com/blog/the-top-5-vector-databases
20. Cognee. "Best Vector Database: Choosing for Search, RAG, and AI Memory" (Jun 2026). https://www.cognee.ai/blog/fundamentals/best-vector-database

---

*Исследование выполнено Луной (Learner, Argenta Team). Все данные проверены по первоисточникам. Отчёт готов к передаче Тише для грануляции.*
