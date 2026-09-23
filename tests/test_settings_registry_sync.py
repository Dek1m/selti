"""Сверка трёх реестров настроек: settings_store.REGISTRY (код) vs сидинг
миграции 027_app_settings.sql (БД) vs docs/SETTINGS_REGISTRY.md (документ).

Ф4-приёмка 2026-09-23 (Катерина). Тест постоянный: любая будущая правка
одного источника без двух других — красный прогон с точным diff.

Сравниваются: множество ключей (97), типы, дефолты, min/max, enum,
is_dangerous, requires_restart, группы, виджеты, русские title/description.

Нормализации при сравнении (задокументированные допущения):
- SQL-дефолты schedule.* содержат явный "day_of_week": null — store
  хранит дефолт без ключа; None-значения вычищаются перед сравнением
  (семантика crontab-конструктора: null == отсутствие == "*").
- Числа сравниваются числовым равенством (jsonb '0' vs float 0.0).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from memory_server.config import settings
from memory_server.settings_store import GROUPS, REGISTRY, get_default

_ROOT = Path(__file__).resolve().parent.parent
SQL_PATH = _ROOT / "migrations" / "027_app_settings.sql"
MD_PATH = _ROOT / "docs" / "SETTINGS_REGISTRY.md"


# ════════════════════════ SQL-парсер сидинга 027 ════════════════════════


def _tokenize_seed(sql_text: str) -> list[list]:
    """Токенайзер VALUES-блока: записи → поля ('str' | None | True/False).

    Посимвольный сканер: записи — (...) верхнего уровня, внутри — поля,
    разделённые запятыми верхнего уровня записи. Строки в '...' с ''-эскейпом.
    """
    insert_at = sql_text.index("INSERT INTO app_settings")
    start = sql_text.index("VALUES", insert_at) + len("VALUES")
    end = sql_text.index("ON CONFLICT (key) DO NOTHING", start)
    body = sql_text[start:end]

    rows: list[list] = []
    cur_row: list | None = None
    cur_field: list[str] = []
    fields: list = []
    depth = 0
    in_str = False
    i = 0
    while i < len(body):
        ch = body[i]
        if not in_str and ch == "-" and body[i : i + 2] == "--":
            # SQL-комментарий до конца строки (в сидинге есть §-заголовки со скобками)
            while i < len(body) and body[i] != "\n":
                i += 1
            continue
        if in_str:
            if ch == "'":
                if i + 1 < len(body) and body[i + 1] == "'":
                    cur_field.append("'")
                    i += 2
                    continue
                in_str = False
                fields.append(("str", "".join(cur_field)))
                cur_field = []
            else:
                cur_field.append(ch)
        elif ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
            if depth == 1:
                cur_row = []
                fields = []
        elif ch == ")":
            if depth == 1:
                if cur_field:
                    fields.append(("raw", "".join(cur_field).strip()))
                    cur_field = []
                cur_row = [_field_value(kind, val) for kind, val in fields]
                rows.append(cur_row)
            depth -= 1
        elif ch == "," and depth == 1:
            if cur_field:
                fields.append(("raw", "".join(cur_field).strip()))
                cur_field = []
        elif depth == 1 and not ch.isspace():
            cur_field.append(ch)
        elif depth >= 2 and not ch.isspace():
            # вложенные скобки json-enum: пропускаем, строкой придёт целиком
            cur_field.append(ch)
        i += 1
    return rows


def _field_value(kind: str, raw: str):
    if kind == "str":
        return raw
    if raw == "NULL":
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise AssertionError(f"unexpected SQL literal: {raw!r}")


def _load_sql_seed() -> dict[str, dict]:
    """97 строк сидинга → {key: {value_type, group, title, desc, default,
    min, max, enum, dangerous, restart}}."""
    rows = _tokenize_seed(SQL_PATH.read_text(encoding="utf-8"))
    assert rows, "сидинг 027 не распознан"
    seed: dict[str, dict] = {}
    for row in rows:
        assert len(row) == 12, f"в строке сидинга {len(row)} полей: {row[:2]}"
        key, _value, vtype, group, title, desc, default, mn, mx, enum, dangerous, restart = row
        assert key not in seed, f"дубль ключа в сидинге: {key}"
        seed[key] = {
            "value_type": vtype,
            "group": group,
            "title": title,
            "description": desc,
            "default": json.loads(default),
            "min": json.loads(mn) if mn is not None else None,
            "max": json.loads(mx) if mx is not None else None,
            "enum": json.loads(enum) if enum is not None else None,
            "dangerous": dangerous,
            "restart": restart,
        }
    return seed


# ════════════════════════ Markdown-парсер реестра ════════════════════════

_MD_GROUP_BY_SECTION = {
    "2.1": "search", "2.2": "dedup", "2.3": "lifecycle", "2.4": "cluster",
    "2.5": "linker", "2.6": "edge", "2.7": "cloud", "2.8": "map",
    "2.9": "celery", "2.10": "schedule", "2.11": "api_caps",
}


def _split_md_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _parse_md_default(raw: str, key: str) -> object | None:
    """Дефолт из ячейки md → python-значение; None = маркер 'см. блок ниже'."""
    raw = raw.strip()
    if raw.startswith("`") and raw.endswith("`"):
        raw = raw[1:-1]
    if raw in ("см. §2.1.1", "см. ниже"):
        return "DICT_REF"
    if raw == '""':
        return ""
    if raw == "true":
        return True
    if raw == "false":
        return False
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if re.fullmatch(r"-?\d+\.\d+", raw):
        return float(raw)
    if raw.startswith("[") and raw.endswith("]"):
        return json.loads(raw)
    # §2.10: interval Ns / cron HH:MM / cron sun HH:MM
    m = re.fullmatch(r"interval (\d+)s", raw)
    if m:
        return {"type": "interval", "seconds": int(m.group(1))}
    m = re.fullmatch(r"cron (\d{2}):(\d{2})", raw)
    if m:
        return {"type": "crontab", "hour": str(int(m.group(1))), "minute": str(int(m.group(2)))}
    m = re.fullmatch(r"cron (\w+) (\d{2}):(\d{2})", raw)
    if m:
        return {
            "type": "crontab", "day_of_week": m.group(1),
            "hour": str(int(m.group(2))), "minute": str(int(m.group(3))),
        }
    return raw  # строковый дефолт (glm-4.7-flash, disabled)


def _parse_md_constraints(raw: str) -> tuple:
    """Ячейка «Ограничения» → (min, max, enum)."""
    raw = raw.strip()
    if raw == "—" or raw == "":
        return None, None, None
    enum_backticks = re.findall(r"`([^`]+)`", raw)
    if raw.startswith("enum"):
        return None, None, tuple(enum_backticks)
    m = re.search(r"(\d+(?:\.\d+)?)\s*\.\.\s*(\d+(?:\.\d+)?)", raw)
    if m:
        return float(m.group(1)), float(m.group(2)), None
    return None, None, None


def _load_md_registry() -> dict[str, dict]:
    lines = MD_PATH.read_text(encoding="utf-8").splitlines()
    entries: dict[str, dict] = {}
    section = None
    for line in lines:
        m = re.match(r"### (2\.\d+)", line)
        if m:
            section = m.group(1)
            continue
        if not line.startswith("|") or not section:
            continue
        cells = _split_md_row(line)
        if len(cells) < 4 or set(cells[0]) <= {"-", " ", ":"}:
            continue  # шапка/разделитель
        key = cells[0].strip("`")
        if not re.fullmatch(r"[a-z_][a-z0-9_.]*", key):
            continue  # строка заголовков («Ключ» и т.п.)
        group = _MD_GROUP_BY_SECTION[section]
        if group == "schedule":
            # | Ключ | Дефолт | Название | Описание | Задача |
            default, title, desc = _parse_md_default(cells[1], key), cells[2], cells[3]
            entries[key] = {
                "group": group, "value_type": "json",
                "default": default, "title": title, "description": desc,
                "min": None, "max": None, "enum": None,
                "dangerous": False, "restart": True, "widget": "text",
            }
            continue
        # | Ключ | Тип | Дефолт | Название | Описание | Ограничения | Опасн | Рестарт | Виджет | Потр. |
        assert len(cells) == 10, f"{key}: {len(cells)} колонок md-строки"
        mn, mx, enum = _parse_md_constraints(cells[5])
        entries[key] = {
            "group": group,
            "value_type": cells[1],
            "default": _parse_md_default(cells[2], key),
            "title": cells[3],
            "description": cells[4],
            "min": mn, "max": mx, "enum": enum,
            "constraint_raw": cells[5],
            "dangerous": cells[6].startswith("да"),
            "restart": cells[7].startswith("да"),
            "widget": cells[8],
        }
    # §2.1.1: json-блок дефолтов dict-ключей (многострочные значения —
    # собираем по балансу фигурных скобок)
    md_text = "\n".join(lines)
    block = re.search(r"```json\n(.*?)```", md_text, re.S)
    assert block, "§2.1.1 json-блок не найден"
    block_text = block.group(1)
    for m in re.finditer(r"^(\w+):\s+", block_text, re.M):
        name = m.group(1)
        depth = 0
        for j in range(m.end(), len(block_text)):
            if block_text[j] == "{":
                depth += 1
            elif block_text[j] == "}":
                depth -= 1
                if depth == 0:
                    entries[name]["default"] = json.loads(
                        block_text[m.end():j + 1]
                    )
                    break
    # §2.2: inline-дефолт dedup_thresholds — следующий backtick-блок после
    # маркера (многострочный, с переносами внутри)
    m = re.search(r"Дефолт `dedup_thresholds`[^`]*`([^`]+)`", md_text)
    if m:
        entries["dedup_thresholds"]["default"] = json.loads(
            re.sub(r"\s+", " ", m.group(1))
        )
    return entries


# ════════════════════════ Нормализация значений ════════════════════════


def _norm(value):
    """Сравнимая форма: числовое равенство int/float; None-ключи dict вычищаются."""
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items()) if v is not None}
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return [_norm(v) for v in value]
    return value


def _store_row(key: str) -> dict:
    spec = REGISTRY[key]
    return {
        "value_type": spec.value_type,
        "group": spec.group,
        "default": get_default(key),
        "min": spec.min_value,
        "max": spec.max_value,
        "enum": list(spec.enum_values) if spec.enum_values else None,
        "dangerous": spec.dangerous,
        "restart": spec.requires_restart,
        "widget": spec.widget,
    }


# ════════════════════════ Сверка ════════════════════════


@pytest.fixture(scope="module")
def sql_seed() -> dict[str, dict]:
    return _load_sql_seed()


@pytest.fixture(scope="module")
def md_registry() -> dict[str, dict]:
    return _load_md_registry()


class TestThreeWaySync:
    def test_97_keys_in_all_three_sources(self, sql_seed, md_registry):
        store_keys = set(REGISTRY)
        assert len(store_keys) == 97
        assert set(sql_seed) == store_keys, (
            f"только в SQL: {sorted(set(sql_seed) - store_keys)}; "
            f"только в store: {sorted(store_keys - set(sql_seed))}"
        )
        assert set(md_registry) == store_keys, (
            f"только в md: {sorted(set(md_registry) - store_keys)}; "
            f"только в store: {sorted(store_keys - set(md_registry))}"
        )

    def test_value_types_match(self, sql_seed, md_registry):
        for key, spec in REGISTRY.items():
            assert sql_seed[key]["value_type"] == spec.value_type, key
            if not key.startswith("schedule."):
                assert md_registry[key]["value_type"] == spec.value_type, key

    def test_groups_match(self, sql_seed, md_registry):
        for key, spec in REGISTRY.items():
            assert sql_seed[key]["group"] == spec.group, key
            assert md_registry[key]["group"] == spec.group, key
        assert set(sql_seed[k]["group"] for k in REGISTRY) <= set(GROUPS)

    def test_defaults_store_vs_sql(self, sql_seed):
        for key in REGISTRY:
            assert _norm(sql_seed[key]["default"]) == _norm(get_default(key)), (
                f"{key}: SQL {sql_seed[key]['default']!r} != store {get_default(key)!r}"
            )

    def test_defaults_store_vs_md(self, md_registry):
        for key in REGISTRY:
            md_default = md_registry[key]["default"]
            assert md_default != "DICT_REF", f"{key}: дефолт-ссылка не разрешена"
            assert _norm(md_default) == _norm(get_default(key)), (
                f"{key}: md {md_default!r} != store {get_default(key)!r}"
            )

    def test_min_max_match(self, sql_seed, md_registry):
        for key, spec in REGISTRY.items():
            assert _norm(sql_seed[key]["min"]) == _norm(spec.min_value), key
            assert _norm(sql_seed[key]["max"]) == _norm(spec.max_value), key
            if spec.value_type not in ("int", "float"):
                # min/max для json/str-ключей живут в подсхемах (см.
                # test_json_subschemas_match_md), а не в колонках — в md
                # ячейка «Ограничения» описывает подсхему
                assert spec.min_value is None and spec.max_value is None, key
                continue
            md_min, md_max = md_registry[key]["min"], md_registry[key]["max"]
            assert _norm(md_min) == _norm(spec.min_value), f"{key}: md min {md_min} != {spec.min_value}"
            assert _norm(md_max) == _norm(spec.max_value), f"{key}: md max {md_max} != {spec.max_value}"

    def test_json_subschemas_match_md(self, md_registry):
        """Подсхемы json-ключей: диапазоны ns-dict и спец-правила str-ключей
        из md «Ограничения» == код валидации."""
        from memory_server.settings_store import (
            SYMMETRIC_LINK_TYPES,
            _NAMESPACE_DICT_SPECS,
        )
        constraints = {k: md_registry[k].get("constraint_raw", "") for k in md_registry}
        for key, (lo, hi) in _NAMESPACE_DICT_SPECS.items():
            assert f"float {lo}..{hi}" in constraints[key], key
            assert "обязателен `default`" in constraints[key], key
        assert "только симметричные типы" in constraints["traverse_symmetric_link_types"]
        assert "URL или пусто" in constraints["linker_llm_base_url"]
        assert "длина 1..100" in constraints["linker_llm_model"]
        assert set(SYMMETRIC_LINK_TYPES) == {"related_to", "alternative_to", "connected_to"}

    def test_enum_match(self, sql_seed, md_registry):
        enum_keys = [k for k, s in REGISTRY.items() if s.enum_values]
        assert enum_keys == ["gc_mode"]
        for key in enum_keys:
            assert sql_seed[key]["enum"] == list(REGISTRY[key].enum_values), key
            assert tuple(md_registry[key]["enum"]) == tuple(REGISTRY[key].enum_values), key

    def test_dangerous_match(self, sql_seed, md_registry):
        for key, spec in REGISTRY.items():
            assert sql_seed[key]["dangerous"] is spec.dangerous, key
            assert md_registry[key]["dangerous"] is spec.dangerous, key

    def test_requires_restart_match(self, sql_seed, md_registry):
        for key, spec in REGISTRY.items():
            assert sql_seed[key]["restart"] is spec.requires_restart, (
                f"{key}: SQL restart={sql_seed[key]['restart']} != store {spec.requires_restart}"
            )
            assert md_registry[key]["restart"] is spec.requires_restart, key

    def test_celery_worker_concurrency_live_everywhere(self, sql_seed, md_registry):
        """Правка Афины после репетиции Норы: налету (broadcast) — рестарт НЕ нужен."""
        assert REGISTRY["celery_worker_concurrency"].requires_restart is False
        assert sql_seed["celery_worker_concurrency"]["restart"] is False
        assert md_registry["celery_worker_concurrency"]["restart"] is False

    def test_russian_titles_match(self, sql_seed, md_registry):
        """Три источника: сидинг 027 == реестр md == SettingSpec (код).
        Тексты в коде — решение Мастера по компромиссу Ф4 (вариант «б»):
        upsert пересоздаёт строки reset→PUT с полными подписями."""
        for key, spec in REGISTRY.items():
            assert spec.title_ru, f"{key}: пустой title_ru в SettingSpec"
            assert sql_seed[key]["title"] == md_registry[key]["title"] == spec.title_ru, key

    def test_russian_descriptions_match(self, sql_seed, md_registry):
        for key, spec in REGISTRY.items():
            assert spec.description_ru, f"{key}: пустой description_ru в SettingSpec"
            assert (
                sql_seed[key]["description"] == md_registry[key]["description"] == spec.description_ru
            ), key

    def test_widget_match(self, md_registry):
        for key, spec in REGISTRY.items():
            assert md_registry[key]["widget"] == spec.widget, key

    def test_store_defaults_equal_config_fields(self):
        """Дефолт store = config.py для всех несхедульных ключей (§6.4 реестра)."""
        for key, spec in REGISTRY.items():
            if key.startswith("schedule."):
                assert spec.default is not None, key
                continue
            assert getattr(settings, key) == get_default(key), key

    def test_md_summary_counts(self):
        """Сводка §7 реестра: 97 / 8 dangerous / 25 restart (26 − concurrency,
        переведённый на налету-применение broadcast'ом)."""
        assert len(REGISTRY) == 97
        assert sum(1 for s in REGISTRY.values() if s.dangerous) == 8
        assert sum(1 for s in REGISTRY.values() if s.requires_restart) == 25
