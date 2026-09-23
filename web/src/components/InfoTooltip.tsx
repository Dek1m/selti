import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties } from "react";

interface InfoTooltipProps {
  /** Текст подсказки (description_ru) */
  text: string;
  /** Человекочитаемое имя настройки для aria-label триггера */
  label: string;
}

const GAP = 8;
const VIEWPORT_PADDING = 8;

/** «?»-в-кружке: подсказка по hover и focus (tabIndex, aria-describedby).
 *
 * Позиционируется fixed относительно триггера: по умолчанию над ним;
 * если сверху нет места (первые строки группы, где поповер срезается
 * границей с overflow: clip) — разворачивается вниз. Боковые края
 * экрана поджимаются. Fixed выводит поповер из clip-контекста группы.
 * Скролл/ресайз закрывают тултип — fixed не должен «отставать». */
export function InfoTooltip({ text, label }: InfoTooltipProps) {
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const tipRef = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  const [style, setStyle] = useState<CSSProperties>({});

  /** Черновое размещение над триггером (до измерения самого тултипа). */
  const place = () => {
    const r = triggerRef.current?.getBoundingClientRect();
    if (!r) return;
    const cx = r.left + r.width / 2;
    setStyle({ left: cx, top: undefined, bottom: window.innerHeight - r.top + GAP });
  };

  /** Уточнение после рендера: flip вниз + clamp по краям viewport. */
  useLayoutEffect(() => {
    if (!open) return;
    const tip = tipRef.current?.getBoundingClientRect();
    const trigger = triggerRef.current?.getBoundingClientRect();
    if (!tip || !trigger) return;
    const cx = trigger.left + trigger.width / 2;
    const fitsAbove = tip.top >= VIEWPORT_PADDING;
    const left = Math.min(
      Math.max(cx, tip.width / 2 + VIEWPORT_PADDING),
      window.innerWidth - tip.width / 2 - VIEWPORT_PADDING,
    );
    setStyle(
      fitsAbove
        ? { left, top: undefined, bottom: window.innerHeight - trigger.top + GAP }
        : { left, top: trigger.bottom + GAP, bottom: undefined },
    );
  }, [open, text]);

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const show = () => {
    place();
    setOpen(true);
  };

  return (
    <span
      className={`info-tip${open ? " open" : ""}`}
      onMouseEnter={show}
      onMouseLeave={() => setOpen(false)}
    >
      <button
        type="button"
        ref={triggerRef}
        className="info-tip-trigger"
        tabIndex={0}
        aria-label={`Подсказка: ${label}`}
        aria-describedby={open ? id : undefined}
        aria-expanded={open}
        onFocus={show}
        onBlur={() => setOpen(false)}
        onClick={(e) => {
          // Тап по тач-экрану: toggle поверх mouse-событий
          e.preventDefault();
          if (open) setOpen(false);
          else show();
        }}
      >
        <i className="bi bi-question-circle" aria-hidden="true" />
      </button>
      {/* Пустое description_ru с бэка — тултип просто не раскрывается */}
      {open && text.trim() !== "" && (
        <span role="tooltip" id={id} ref={tipRef} className="info-tip-body" style={style}>
          {text}
        </span>
      )}
    </span>
  );
}
