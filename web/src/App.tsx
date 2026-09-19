import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes, useParams } from "react-router";
import { Topbar } from "./components/Topbar";
import { SearchScreen } from "./screens/SearchScreen";
import { ProjectsScreen } from "./screens/ProjectsScreen";
import { StatsScreen } from "./screens/StatsScreen";

// sigma + graphology weigh ~100KB gz — the constellation loads on demand
const GraphScreen = lazy(() =>
  import("./screens/GraphScreen").then((m) => ({ default: m.GraphScreen })),
);

/** /memory/:id keeps the search results behind it (§3): same screen + panel */
function MemoryRoute() {
  const { id } = useParams<{ id: string }>();
  if (!id) return <Navigate to="/search" replace />;
  return <SearchScreen panelId={id} />;
}

/** Chunk-level spinner for the lazy graph screen */
function GraphFallback() {
  return (
    <div className="graph-root">
      <div className="state-block graph-empty">
        <span className="pulse-dot big" aria-hidden="true" />
        <h3>Пробуждаю WebGL…</h3>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <BrowserRouter basename="/ui">
      <Topbar />
      <Routes>
        <Route path="/" element={<Navigate to="/search" replace />} />
        <Route path="/search" element={<SearchScreen />} />
        <Route path="/memory/:id" element={<MemoryRoute />} />
        <Route
          path="/graph"
          element={
            <Suspense fallback={<GraphFallback />}>
              <GraphScreen />
            </Suspense>
          }
        />
        <Route path="/projects" element={<ProjectsScreen />} />
        <Route path="/stats" element={<StatsScreen />} />
        <Route path="*" element={<Navigate to="/search" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
