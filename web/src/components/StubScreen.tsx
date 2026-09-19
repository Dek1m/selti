import { useNavigate } from "react-router";

/** Placeholder for the next-iteration screens (§3): menu exists, page not yet. */
export function StubScreen({
  icon,
  title,
  hint,
}: {
  icon: string;
  title: string;
  hint: string;
}) {
  const navigate = useNavigate();
  return (
    <div className="stage">
      <div className="stub">
        <div className="halo">
          <i className={`bi ${icon}`} aria-hidden="true" />
        </div>
        <h2>{title}</h2>
        <p>{hint}</p>
        <button className="btn primary" onClick={() => navigate("/search")}>
          <i className="bi bi-search" aria-hidden="true" /> Вернуться к поиску
        </button>
      </div>
    </div>
  );
}
