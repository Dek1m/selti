import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { getContext, getProject, getProjects } from "../api/selti";
import type { ProjectContext } from "../api/types";

const KIND_ICONS: Record<string, string> = {
  code: "bi-code-slash",
  org: "bi-people",
  workspace: "bi-briefcase",
};

/** Sections rendered head-first; known layers keep their canonical order. */
const SECTION_ORDER = ["stack", "decisions", "code", "insights", "infra"];

function orderedSections(sections: Record<string, string[]>): Array<[string, string[]]> {
  const known = SECTION_ORDER.filter((key) => sections[key]?.length);
  const rest = Object.keys(sections).filter((key) => !SECTION_ORDER.includes(key) && sections[key]?.length);
  return [...known, ...rest].map((key) => [key, sections[key]] as [string, string[]]);
}

/** Link fields the list payload always carries; the full card adds docs/homepage. */
type LinkSource = {
  repo_url: string | null;
  local_path: string | null;
  docs_url?: string | null;
  homepage_url?: string | null;
};

function ProjectLinks({ project }: { project: LinkSource }) {
  const links: Array<{ icon: string; url: string; label: string }> = [];
  if (project.repo_url) links.push({ icon: "bi-github", url: project.repo_url, label: "Репозиторий" });
  if (project.local_path) links.push({ icon: "bi-folder-symlink", url: `file:///${project.local_path.replace(/\\/g, "/")}`, label: "Локальная папка" });
  if (project.docs_url) links.push({ icon: "bi-book", url: project.docs_url, label: "Документация" });
  if (project.homepage_url) links.push({ icon: "bi-globe", url: project.homepage_url, label: "Домашняя страница" });
  if (links.length === 0) return null;
  return (
    <div className="project-links">
      {links.map((l) => (
        <a key={l.url} className="icon-btn project-link" href={l.url} target="_blank" rel="noreferrer" aria-label={l.label} title={l.label}>
          <i className={`bi ${l.icon}`} aria-hidden="true" />
        </a>
      ))}
    </div>
  );
}

function ContextSection({ title, lines }: { title: string; lines: string[] }) {
  return (
    <section className="section">
      <h4 className="section-label">
        <i className="bi bi-hexagon-half" aria-hidden="true" /> {title}
      </h4>
      <pre className="ctx-section">{lines.join("\n")}</pre>
    </section>
  );
}

/** Knowledge cloud panel (§7): full card + context snapshot. */
function ProjectPanel({ slug, onClose }: { slug: string; onClose: () => void }) {
  const project = useQuery({ queryKey: ["project", slug], queryFn: () => getProject(slug) });
  const context = useQuery({ queryKey: ["context", slug], queryFn: () => getContext(slug) });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const detail = project.data;
  const cloud: ProjectContext | undefined = context.data;
  const sections = cloud ? orderedSections(cloud.sections) : [];

  return (
    <aside className="panel" role="complementary" aria-label={`Проект ${slug}`}>
      <div className="panel-head">
        <span className="mono panel-slug">{slug}</span>
        {project.data && (
          <>
            <span className="badge kind">
              <i className={`bi ${KIND_ICONS[project.data.kind] ?? "bi-box"}`} aria-hidden="true" />
              {project.data.kind}
            </span>
            <span className={`badge ${project.data.status === "active" ? "asserted" : "superseded"}`}>
              {project.data.status}
            </span>
          </>
        )}
        <button className="icon-btn close" aria-label="Закрыть карточку (Esc)" onClick={onClose}>
          <i className="bi bi-x-lg" aria-hidden="true" />
        </button>
      </div>

      <div className="panel-body">
        {project.isPending ? (
          <div style={{ display: "grid", gap: 12 }} aria-hidden="true">
            <div className="sk" style={{ height: 44 }} />
            <div className="sk" style={{ height: 96 }} />
            <div className="sk" style={{ height: 160 }} />
          </div>
        ) : project.isError ? (
          <div className="state-block error">
            <i className="bi bi-slash-circle" aria-hidden="true" />
            <h3>Проект не найден</h3>
            <p>{(project.error as Error).message}</p>
          </div>
        ) : (
          <>
            <section className="section">
              <h3 className="project-name">{detail?.name}</h3>
              <p className="content">{detail?.description ?? "Описание не заполнено."}</p>
              {detail && <ProjectLinks project={detail} />}
            </section>

            {detail && detail.technologies.length > 0 && (
              <section className="section">
                <h4 className="section-label">
                  <i className="bi bi-stack" aria-hidden="true" /> Стек
                </h4>
                <div className="stack-tags">
                  {detail.technologies.map((t) => (
                    <span key={t.name} className="chip-static" title={t.purpose ?? t.name}>
                      {t.name}
                    </span>
                  ))}
                </div>
              </section>
            )}

            <section className="section">
              <h4 className="section-label">
                <i className="bi bi-cloud-fill" aria-hidden="true" /> Облачко знаний
              </h4>
              {context.isPending ? (
                <p className="rel-empty">Собираю снапшот…</p>
              ) : context.isError ? (
                <div className="superseded-note">
                  <i className="bi bi-hourglass-split" aria-hidden="true" />
                  Снапшот ещё собирается — будет готов после ближайшего цикла beat.
                </div>
              ) : sections.length === 0 ? (
                <p className="rel-empty">Снапшот пуст: у проекта пока нет гранул.</p>
              ) : (
                <>
                  {cloud?.stale && (
                    <div className="superseded-note">
                      <i className="bi bi-clock-history" aria-hidden="true" />
                      После снапшота были новые записи — показываю как есть.
                    </div>
                  )}
                  <p className="ctx-meta">
                    {cloud?.granule_count.toLocaleString("ru-RU")} гранул ·{" "}
                    {cloud?.computed_at ? new Date(cloud.computed_at).toLocaleString("ru-RU") : "—"}
                  </p>
                  {sections.map(([title, lines]) => (
                    <ContextSection key={title} title={title} lines={lines} />
                  ))}
                </>
              )}
            </section>
          </>
        )}
      </div>
    </aside>
  );
}

