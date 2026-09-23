import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { ApiError } from "../api/client";
import { applyProfile, createProfile, deleteProfile, getProfiles, getSettings, groupSettings, resetAllSettings, resetSetting, updateProfile, updateSetting } from "../api/settings";
import type { SettingsPayload } from "../api/types";
import { ConfirmDangerModal } from "../components/ConfirmDangerModal";
import { ProfileSelector } from "../components/ProfileSelector";
import { SettingRow, type ConfirmRequest } from "../components/SettingRow";

const COLLAPSED_KEY = "selti.settings.collapsed";

function loadCollapsed(): Set<string> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

/** /ui/settings — «Конфигурация»: каталог настроек по группам из API
 * (сидинг SETTINGS_REGISTRY.md), профили и опасные операции. */
export function SettingsScreen() {
  const queryClient = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: getSettings });
  const profiles = useQuery({ queryKey: ["settings-profiles"], queryFn: getProfiles, staleTime: 30_000 });

  const [collapsed, setCollapsed] = useState<Set<string>>(loadCollapsed);
  const [confirmState, setConfirmState] = useState<ConfirmRequest | null>(null);
  const [bannerError, setBannerError] = useState<string | null>(null);
  /** Тихое зелёное уведомление (профиль применён, env-ключи пропущены) */
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const payload: SettingsPayload | undefined = settings.data;
  const groups = payload ? groupSettings(payload) : [];
  const allCollapsed = groups.length > 0 && groups.every((g) => collapsed.has(g.group));

  /** Человекочитаемая метка ключа: «Название (key)». */
  const keyLabel = (key: string): string => {
    const meta = payload?.settings.find((s) => s.key === key);
    return meta ? `${meta.title_ru} (${key})` : key;
  };

  const persistCollapsed = (next: Set<string>) => {
    setCollapsed(next);
    try {
      localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...next]));
    } catch {
      // приватный режим — просто не запоминаем
    }
  };

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ["settings"] });
  };

  const showApplyResult = (res: { applied: string[]; skipped_env: string[] }) => {
    setNotice(
      res.skipped_env.length > 0
        ? `Профиль применён: ${res.applied.length} ключей. Пропущены env-ключи (${res.skipped_env.length}): ${res.skipped_env.join(", ")} — правятся только через .env/compose.`
        : `Профиль применён: ${res.applied.length} ключей.`,
    );
  };

  /* ── Строки ── */

  const handleSave = async (key: string, value: unknown, confirm: boolean) => {
    await updateSetting(key, value, confirm);
    invalidate();
  };

  const handleResetRow = async (key: string, confirm: boolean) => {
    setBannerError(null);
    try {
      await resetSetting(key, confirm);
      invalidate();
    } catch (e) {
      setBannerError((e as ApiError).message);
    }
  };

  /* ── Сброс всего ── */

  const requestResetAll = () =>
    setConfirmState({
      title: "Сбросить всё к заводским?",
      body: "Все изменённые настройки вернутся к значениям по умолчанию. Настройки, управляемые через .env / docker-compose, не тронем — они и так не отсюда правятся.",
      confirmLabel: "Сбросить всё",
      action: () => {
        setBusy(true);
        resetAllSettings()
          .then((res) => {
            invalidate();
            setNotice(`Сброшено к заводским: ${res.reset.length} ключей.`);
          })
          .catch((e) => setBannerError((e as ApiError).message))
          .finally(() => {
            setBusy(false);
            setConfirmState(null);
          });
      },
    });

  /* ── Профили ── */

  const applyProfileById = (id: string) => {
    setBannerError(null);
    setNotice(null);
    setBusy(true);
    applyProfile(id)
      .then((res) => {
        invalidate();
        showApplyResult(res);
      })
      .catch((e: ApiError) => {
        if (e.status === 409 && e.keys && e.keys.length > 0) {
          setConfirmState({
            title: "Профиль затрагивает опасные настройки",
            body: "Профиль — снапшот всех ключей, среди них есть опасные. Подтвердите применение со всеми изменениями.",
            details: e.keys.map(keyLabel),
            confirmLabel: "Применяю с опасными",
            action: () => {
              setBusy(true);
              applyProfile(id, true)
                .then((res) => {
                  invalidate();
                  showApplyResult(res);
                })
                .catch((err) => setBannerError((err as ApiError).message))
                .finally(() => {
                  setBusy(false);
                  setConfirmState(null);
                });
            },
          });
        } else {
          setBannerError(e.message);
        }
      })
      .finally(() => setBusy(false));
  };

  const updateSnapshotById = (id: string) => {
    setBannerError(null);
    setBusy(true);
    updateProfile(id)
      .then(() => void queryClient.invalidateQueries({ queryKey: ["settings-profiles"] }))
      .catch((e) => setBannerError((e as ApiError).message))
      .finally(() => setBusy(false));
  };

  const requestDeleteProfile = (id: string) => {
    const p = (profiles.data?.profiles ?? []).find((x) => x.id === id);
    setConfirmState({
      title: "Удалить профиль?",
      body: `Профиль «${p?.name ?? id}» и его снапшот будут удалены. Настройки не изменятся.`,
      confirmLabel: "Удалить профиль",
      action: () => {
        setBusy(true);
        deleteProfile(id)
          .then(() => void queryClient.invalidateQueries({ queryKey: ["settings-profiles"] }))
          .catch((e) => setBannerError((e as ApiError).message))
          .finally(() => {
            setBusy(false);
            setConfirmState(null);
          });
      },
    });
  };

  const handleCreateProfile = async (name: string, description: string) => {
    await createProfile(name, description);
    await queryClient.invalidateQueries({ queryKey: ["settings-profiles"] });
  };

  /* ── Состояния каталога ── */

  if (settings.isPending) {
    return (
      <div className="stage settings-stage">
        <h2 className="screen-title">Конфигурация</h2>
        <div aria-hidden="true">
          {Array.from({ length: 4 }, (_, i) => (
            <div key={i} className="sk settings-sk" style={{ height: 120 }} />
          ))}
        </div>
      </div>
    );
  }

  if (settings.isError) {
    return (
      <div className="stage settings-stage">
        <h2 className="screen-title">Конфигурация</h2>
        <div className="state-block error">
          <i className="bi bi-wifi-off" aria-hidden="true" />
          <h3>Каталог настроек недоступен</h3>
          <p>{(settings.error as Error).message}</p>
          <button className="btn" onClick={() => void settings.refetch()}>
            <i className="bi bi-arrow-clockwise" aria-hidden="true" /> Повторить
          </button>
        </div>
      </div>
    );
  }

  const changedCount = (payload?.settings ?? []).filter((s) => s.differs_from_default && !s.is_env_locked).length;

  return (
    <div className="stage settings-stage">
      <div className="settings-head">
        <div className="settings-heading">
          <h2 className="screen-title">Конфигурация</h2>
          <p className="settings-sub">
            {changedCount > 0
              ? `Изменено настроек: ${changedCount}. Изменения применяются сразу, кроме помеченных «нужен рестарт».`
              : "Всё по заводским значениям. Изменения применяются сразу, кроме помеченных «нужен рестарт»."}
          </p>
        </div>
        <div className="settings-toolbar">
          {!profiles.isPending && !profiles.isError && (
            <ProfileSelector
              profiles={profiles.data?.profiles ?? []}
              busy={busy}
              onApply={applyProfileById}
              onUpdateSnapshot={updateSnapshotById}
              onDelete={requestDeleteProfile}
              onCreate={handleCreateProfile}
            />
          )}
          <button type="button" className="btn slim" onClick={() => persistCollapsed(allCollapsed ? new Set() : new Set(groups.map((g) => g.group)))}>
            <i className={`bi ${allCollapsed ? "bi-arrows-expand" : "bi-arrows-collapse"}`} aria-hidden="true" />
            {allCollapsed ? "Развернуть все" : "Свернуть все"}
          </button>
          <button type="button" className="btn slim danger-ghost" disabled={busy} onClick={requestResetAll}>
            <i className="bi bi-arrow-counterclockwise" aria-hidden="true" /> Сбросить всё к заводским
          </button>
        </div>
      </div>

      {bannerError !== null && (
        <div className="setting-server-error banner" role="alert">
          <i className="bi bi-slash-circle" aria-hidden="true" /> {bannerError}
          <button type="button" className="icon-btn" aria-label="Закрыть ошибку" onClick={() => setBannerError(null)}>
            <i className="bi bi-x-lg" aria-hidden="true" />
          </button>
        </div>
      )}

      {notice !== null && (
        <div className="settings-notice banner" role="status">
          <i className="bi bi-check-circle" aria-hidden="true" /> {notice}
          <button type="button" className="icon-btn" aria-label="Закрыть уведомление" onClick={() => setNotice(null)}>
            <i className="bi bi-x-lg" aria-hidden="true" />
          </button>
        </div>
      )}

      <div className="settings-groups">
        {groups.map((g) => {
          const isCollapsed = collapsed.has(g.group);
          const changed = g.items.filter((s) => s.differs_from_default && !s.is_env_locked).length;
          const dangerous = g.items.filter((s) => s.is_dangerous).length;
          const envLocked = g.items.filter((s) => s.is_env_locked).length;
          return (
            <section key={g.group} className={`settings-group${isCollapsed ? " collapsed" : ""}`}>
              <button
                type="button"
                className="settings-group-head"
                aria-expanded={!isCollapsed}
                onClick={() => {
                  const next = new Set(collapsed);
                  if (isCollapsed) next.delete(g.group);
                  else next.add(g.group);
                  persistCollapsed(next);
                }}
              >
                <i className={`bi ${g.icon}`} aria-hidden="true" />
                <h3 className="settings-group-title">{g.title}</h3>
                <span className="settings-group-count">{g.items.length}</span>
                {changed > 0 && (
                  <span className="badge modified">
                    <i className="bi bi-pencil-fill" aria-hidden="true" /> {changed}
                  </span>
                )}
                {dangerous > 0 && (
                  <span className="badge danger" title="Есть опасные настройки">
                    <i className="bi bi-exclamation-triangle" aria-hidden="true" /> {dangerous}
                  </span>
                )}
                {envLocked > 0 && (
                  <span className="badge env" title="Часть настроек управляется через .env">
                    <i className="bi bi-lock-fill" aria-hidden="true" /> {envLocked}
                  </span>
                )}
                <i className={`bi chevron ${isCollapsed ? "bi-chevron-down" : "bi-chevron-up"}`} aria-hidden="true" />
              </button>
              {!isCollapsed && (
                <div className="settings-group-body">
                  {g.items.map((s) => (
                    <SettingRow key={s.key} meta={s} onSave={handleSave} onReset={handleResetRow} onRequestConfirm={setConfirmState} />
                  ))}
                </div>
              )}
            </section>
          );
        })}
      </div>

      {confirmState !== null && (
        <ConfirmDangerModal
          title={confirmState.title}
          body={confirmState.body}
          details={confirmState.details}
          confirmLabel={confirmState.confirmLabel}
          busy={busy}
          onCancel={() => setConfirmState(null)}
          onConfirm={confirmState.action}
        />
      )}
    </div>
  );
}
