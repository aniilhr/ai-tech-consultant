import { inboxApi, type KindStatus, type RulesVersion } from "../api";
import { Badge, ErrorAlert, Loading, Progress, fmtDate, label, pct, useAction, useAsync } from "../ui";

interface LearningData {
  kinds: KindStatus[];
  rules: RulesVersion[];
  edit_pairs_available: number;
  auto_send_enabled: boolean;
  streak_required: number;
}

export default function Learning() {
  const { data, loading, error, reload } = useAsync(() => inboxApi<LearningData>("/learning"), []);
  const action = useAction();
  if (loading && !data) return <Loading />;
  if (!data) return <ErrorAlert error={error} onRetry={reload} />;

  const propose = async () => {
    await action.run("propose", () => inboxApi("/learning/rules/propose", { method: "POST" }));
    await reload();
  };
  const activate = async (v: number) => {
    await action.run(`activate-${v}`, () => inboxApi(`/learning/rules/${v}/activate`, { method: "POST" }));
    await reload();
  };

  return (
    <div className="stack">
      <div className={`alert ${data.auto_send_enabled ? "alert-warn" : "alert-info"}`}>
        <div>
          <strong>Automatic sending is {data.auto_send_enabled ? "ON" : "OFF"}.</strong> An email kind earns autonomy after {data.streak_required} consecutive
          human approvals with no edits. Any edit, rejection, hand-written reply or flagged auto-reply resets it to zero. Refunds, cancellations, billing,
          complaints, account access and quotes always need a human, and so does any single email with risk flags, policy violations or missing evidence.
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <div>
            <h2>Trust by email kind</h2>
            <p>“Unchanged” means the human sent the draft exactly as written (whitespace ignored).</p>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Kind</th>
                <th>Streak</th>
                <th>Status</th>
                <th className="num">Reviewed</th>
                <th className="num">Unchanged (last 20)</th>
                <th>Last reset</th>
              </tr>
            </thead>
            <tbody>
              {data.kinds.map((k) => (
                <tr key={k.kind}>
                  <td>{label(k.kind)}</td>
                  <td style={{ width: 200 }}>
                    <div className="row" style={{ flexWrap: "nowrap" }}>
                      <Progress value={k.streak} max={k.required} />
                      <span className="small nowrap">
                        {k.streak}/{k.required}
                      </span>
                    </div>
                  </td>
                  <td>{k.blocked_reason ? <Badge tone="red">Always human</Badge> : k.eligible ? <Badge tone="green">Eligible</Badge> : <Badge>Learning</Badge>}</td>
                  <td className="num">{k.reviewed}</td>
                  <td className="num">{pct(k.recent_unchanged_rate)}</td>
                  <td className="small">{k.last_reset ? `${k.last_reset.reason} · ${fmtDate(k.last_reset.at)}` : <span className="muted">–</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <div className="card-header">
          <div>
            <h2>Learned style rules</h2>
            <p>
              Distilled from {data.edit_pairs_available} human edits. Each version is measured on the drafts written with it; activate any version to roll
              back.
            </p>
          </div>
          <button className="btn btn-primary" disabled={!!action.busy || data.edit_pairs_available < 2} onClick={() => void propose()}>
            {action.busy === "propose" ? "Learning…" : "Learn from edits"}
          </button>
        </div>
        <ErrorAlert error={action.error} />
        {data.edit_pairs_available < 2 && (
          <div className="card-body muted small">Edit and approve at least two drafts in Inbox Review to learn rules.</div>
        )}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Version</th>
                <th>Rules</th>
                <th className="num">Drafts reviewed</th>
                <th className="num">Unchanged</th>
                <th className="num">Avg. edit</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {data.rules.map((r) => (
                <tr key={r.version}>
                  <td className="nowrap">
                    {r.version === 0 ? "Baseline" : `v${r.version}`} {r.active && <Badge tone="green">active</Badge>}
                    <div className="small muted">{r.source}</div>
                  </td>
                  <td>
                    {r.rules.length ? (
                      <ul style={{ margin: 0, paddingLeft: 16 }}>
                        {r.rules.map((x) => (
                          <li key={x} className="small">
                            {x}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <span className="muted small">No learned rules</span>
                    )}
                    {r.note && <div className="small muted">{r.note}</div>}
                  </td>
                  <td className="num">{r.metrics.drafts_reviewed}</td>
                  <td className="num">{pct(r.metrics.unchanged_rate)}</td>
                  <td className="num">{pct(r.metrics.avg_edit_ratio)}</td>
                  <td>
                    {!r.active && (
                      <button className="btn btn-sm" disabled={!!action.busy} onClick={() => void activate(r.version)}>
                        Activate
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