/** /ui/projects — registry bento grid (§7); click opens the cloud panel. */
export function ProjectsScreen() {
  const projects = useQuery({ queryKey: ["projects"], queryFn: getProjects, staleTime: 5 * 60_000 });
  const [selected, setSelected] = useState<string | null>(null);
  // Re-clicking the active card folds the panel back
  const toggle = (slug: string) => setSelected(selected === slug ? null : slug);

  return (
    <div className="stage">
      <h2 className="screen-title">Реестр проектов</h2>

      {projects.isPending ? (
        <div className="bento-grid" aria-hidden="true">
          {Array.from({ length: 6 }, (_, i) => (
            <div key={i} className="sk" style={{ height: 148 }} />
          ))}
        </div>
      ) : projects.isError ? (
        <div className="state-block error">
          <i className="bi bi-wifi-off" aria-hidden="true" />
          <h3>Реестр недоступен</h3>
          <p>{(projects.error as Error).message}</p>
          <button className="btn" onClick={() => void projects.refetch()}>
            <i className="bi bi-arrow-clockwise" aria-hidden="true" /> Повторить
          </button>
        </div>
      ) : (projects.data?.projects ?? []).length === 0 ? (
        <div className="state-block">
          <i className="bi bi-collection" aria-hidden="true" />
          <h3>Реестр пуст</h3>
          <p>Проекты появятся после регистрации через POST /api/projects.</p>
        </div>
      ) : (
        <div className={`bento-grid${selected ? " with-panel" : ""}`}>
          <section aria-label="Проекты" className="bento-cards">
            {(projects.data?.projects ?? []).map((p) => (
              <article
                key={p.id}
                className={`project-card${selected === p.slug ? " active" : ""}`}
                role="button"
                tabIndex={0}
                aria-pressed={selected === p.slug}
                onClick={() => toggle(p.slug)}
                onKeyDown={(e) => e.key === "Enter" && toggle(p.slug)}
              >
                <div className="project-card-head">
                  <span className="mono project-card-slug">{p.slug}</span>
                  <span className="badge kind">
                    <i className={`bi ${KIND_ICONS[p.kind] ?? "bi-box"}`} aria-hidden="true" />
                    {p.kind}
                  </span>
                  <span className={`badge ${p.status === "active" ? "asserted" : "superseded"}`}>{p.status}</span>
                </div>
                <h3 className="project-card-name">{p.name}</h3>
                <p className="project-card-desc">{p.description ?? "Описание не заполнено."}</p>
                <ProjectLinks project={p} />
              </article>
            ))}
          </section>

          {selected && <ProjectPanel slug={selected} onClose={() => setSelected(null)} />}
        </div>
      )}
    </div>
  );
}
