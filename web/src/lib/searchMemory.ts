// Мост поискового запроса между экранами Графа и Поиска (фидбек Мастера):
// текст поля переживает переходы — один источник истины на оба экрана.
// sessionStorage: вкладочная изоляция без персистентности между сессиями.

const STORAGE_KEY = "selti.search.query";

/** Последний запрос; "" если хранилище недоступно или ничего не сохранено. */
export function getSearchQuery(): string {
  try {
    return sessionStorage.getItem(STORAGE_KEY) ?? "";
  } catch {
    // SSR / private mode / заблокированное хранилище — читаем как пустое
    return "";
  }
}

/** Запомнить запрос; пустая строка стирает ключ (честное «запроса нет»). */
export function setSearchQuery(query: string): void {
  try {
    if (query) sessionStorage.setItem(STORAGE_KEY, query);
    else sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // запись не критична — пережить переход просто не получится
  }
}
