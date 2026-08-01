# Changelog — auto-linker & CNLM

История изменений auto-linker (`link-enricher.ts`) и CNLM-матрицы (`schema.ts`).

---

## [1.1.0] — 2026-08-01

**ADR-015:** Оптимизация связей графа знаний — замена generic `references` на конкретные типы.

### Изменён auto-linker (link-enricher.ts)

**Пороги confidence:**

| Версия | Порог | Действие |
|--------|-------|----------|
| Было | ≥ 0.85 | Конкретный тип |
| Было | ≥ 0.75 | `references` (fallback) |
| Было | < 0.75 | Связь не ставится |
| **Стало** | ≥ 0.90 | Конкретный тип (высокая уверенность) |
| **Стало** | ≥ 0.85 | Конкретный тип (средняя уверенность) |
| **Стало** | ≥ 0.80 | `related_to` (fallback) |
| **Стало** | < 0.80 | Связь не ставится |

**Логика:**
- Поднят порог для fallback с 0.75 до 0.80
- Fallback заменён: `references` → `related_to` (менее generic)
- При confidence < 0.80 связь вообще не ставится (ранее ставилась `references`)

### Изменена CNLM-матрица (schema.ts)

**Запреты для кросс-namespace пар:**

| Пара namespace | Запрещённые fallback | Разрешённые замены |
|---|---|---|
| `project_meta → code_knowledge` | `references` | `implements_adr`, `solves`, `motivates` |
| `dialogue_insights → code_knowledge` | `references` | `solves`, `derived_from`, `motivates` |
| `user_facts → code_knowledge` | `references` | `motivates`, `derived_from` |
| `project_meta → dialogue_insights` | — | `informed_by` |
| `dialogue_insights → project_meta` | — | `motivates`, `informed_by` |
| `user_facts → project_meta` | — | `motivates`, `derived_from` |

**Разрешено:**
- `code_knowledge → code_knowledge`: все типы (включая `references`)
- `references` — только для упоминаний без семантики

### Обновлён промпт Тиши (granulate-tool.ts)

**Новый приоритет типов:**
```
depends_on > solves > implements_adr > motivates > related_to > references
```

**Правила:**
- ADR → модуль: `implements_adr`
- Инсайт → проблема: `solves`
- Факт → решение: `motivates`
- Инсайт → ADR: `informed_by`
- Нет семантики: `related_to` (не `references`)
- `references` — только когда реально нет другого варианта

### Ожидаемые метрики

| Метрика | До | После |
|---|---|---|
| `references` | 47.7% (3 049) | < 20% (~600) |
| `implements_adr` | 5.9% (384) | ~12% (~800) |
| `solves` | 5.7% (369) | ~10% (~650) |
| `motivates` | 0.4% (27) | ~3% (~200) |

---

## [1.0.0] — 2026-07-25

**Начальное состояние:** auto-linker и CNLM матрица введены в эксплуатацию.

### Auto-linker (link-enricher.ts)

- Порог confidence для fallback: 0.75
- Fallback: `references`
- Max 5 связей на гранулу
- Только cross-namespace связи

### CNLM-матрица (schema.ts)

- 5 namespace: `user_facts`, `dialogue_insights`, `project_meta`, `code_knowledge`, `infrastructure`
- `code_knowledge → code_knowledge`: `*` (все типы)
- Non-blocking проверка: `console.warn()` при нарушении

---

## Связи

- **ADR-015:** Оптимизация связей графа знаний (accepted)
- **ADR-011:** Superseded (черновик анализа)
- **ADR-010:** Новые cross-namespace LinkType
- **ADR-013:** Graph health tool
