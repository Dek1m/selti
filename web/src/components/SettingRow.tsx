import { useEffect, useState } from "react";
import type { ApiError } from "../api/client";
import { formatValue, parseSettingValue, sliderStep, validateValue, valuesEqual, widgetKind } from "../api/settings";
import type { SettingMeta } from "../api/types";
import { InfoTooltip } from "./InfoTooltip";

/** Контекст для общей danger-модалки экрана (одна на экран). */
export interface ConfirmRequest {
  title: string;
  body: string;
  details?: string[];
  /** Своя подпись кнопки подтверждения (напр. «Сбросить всё») */
  confirmLabel?: string;
  action: () => void;
}

interface SettingRowProps {
  meta: SettingMeta;
  onSave: (key: string, value: unknown, confirm: boolean) => Promise<void>;
  /** confirm — для опасных настроек: reset тоже требует подтверждения */
  onReset: (key: string, confirm: boolean) => Promise<void>;
  onRequestConfirm: (ctx: ConfirmRequest) => void;
}

/** Отдельные человеческие тексты для особо опасных переключений. */
const DANGER_TEXTS: Record<string, { title: string; body: string }> = {
  gc_purge_enabled: {
    title: "Открыть физическое удаление знаний?",
    body: "Мастер-кран откроет hard delete устаревших версий: сборщик мусора начнёт безвозвратно удалять знания из базы и векторного хранилища — не только помечать их. Включать только осознанно после бэкапа; восстановление — из снапшота.",
  },
};

const ENV_NOTE = "Управляется через .env / docker-compose";
const ENV_TOOLTIP = "Чтобы править здесь — удалите переменную из compose/.env и перезапустите контейнеры.";

