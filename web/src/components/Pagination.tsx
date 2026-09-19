// Pagination control (§4.4): the API ranks deterministically, so prev/next
// over offset pages is stable. No total count exists for semantic search —
// "next" simply disappears on a short page.

import { PAGE_SIZE } from "../api/selti";

interface PaginationProps {
  page: number;
  resultCount: number;
  onPage: (page: number) => void;
}

export function Pagination({ page, resultCount, onPage }: PaginationProps) {
  if (page === 1 && resultCount < PAGE_SIZE) return null;
  const from = (page - 1) * PAGE_SIZE + 1;
  const to = (page - 1) * PAGE_SIZE + resultCount;
  const hasPrev = page > 1;
  const hasNext = resultCount === PAGE_SIZE;
  return (
    <nav className="pagination" aria-label="Страницы результатов">
      <button className="page-btn" disabled={!hasPrev} onClick={() => onPage(page - 1)}>
        <i className="bi bi-arrow-left" aria-hidden="true" /> Назад
      </button>
      <span className="page-indicator" aria-live="polite">
        Показаны {from}–{to}
      </span>
      <button className="page-btn" disabled={!hasNext} onClick={() => onPage(page + 1)}>
        Вперёд <i className="bi bi-arrow-right" aria-hidden="true" />
      </button>
    </nav>
  );
}
