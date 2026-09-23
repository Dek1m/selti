import { useState } from "react";
import type { SettingsProfile } from "../api/types";

interface ProfileSelectorProps {
  profiles: SettingsProfile[];
  busy?: boolean;
  /** Экран обрабатывает 409 (dangerous в профиле) → confirm-модалка */
  onApply: (id: string) => void;
  onUpdateSnapshot: (id: string) => void;
  onDelete: (id: string) => void;
  onCreate: (name: string, description: string) => Promise<void>;
}

/** Профили конфигурации: builtin «Заводские настройки» + пользовательские.
 * Для кастомных доступны обновление снапшота и удаление; создание —
 * через модалку с именем и необязательным описанием. */
export function ProfileSelector({ profiles, busy, onApply, onUpdateSnapshot, onDelete, onCreate }: ProfileSelectorProps) {
  const builtinFirst = [...profiles].sort((a, b) => Number(b.is_builtin) - Number(a.is_builtin));
  const [selectedId, setSelectedId] = useState(builtinFirst[0]?.id ?? "");
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [savingProfile, setSavingProfile] = useState(false);

  const active = profiles.find((p) => p.id === selectedId);

  const submitCreate = async () => {
    if (!name.trim() || savingProfile) return;
    setSavingProfile(true);
    setCreateError(null);
    try {
      await onCreate(name, desc);
      setCreating(false);
      setName("");
      setDesc("");
    } catch (e) {
      setCreateError((e as Error).message);
    } finally {
      setSavingProfile(false);
    }
  };

  return (
    <div className="profile-bar" role="group" aria-label="Профили конфигурации">
      <i className="bi bi-collection-play" aria-hidden="true" />
      <select
        className="set-select"
        aria-label="Профиль настроек"
        value={selectedId}
        onChange={(e) => setSelectedId(e.target.value)}
      >
        {builtinFirst.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
            {p.is_builtin ? "" : " ★"}
          </option>
        ))}
      </select>
      <button type="button" className="btn slim primary" disabled={busy || !active} onClick={() => onApply(selectedId)}>
        <i className="bi bi-play-fill" aria-hidden="true" /> Применить
      </button>
      {active && !active.is_builtin && (
        <>
          <button
            type="button"
            className="btn slim"
            title="Перезаписать снапшот профиля текущими значениями"
            disabled={busy}
            onClick={() => onUpdateSnapshot(selectedId)}
          >
            <i className="bi bi-cloud-arrow-up" aria-hidden="true" /> Обновить снапшот
          </button>
          <button
            type="button"
            className="btn slim danger-ghost"
            disabled={busy}
            onClick={() => onDelete(selectedId)}
          >
            <i className="bi bi-trash3" aria-hidden="true" /> Удалить
          </button>
        </>
      )}
      <button type="button" className="btn slim" disabled={busy} onClick={() => setCreating(true)}>
        <i className="bi bi-save" aria-hidden="true" /> Сохранить текущее как профиль
      </button>

      {creating && (
        <div className="modal-overlay" onClick={() => setCreating(false)}>
          <div
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="profile-create-title"
            onClick={(e) => e.stopPropagation()}
          >
            <h3 id="profile-create-title" className="modal-title">
              <i className="bi bi-save" aria-hidden="true" /> Новый профиль
            </h3>
            <p className="modal-body">В профиль попадут текущие effective-значения всех настроек.</p>
            <div className="profile-form">
              <label className="profile-field">
                <span>Имя профиля</span>
                <input
                  className="set-input wide"
                  type="text"
                  value={name}
                  autoFocus
                  placeholder="Например: «Агрессивный GC»"
                  onChange={(e) => setName(e.target.value)}
                />
              </label>
              <label className="profile-field">
                <span>Описание (необязательно)</span>
                <input
                  className="set-input wide"
                  type="text"
                  value={desc}
                  placeholder="Зачем этот профиль"
                  onChange={(e) => setDesc(e.target.value)}
                />
              </label>
            </div>
            {createError !== null && (
              <p className="setting-error" role="alert">
                {createError}
              </p>
            )}
            <div className="modal-actions">
              <button type="button" className="btn" onClick={() => setCreating(false)} disabled={savingProfile}>
                Отмена
              </button>
              <button
                type="button"
                className="btn primary"
                disabled={!name.trim() || savingProfile}
                onClick={() => void submitCreate()}
              >
                {savingProfile ? "Сохраняю…" : "Сохранить профиль"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
