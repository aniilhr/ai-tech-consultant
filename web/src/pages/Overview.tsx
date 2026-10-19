import { brainApi, inboxApi, scopeApi, type BrainStats, type ProjectSummary } from "../api";
import { Badge, ErrorAlert, Loading, Stat, fmtDate, pct, useAsync } from "../ui";

interface Health {
  status: string;
  llm?: { provider: string; models?: Record<string, string> };
  brain_reachable?: boolean;
}
interface InboxStats {
  emails: Record<string, number>;
  reviews: { total: number; unchanged_rate: number | null; avg_edit_ratio: number | null };
  outbox: number;
  auto_send_enabled: boolean;
  streak_required: number;
}

async function settle<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch {
    return null;
  }
}

export default function Overview() {
  const { data, loading, error, reload } = useAsync(
    async () => {
      const [brainHealth, inboxHealth, scopeHealth, brainStats, inboxStats, projects] = await Promise.all([
        settle(brainApi<Health>("/health")),
        settle(inboxApi<Health>("/health")),
        settle(scopeApi<Health>("/health")),
        settle(brainApi<BrainStats>("/v1/stats")),
        settle(inboxApi<InboxStats>("/stats")),
        settle(scopeApi<ProjectSummary[]>("/projects")),
      ]);
      return { brainHealth, inboxHealth, scopeHealth, brainStats, inboxStats, projects };
    },
    [],
  );
  if (loading && !data) return <Loading />;
  if (!data) return <ErrorAlert error={error} onRetry={reload} />;
  const { brainHealth, inboxHealth, scopeHealth, brainStats, inboxStats, projects } = data;
  const offline = [brainHealth, inboxHealth, scopeHealth].some((h) => h?.llm?.provider === "offline");
  const activeArticles = brainStats?.articles.active ?? 0;
  const emails = inboxStats ? Object.values(inboxStats.emails).reduce((a, b) => a + b, 0) : 0;

  const services = [
    { name: "Knowledge Brain", lang: "Python · FastAPI", health: brainHealth, href: "#/chat" },
    { name: "Inbox Agent", lang: "TypeScript · Node", health: inboxHealth, href: "#/inbox" },
    { name: "Scope Builder", lang: "Python · FastAPI", health: scopeHealth, href: "#/scope" },
  ];

  return (
    <div className="stack">
      {offline && (
        <div className="alert alert-warn">
          <div>
            <strong>Offline mode.</strong> No AI provider key is configured, so answers are extractive quotes, drafts are templates and
            extraction uses keyword rules. Everything is labelled “Offline mode”. Set <code>GEMINI_API_KEY</code> in <code>.env</code> and restart to
            use the AI models.
          </div>
        </div>
      )}
      {activeArticles === 0 && brainStats && (
        <div className="alert alert-info">
          <div>
            The knowledge base is empty. Open <a href="#/kb">Knowledge Base</a> and run a first sync (start with 25 articles) before using chat, inbox
            drafts or capability checks.
          </div>
        </div>
      )}

      <div className="grid-3">
        {services.map((s) => (
          <a key={s.name} href={s.href} className="card card-body" style={{ color: "inherit", textDecoration: "none" }}>
            <div className="row-between">
              <h2>{s.name}</h2>
              {s.health ? (
                <Badge tone="green">
                  <span className="dot dot-green" /> Online
                </Badge>
              ) : (
                <Badge tone="red">
                  <span className="dot dot-red" /> Unreachable
                </Badge>
              )}
            </div>
            <div className="muted small" style={{ marginTop: 4 }}>
              {s.lang}
            </div>
            <div className="small" style={{ marginTop: 10 }}>
              Model:{" "}
              {s.health?.llm?.provider === "offline" ? (
                <Badge tone="amber">offline</Badge>
              ) : (
                <span className="mono">{s.health?.llm?.models?.smart ?? s.health?.llm?.provider ?? "–"}</span>
              )}
              {s.health?.brain_reachable !== undefined && (
                <span style={{ marginLeft: 8 }}>
                  Brain API: {s.health.brain_reachable ? <Badge tone="green">connected</Badge> : <Badge tone="red">not reachable</Badge>}
                </span>
              )}
            </div>
          </a>
        ))}
      </div>

      <h2>Knowledge Brain</h2>
      <div className="stats">
        <Stat label="Active articles" value={activeArticles} sub={`${brainStats?.articles.pending ?? 0} pending · ${brainStats?.articles.failed ?? 0} failed · ${brainStats?.articles.removed ?? 0} removed`} />
        <Stat label="Searchable chunks" value={brainStats?.chunks ?? "–"} sub={`${brainStats?.chunks_embedded ?? 0} with embeddings`} />
        <Stat label="Screenshots understood" value={brainStats?.images.described ?? 0} sub={`${brainStats?.images.pending ?? 0} pending`} />
        <Stat label="Open escalations" value={brainStats?.escalations_open ?? "–"} sub={`${brainStats?.escalations_answered ?? 0} answered by experts`} />
        <Stat label="Questions answered" value={pct(brainStats?.queries.answer_rate)} sub={`${brainStats?.queries.total ?? 0} questions · last sync ${fmtDate(brainStats?.last_sync?.started_at)}`} />
      </div>

      <h2>Inbox Agent</h2>
      <div className="stats">
        <Stat label="Emails" value={emails} sub={`${inboxStats?.emails.new ?? 0} new · ${inboxStats?.emails.drafted ?? 0} awaiting review`} />
        <Stat label="Human reviews" value={inboxStats?.reviews.total ?? "–"} sub={`${pct(inboxStats?.reviews.unchanged_rate)} approved unchanged`} />
        <Stat label="Avg. edit size" value={pct(inboxStats?.reviews.avg_edit_ratio)} sub="share of words changed by humans" />
        <Stat label="Auto-send" value={inboxStats?.auto_send_enabled ? "Enabled" : "Off"} sub={`needs ${inboxStats?.streak_required ?? 20} unchanged in a row`} />
      </div>

      <h2>Scope Builder</h2>
      <div className="stats">
        <Stat label="Projects" value={projects?.length ?? "–"} />
        <Stat label="Approved baselines" value={projects?.filter((p) => p.baseline_version).length ?? "–"} />
        <Stat label="Proposals ready to send" value={projects?.reduce((a, p) => a + p.approved_proposals, 0) ?? "–"} />
      </div>
    </div>
  );
}
