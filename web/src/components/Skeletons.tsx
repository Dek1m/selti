/** Loading skeletons (§4.4): 4 shimmering cards, no CLS. */
export function ResultsSkeleton() {
  return (
    <div aria-hidden="true">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="skeleton-card">
          <div style={{ flex: 1, display: "grid", gap: 10 }}>
            <div className="sk" style={{ width: `${92 - i * 7}%`, height: 14 }} />
            <div className="sk" style={{ width: "88%", height: 10 }} />
            <div className="sk" style={{ width: `${70 - i * 5}%`, height: 10 }} />
          </div>
          <div style={{ display: "grid", gap: 8, justifyContent: "end", width: 104 }}>
            <div className="sk" style={{ width: 64, height: 20 }} />
            <div className="sk" style={{ width: 104, height: 4 }} />
          </div>
        </div>
      ))}
    </div>
  );
}
