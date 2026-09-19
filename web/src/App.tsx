import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router";
import { Topbar } from "./components/Topbar";
import { SearchScreen } from "./screens/SearchScreen";
import { StubScreen } from "./components/StubScreen";

/** /memory/:id keeps the search results behind it (§3): same screen + panel */
function MemoryRoute() {
  const { id } = useParams<{ id: string }>();
  if (!id) return <Navigate to="/search" replace />;
  return <SearchScreen panelId={id} />;
}

export default function App() {
  return (
    <BrowserRouter>
      <Topbar />
      <Routes>
        <Route path="/" element={<Navigate to="/search" replace />} />
        <Route path="/search" element={<SearchScreen />} />
        <Route path="/memory/:id" element={<MemoryRoute />} />
        <Route
          path="/graph"
          element={
            <StubScreen
              icon="bi-diagram-3"
              title="Созвездие собирается"
              hint="Экран «Граф» (@react-sigma, WebGL) — в следующей итерации Фазы 5. Пока свет ищут через поиск."
            />
          }
        />
        <Route
          path="/projects"
          element={
            <StubScreen
              icon="bi-collection"
              title="Проекты спят во тьме"
              hint="Реестр проектов с облачками знаний — в следующей итерации Фазы 5."
            />
          }
        />
        <Route
          path="/stats"
          element={
            <StubScreen
              icon="bi-graph-up"
              title="Глубина ещё не измерена"
              hint="Дашборд метрик из /api/stats — в следующей итерации Фазы 5."
            />
          }
        />
        <Route path="*" element={<Navigate to="/search" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