export function SettingRow({ meta, onSave, onReset, onRequestConfirm }: SettingRowProps) {
  const kind = widgetKind(meta);
  const locked = meta.is_env_locked;
  // Бэк может отдать пустой title_ru (чинится, но UI переживает) — фолбэк на key
  const title = meta.title_ru.trim() || meta.key;

  const [draft, setDraft] = useState<unknown>(meta.value);
  const [rawDraft, setRawDraft] = useState<string>(() => toRaw(meta));
  const [parseError, setParseError] = useState<string | null>(null);
  const [serverError, setServerError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // Пришедшее с сервера значение — истина: сбрасываем черновик и ошибки
  useEffect(() => {
    setDraft(meta.value);
    setRawDraft(toRaw(meta));
    setParseError(null);
    setServerError(null);
  }, [meta.value]);

  const dirty = !valuesEqual(draft, meta.value);
  const invalid = parseError !== null || validateValue(meta, draft) !== null;
  const busy = saving;

  /* ── Изменение черновика ── */

  const setFromRaw = (raw: string) => {
    setRawDraft(raw);
    const parsed = parseSettingValue(meta, raw);
    if (parsed.ok) {
      setDraft(parsed.value);
      setParseError(null);
    } else {
      setParseError(parsed.error);
    }
  };

  const toggleListItem = (item: string) => {
    const list = Array.isArray(draft) ? [...draft] : [];
    const i = list.indexOf(item);
    if (i >= 0) list.splice(i, 1);
    else list.push(item);
    setDraft(list);
  };

  const setDictEntry = (k: string, raw: string) => {
    const d = { ...((draft as Record<string, unknown>) ?? {}) };
    const n = Number(raw);
    if (raw.trim() === "" || Number.isNaN(n)) return; // не валидно — не пишем
    d[k] = n;
    setDraft(d);
  };

  const renameDictKey = (oldKey: string, next: string) => {
    const d = { ...((draft as Record<string, unknown>) ?? {}) };
    const v = d[oldKey];
    delete d[oldKey];
    d[next] = v;
    setDraft(d);
  };

  const removeDictKey = (k: string) => {
    const d = { ...((draft as Record<string, unknown>) ?? {}) };
    delete d[k];
    setDraft(d);
  };

  const addDictRow = () => {
    const d = { ...((draft as Record<string, unknown>) ?? {}) };
    let name = "namespace";
    let i = 2;
    while (name in d) name = `namespace_${i++}`;
    d[name] = 0.9;
    setDraft(d);
  };

  /* ── Сохранение / сброс ── */

  const doSave = async (confirm: boolean) => {
    setSaving(true);
    setServerError(null);
    try {
      await onSave(meta.key, draft, confirm);
      // Успех: строка обновится через react-query invalidate
    } catch (e) {
      setServerError((e as ApiError).message);
    } finally {
      setSaving(false);
    }
  };

  const requestSave = () => {
    if (!dirty || invalid || busy) return;
    if (meta.is_dangerous) {
      const text = DANGER_TEXTS[meta.key];
      onRequestConfirm({
        title: text?.title ?? "Опасное изменение настройки",
        body:
          text?.body ??
          `«${title}» влияет на целостность данных. Применить новое значение: ${formatValue(draft)}?`,
        action: () => void doSave(true),
      });
      return;
    }
    void doSave(false);
  };

  /** Сброс к дефолту; опасные настройки — через confirm (reset тоже 409-ится). */
  const requestReset = () => {
    if (meta.is_dangerous) {
      onRequestConfirm({
        title: "Опасный сброс настройки",
        body: `«${title}» вернётся к значению по умолчанию (${formatValue(meta.default_value)}). Сброс опасной настройки требует подтверждения.`,
        confirmLabel: "Сбрасываю",
        action: () => {
          setSaving(true);
          onReset(meta.key, true)
            .catch((e) => setServerError((e as ApiError).message))
            .finally(() => setSaving(false));
        },
      });
      return;
    }
    void onReset(meta.key, false);
  };

  /* ── Виджеты ── */

  const widget = () => {
    switch (kind) {
      case "switch":
        return (
          <label className="set-switch">
            <input
              type="checkbox"
              disabled={locked || busy}
              checked={draft === true}
              onChange={(e) => setDraft(e.target.checked)}
            />
            <span className="set-switch-track" aria-hidden="true">
              <span className="set-switch-thumb" />
            </span>
            <span className="set-switch-text">{draft === true ? "вкл" : "выкл"}</span>
          </label>
        );

      case "slider":
        return (
          <div className="set-slider">
            {meta.min_value !== undefined && <span className="set-range-edge">{meta.min_value}</span>}
            <input
              type="range"
              min={meta.min_value}
              max={meta.max_value}
              step={sliderStep(meta)}
              disabled={locked || busy}
              value={typeof draft === "number" ? draft : meta.min_value ?? 0}
              onChange={(e) => setFromRaw(e.target.value)}
            />
            {meta.max_value !== undefined && <span className="set-range-edge">{meta.max_value}</span>}
            <input
              className="set-input num"
              type="text"
              inputMode={meta.value_type === "int" ? "numeric" : "decimal"}
              disabled={locked || busy}
              value={rawDraft}
              onChange={(e) => setFromRaw(e.target.value)}
              aria-invalid={parseError !== null}
            />
          </div>
        );

      case "number":
        return (
          <input
            className="set-input num"
            type="text"
            inputMode="numeric"
            disabled={locked || busy}
            value={rawDraft}
            onChange={(e) => setFromRaw(e.target.value)}
            aria-invalid={parseError !== null}
          />
        );

      case "radio":
        return (
          <div className="set-radio-group" role="radiogroup" aria-label={title}>
            {(meta.enum_values ?? []).map((v) => (
              <label key={v} className="set-radio">
                <input
                  type="radio"
                  name={`radio-${meta.key}`}
                  disabled={locked || busy}
                  checked={draft === v}
                  onChange={() => setDraft(v)}
                />
                <span className="set-radio-dot" aria-hidden="true" />
                <span className="mono">{v}</span>
              </label>
            ))}
          </div>
        );

      case "select":
        return (
          <select
            className="set-select"
            disabled={locked || busy}
            value={String(draft ?? "")}
            onChange={(e) => setDraft(e.target.value)}
          >
            {(meta.enum_values ?? []).map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        );

      case "checkboxes": {
        const options = meta.enum_values ?? unionWithOptions(meta);
        const current = Array.isArray(draft) ? (draft as string[]) : [];
        return (
          <div className="set-checks">
            {options.map((v) => (
              <label key={v} className="set-check">
                <input
                  type="checkbox"
                  disabled={locked || busy}
                  checked={current.includes(v)}
                  onChange={() => toggleListItem(v)}
                />
                <span className="mono">{v}</span>
              </label>
            ))}
          </div>
        );
      }

      case "dict": {
        const entries = Object.entries((draft as Record<string, unknown>) ?? {});
        return (
          <div className="set-dict">
            {entries.map(([k, v]) => (
              <div key={k} className="set-dict-row">
                <input
                  className="set-input key mono"
                  type="text"
                  disabled={locked || busy}
                  value={k}
                  aria-label="Ключ (namespace)"
                  onChange={(e) => renameDictKey(k, e.target.value)}
                />
                <input
                  className="set-input num"
                  type="number"
                  step="0.001"
                  disabled={locked || busy}
                  value={typeof v === "number" ? v : ""}
                  aria-label="Значение"
                  onChange={(e) => setDictEntry(k, e.target.value)}
                />
                <button
                  type="button"
                  className="icon-btn"
                  disabled={locked || busy}
                  aria-label={`Удалить строку ${k}`}
                  onClick={() => removeDictKey(k)}
                >
                  <i className="bi bi-x-lg" aria-hidden="true" />
                </button>
              </div>
            ))}
            <button type="button" className="btn slim" disabled={locked || busy} onClick={addDictRow}>
              <i className="bi bi-plus-lg" aria-hidden="true" /> Добавить namespace
            </button>
          </div>
        );
      }

      case "json_text":
        return (
          <textarea
            className="set-textarea mono"
            rows={3}
            spellCheck={false}
            disabled={locked || busy}
            value={rawDraft}
            onChange={(e) => setFromRaw(e.target.value)}
            aria-invalid={parseError !== null}
            aria-label={title}
          />
        );

      case "text":
      default:
        return (
          <input
            className="set-input wide"
            type="text"
            disabled={locked || busy}
            value={typeof draft === "string" ? draft : ""}
            onChange={(e) => setDraft(e.target.value)}
          />
        );
    }
  };

  /* ── Тултип: описание + env-приписка + БД-значение + автор правки ── */
  const tooltipParts = [
    meta.description_ru.trim(),
    locked ? ENV_TOOLTIP : "",
    locked && meta.db_value !== null && meta.db_value !== undefined
      ? `Сохранено в БД: ${formatValue(meta.db_value)} — перекрыто переменной окружения.`
      : "",
    meta.updated_at
      ? `Обновлено: ${new Date(meta.updated_at).toLocaleString("ru-RU")}${meta.updated_by ? ` · ${meta.updated_by}` : ""}`
      : "",
  ].filter(Boolean);
  const tooltip = tooltipParts.join("\n\n");

  return (
    <article className={`setting-row${locked ? " locked" : ""}${dirty ? " dirty" : ""}`}>
      <div className="setting-head">
        <div className="setting-name">
          <span className="setting-title">{title}</span>
          <InfoTooltip text={tooltip} label={title} />
          <span className="setting-key mono">{meta.key}</span>
        </div>
        <div className="setting-badges">
          {locked && (
            <span className="badge env" title={`${ENV_NOTE}. ${ENV_TOOLTIP}`}>
              <i className="bi bi-lock-fill" aria-hidden="true" /> env
            </span>
          )}
          {meta.is_dangerous && (
            <span className="badge danger">
              <i className="bi bi-exclamation-triangle" aria-hidden="true" /> опасно
            </span>
          )}
          {meta.requires_restart && (
            <span className="badge restart">
              <i className="bi bi-arrow-repeat" aria-hidden="true" /> нужен рестарт
            </span>
          )}
          {meta.differs_from_default && !locked && (
            <span className="badge modified">
              <i className="bi bi-pencil-fill" aria-hidden="true" /> изменено
            </span>
          )}
        </div>
      </div>

      <div className="setting-body">
        <div className="setting-widget">{widget()}</div>
        {!locked && (
          <div className="setting-actions">
            {meta.differs_from_default && (
              <button
                type="button"
                className="icon-btn"
                disabled={busy}
                title={`Сбросить к значению по умолчанию (${formatValue(meta.default_value)})`}
                aria-label={`Сбросить «${title}» к значению по умолчанию`}
                onClick={requestReset}
              >
                <i className="bi bi-arrow-counterclockwise" aria-hidden="true" />
              </button>
            )}
            {dirty && (
              <button type="button" className="btn slim primary" disabled={invalid || busy} onClick={requestSave}>
                {busy ? "Сохраняю…" : "Сохранить"}
              </button>
            )}
          </div>
        )}
      </div>

      {parseError !== null && <p className="setting-error">{parseError}</p>}
      {!locked && parseError === null && validateValue(meta, draft) !== null && (
        <p className="setting-error">{validateValue(meta, draft)}</p>
      )}
      {serverError !== null && (
        <div className="setting-server-error" role="alert">
          <i className="bi bi-slash-circle" aria-hidden="true" /> {serverError}
        </div>
      )}
    </article>
  );
}

/** Строковое представление значения для текстовых инпутов. */
function toRaw(meta: SettingMeta): string {
  const v = meta.value;
  if (meta.value_type === "json") return JSON.stringify(v, null, 2);
  if (typeof v === "number") return String(v);
  if (typeof v === "string") return v;
  return "";
}

/** Опции чекбоксов без enum_values: объединение текущего и дефолтного. */
function unionWithOptions(meta: SettingMeta): string[] {
  const set = new Set<string>();
  for (const src of [meta.value, meta.default_value]) {
    if (Array.isArray(src)) for (const item of src) set.add(String(item));
  }
  return [...set];
}
