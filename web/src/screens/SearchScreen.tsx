import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router";
import { searchGranules, type SearchFilters } from "../api/selti";
import type { SearchHit } from "../api/types";
import { FilterBar } from "../components/FilterBar";
import { GranuleCard } from "../components/GranuleCard";
import { GranulePanel } from "../components/GranulePanel";
import { ResultsSkeleton } from "../components/Skeletons";
import { useDebouncedValue } from "../hooks/useDebouncedValue";
import { useUrlSync } from "../hooks/useUrlSync";
import { useFilters } from "../store/filters";

// Real-query examples (§4.1) — clicked, they fill the hero search
const SUGGESTIONS = [
  "деплой фазы 2",
  "миграция на Celery",
  "кластеры Level 2",
  "дедупликация эмбеддингов",
  "суперсессия гранул",
];

function isEditable(el: EventTarget | null): boolean {
  const tag = (el as HTMLElement | null)?.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";
}

export function SearchScreen({ panelId }: { panelId?: string }) {
  useUrlSync();
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const [activeIndex, setActiveIndex] = useState<number | null>(null);

  const { query, namespaces, status, period, project } = useFilters();
  const debouncedQuery = useDebouncedValue(query, 300);
  const submitted = debouncedQuery.trim().length > 0;

  const filters: SearchFilters = {
    query: debouncedQuery.trim(),
    namespaces,
    project,
    status,
    // superseded/retracted are hidden behind the historical flag on the backend
    includeHistorical: status === "superseded" || status === "retracted",
    period,
  };

  const search = useQuery({
    queryKey: ["search", filters],
    queryFn: () => searchGranules(filters),
    enabled: submitted,
    placeholderData: keepPreviousData,
  });

  const results = submitted ? (search.data?.results ?? []) : [];
  const stale = search.isPlaceholderData;

  // §4.4: new search closes the card — the beam is the protagonist again.
  // Keep the query string: the panel route shares the search cache.
  const openCard = (hit: SearchHit) =>
    navigate(
      { pathname: `/memory/${hit.id}`, search: window.location.search },
      // pass the hit so the panel can render its score decomposition
      { state: { hit } },
    );

  // Keyboard (§9): "/" focuses the search, arrows walk the results,
  // Enter opens the active card. Reset the walk on every new result set.
  useEffect(() => setActiveIndex(null), [debouncedQuery, namespaces, status, period, project]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "/" && !isEditable(e.target)) {
        e.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
        return;
      }
      if (results.length === 0 || isEditable(e.target) && e.target !== inputRef.current) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setActiveIndex((i) => (i === null ? 0 : Math.min(i + 1, results.length - 1)));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActiveIndex((i) => (i === null ? results.length - 1 : Math.max(i - 1, 0)));
      } else if (e.key === "Enter" && activeIndex !== null) {
        e.preventDefault();
        openCard(results[activeIndex]);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [results, activeIndex, navigate]);

  useEffect(() => {
    if (activeIndex !== null) {
      document.querySelectorAll<HTMLElement>(".result")[activeIndex]?.scrollIntoView({
        block: "nearest",
        behavior: "smooth",
      });
    }
  }, [activeIndex]);

  const heroMode = !submitted && !panelId;

  return (
    <div className={heroMode ? "stage hero-mode" : "stage"}>
      <div className={heroMode ? "hero" : "hero hero-compact"}>
        <div className="searchbox">
          <i className="bi bi-search icon" aria-hidden="true" />
          <input
            ref={inputRef}
            type="text"
            value={query}
            onChange={(e) => useFilters.getState().setQuery(e.target.value)}
            placeholder="Спроси глубину…"
            aria-label="Поиск по памяти"
            autoComplete="off"
            spellCheck={false}
          />
          {query && (
            <button
              className="icon-btn"
              aria-label="Очистить запрос"
              onClick={() => {
                useFilters.getState().setQuery("");
                inputRef.current?.focus();
              }}
            >
              <i className="bi bi-x-lg" aria-hidden="true" />
            </button>
          )}
          <kbd aria-hidden="true">/</kbd>
        </div>
        {heroMode && (
          <div className="hero-suggest">
            {SUGGESTIONS.map((s) => (
              <button key={s} className="suggest" onClick={() => useFilters.getState().setQuery(s)}>
                {s}
              </button>
            ))}
          </div>
        )}
      </div>

      <FilterBar />

      {submitted && (
        <div className={`results-grid${panelId ? " with-panel" : ""}`} style={{ marginTop: 24 }}>
          <section aria-label="Результаты поиска">
            <span className="sr-only" role="status" aria-live="polite">
              {search.data ? `Найдено ${search.data.results.length} гранул` : ""}
            </span>

            {search.isPending ? (
              <ResultsSkeleton />
            ) : search.isError ? (
              <div className="state-block error">
                <i className="bi bi-wifi-off" aria-hidden="true" />
                <h3>Selti не отвечает</h3>
                <p>{(search.error as Error).message}</p>
                <button className="btn" onClick={() => void search.refetch()}>
                  <i className="bi bi-arrow-clockwise" aria-hidden="true" /> Повторить
                </button>
              </div>
            ) : results.length === 0 ? (
              <div className="state-block">
                <i className="bi bi-stars" aria-hidden="true" />
                <h3>Ничего не нашлось по «{debouncedQuery.trim()}»</h3>
                <p>Попробуйте убрать фильтры, искать по корню слова или проверить статус-фильтр.</p>
              </div>
            ) : (
              <>
                <div className="results-head">
                  <span className="count">
                    Найдено <b>{results.length}</b> гранул
                  </span>
                  <span className="took">{((search.data?.tookMs ?? 0) / 1000).toFixed(2)} с</span>
                  <span className="formula" title="Разложение релевантности">
                    score = <i>rrf</i> × <i>decay</i> × <i>importance</i>
                  </span>
                </div>
                <div style={stale ? { opacity: 0.6, transition: "opacity var(--sl-dur) var(--sl-ease)" } : undefined}>
                  {results.map((hit, i) => (
                    <GranuleCard
                      key={hit.id}
                      hit={hit}
                      query={filters.query}
                      active={i === activeIndex || hit.id === panelId}
                      onOpen={openCard}
                    />
                  ))}
                </div>
              </>
            )}
          </section>

          {panelId && (
            <GranulePanel
              id={panelId}
              onClose={() =>
                navigate({ pathname: "/search", search: window.location.search }, { replace: true })
              }
            />
          )}
        </div>
      )}

      {/* Direct deep link with no query: centered card, hero search above */}
      {!submitted && panelId && (
        <GranulePanel
          id={panelId}
          centered
          onClose={() => navigate({ pathname: "/search" }, { replace: true })}
        />
      )}
    </div>
  );
}
