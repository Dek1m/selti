import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";

interface ConfirmDangerModalProps {
  title: string;
  /** Человеческий текст: что именно произойдёт */
  body: string;
  /** Список затрагиваемых опасных ключей (имена + текущие значения) */
  details?: string[];
  confirmLabel?: string;
  cancelLabel?: string;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Модалка подтверждения опасного действия (удаление/сброс/dangerous-правка).
 * Escape и клик по подложке отменяют; фокус сразу на безопасной кнопке. */
export function ConfirmDangerModal({
  title,
  body,
  details,
  confirmLabel = "Подтверждаю",
  cancelLabel = "Отмена",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDangerModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    cancelRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onCancel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return createPortal(
    <div className="modal-overlay" onClick={busy ? undefined : onCancel}>
      <div
        className="modal danger"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="modal-title"
        aria-describedby="modal-body"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 id="modal-title" className="modal-title">
          <i className="bi bi-exclamation-triangle-fill" aria-hidden="true" /> {title}
        </h3>
        <p id="modal-body" className="modal-body">
          {body}
        </p>
        {details && details.length > 0 && (
          <ul className="modal-details">
            {details.map((d) => (
              <li key={d}>
                <i className="bi bi-exclamation-circle" aria-hidden="true" /> {d}
              </li>
            ))}
          </ul>
        )}
        <div className="modal-actions">
          <button ref={cancelRef} type="button" className="btn" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </button>
          <button type="button" className="btn danger" onClick={onConfirm} disabled={busy}>
            {busy ? "Применяю…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
