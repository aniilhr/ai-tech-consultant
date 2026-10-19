import { useEffect, useState } from "react";
import { scopeApi, type ProjectDetail, type ProjectSummary, type Proposal, type Scope } from "../api";
import { Badge, Empty, ErrorAlert, Loading, Markdown, Modal, ModeBadge, StatusBadge, fmtDate, hours, label, useAction, useAsync } from "../ui";

interface Sample {
  id: string;
  title: string;
  client: string;
  kind: string;
  chars: number;
  note: string | null;
}

function NewProject({ onClose, onCreated }: { onClose: () => void; onCreated: (id: number) => void }) {
  const [name, setName] = useState("");
  const [client, setClient] = useState("");
  const action = useAction();
  return (
    <Modal title="New project" onClose={onClose}>
      <div className="field">
        <label>Project name</label>
        <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Dental directory launch" />
      </div>
      <div className="field">
        <label>Client</label>
        <input type="text" value={client} onChange={(e) => setClient(e.target.value)} />
      </div>
      <ErrorAlert error={action.error} />
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button
          className="btn btn-primary"
          disabled={!!action.busy}
          onClick={() =>
            void action.run("create", () => scopeApi<ProjectDetail>("/projects", { method: "POST", json: { name, client } })).then((p) => p && onCreated(p.project.id))
          }
        >
          Create project
        </button>
      </div>
    </Modal>
  );
}

function AddTranscript({ projectId, onClose, onAdded }: { projectId: number; onClose: () => void; onAdded: () => void }) {
  const samples = useAsync(() => scopeApi<Sample[]>("/samples"), []);
  const [mode, setMode] = useState<"sample" | "paste">("sample");
  const [sampleId, setSampleId] = useState("");
  const [title, setTitle] = useState("");
  const [kind, setKind] = useState("initial");
  const [text, setText] = useState("");
  const action = useAction();
  const submit = async () => {
    const json = mode === "sample" ? { sample_id: sampleId } : { title, kind, text };
    const ok = await action.run("add", () => scopeApi(`/projects/${projectId}/transcripts`, { method: "POST", json }));
    if (ok) onAdded();
  };
  return (
    <Modal title="Add meeting transcript" onClose={onClose}>
      <div className="row" style={{ marginBottom: 12 }}>
        <button className={`btn btn-sm ${mode === "sample" ? "btn-primary" : ""}`} onClick={() => setMode("sample")}>
          Use a sample
        </button>
        <button className={`btn btn-sm ${mode === "paste" ? "btn-primary" : ""}`} onClick={() => setMode("paste")}>
          Paste transcript
        </button>
      </div>
      {mode === "sample" ? (
        <div className="field">
          <label>Sample meeting</label>
          <select value={sampleId} onChange={(e) => setSampleId(e.target.value)}>
            <option value="">Choose…</option>
            {samples.data?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title} ({s.kind === "follow_up" ? "follow-up" : "initial"}, {Math.round(s.chars / 100) / 10}k chars)
              </option>
            ))}
          </select>
        </div>
      ) : (
        <>
          <div className="grid-2">
            <div className="field">
              <label>Title</label>
              <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="field">
              <label>Meeting type</label>
              <select value={kind} onChange={(e) => setKind(e.target.value)}>
                <option value="initial">Initial / discovery</option>
                <option value="follow_up">Follow-up (compare with approved baseline)</option>
              </select>
            </div>
          </div>
          <div className="field">
            <label>Transcript (format “Name (Role): text” works best)</label>
            <textarea rows={10} value={text} onChange={(e) => setText(e.target.value)} />
          </div>
        </>
      )}
      <ErrorAlert error={action.error} />
      <div className="row" style={{ justifyContent: "flex-end" }}>
        <button className="btn btn-primary" disabled={!!action.busy || (mode === "sample" ? !sampleId : text.length < 50)} onClick={() => void submit()}>
          Add transcript
        </button>
      </div>
    </Modal>
  );
}

function RequirementsTab({ scope }: { scope: Scope }) {
  const d = scope.data;
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Requirement</th>
            <th>Delivery approach (from Knowledge Brain)</th>
            <th className="num">Likely</th>
          </tr>
        </thead>
        <tbody>
          {d.requirements.map((r) => {
            const c = d.capability_checks[r.id];
            const h = d.estimate.by_requirement[r.id];
            return (
              <tr key={r.id}>
                <td className="mono">{r.id}</td>
                <td style={{ maxWidth: 420 }}>
                  <div className="row" style={{ gap: 6 }}>
                    <strong>{r.title}</strong>
                    <Badge tone={r.priority === "must" ? "red" : r.priority === "could" ? "gray" : "blue"}>{r.priority}</Badge>
                    {r.origin.startsWith("change") && <Badge tone="amber">changed</Badge>}
                  </div>
                  <div className="small">{r.description}</div>
                  {r.source_quote && <div className="quote">“{r.source_quote}”{r.speaker ? ` — ${r.speaker}` : ""}</div>}
                  {!r.quote_verified && <Badge tone="red" title="The quoted text was not found in the transcript">⚠ quote not found in transcript</Badge>}
                  {r.ambiguous && r.clarification_question && <div className="small" style={{ color: "var(--purple)" }}>Ask: {r.clarification_question}</div>}
                </td>
                <td style={{ maxWidth: 380 }}>
                  {c ? (
                    <>
                      <StatusBadge status={c.status} /> <ModeBadge mode={c.mode} />
                      <div className="small" style={{ marginTop: 4 }}>
                        {c.rationale}
                      </div>
                      {c.guard_note && <div className="small" style={{ color: "var(--amber)" }}>{c.guard_note}</div>}
                      {c.evidence.slice(0, 2).map((e) => (
                        <div key={e.title} className="small">
                          ↳ {e.url ? <a href={e.url} target="_blank" rel="noreferrer">{e.title}</a> : e.title}
                        </div>
                      ))}
                      {!c.brain_answered && <div className="small muted">No help-center evidence found</div>}
                    </>
                  ) : (
                    <span className="muted">–</span>
                  )}
                </td>
                <td className="num">{h ? hours(h.likely) : "–"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function EstimateTab({ scope }: { scope: Scope }) {
  const e = scope.data.estimate;
  return (
    <div>
      <div className="stats card-body">
        <div className="card stat">
          <div className="label">Low</div>
          <div className="value">{hours(e.totals.low)}</div>
        </div>
        <div className="card stat">
          <div className="label">Likely</div>
          <div className="value">{hours(e.totals.likely)}</div>
        </div>
        <div className="card stat">
          <div className="label">High</div>
          <div className="value">{hours(e.totals.high)}</div>
        </div>
        <div className="card stat">
          <div className="label">Method</div>
          <div className="small" style={{ marginTop: 6 }}>
            {e.method}
          </div>
        </div>
      </div>
      {e.notes.map((n) => (
        <div key={n} className="alert alert-info" style={{ margin: "0 16px 12px" }}>
          {n}
        </div>
      ))}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Task</th>
              <th>Req.</th>
              <th>Role</th>
              <th className="num">Low</th>
              <th className="num">Likely</th>
              <th className="num">High</th>
            </tr>
          </thead>
          <tbody>
            {e.tasks.map((t) => (
              <tr key={t.id}>
                <td>
                  <span className="mono muted">{t.id}</span> {t.title}
                  {t.notes && <div className="small muted">{t.notes}</div>}
                </td>
                <td className="mono">{t.requirement_id ?? "–"}</td>
                <td>{label(t.role)}</td>
                <td className="num">{t.low}</td>
                <td className="num">{t.likely}</td>
                <td className="num">{t.high}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ChangesTab({ scope }: { scope: Scope }) {
  const r = scope.change_report;
  if (!r) return <Empty title="No changes">This version came from an initial meeting. Analyse a follow-up meeting after approving a baseline to see changes.</Empty>;
  return (
    <div>
      <div className="stats card-body">
        <div className="card stat">
          <div className="label">Added</div>
          <div className="value">{r.added}</div>
        </div>
        <div className="card stat">
          <div className="label">Modified</div>
          <div className="value">{r.modified}</div>
        </div>
        <div className="card stat">
          <div className="label">Removed</div>
          <div className="value">{r.removed}</div>
        </div>
        <div className="card stat">
          <div className="label">Quote extra (likely)</div>
          <div className="value">{hours(r.out_of_scope_hours.likely)}</div>
          <div className="sub">
            {hours(r.out_of_scope_hours.low)}–{hours(r.out_of_scope_hours.high)}
          </div>
        </div>
        <div className="card stat">
          <div className="label">Net change vs v{r.baseline_version}</div>
          <div className="value">
            {r.total_delta.likely > 0 ? "+" : ""}
            {hours(r.total_delta.likely)}
          </div>
        </div>
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Change</th>
              <th>Requirement</th>
              <th>What changed</th>
              <th className="num">Δ likely</th>
              <th>Scope</th>
            </tr>
          </thead>
          <tbody>
            {r.items.map((i) => (
              <tr key={`${i.change_type}-${i.requirement_id}`}>
                <td>
                  <StatusBadge status={i.change_type} />
                </td>
                <td>
                  <span className="mono muted">{i.requirement_id}</span> {i.title}
                </td>
                <td style={{ maxWidth: 420 }}>
                  <div className="small">{i.summary}</div>
                  {i.source_quote && <div className="quote">“{i.source_quote}”</div>}
                  {!i.quote_verified && <Badge tone="red">⚠ quote not found in transcript</Badge>}
                </td>
                <td className="num">
                  {i.hours_delta.likely > 0 ? "+" : ""}
                  {hours(i.hours_delta.likely)}
                </td>
                <td>{i.out_of_scope ? <Badge tone="red">Out of scope · quote extra</Badge> : <Badge>Within scope</Badge>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function DetailsTab({ scope }: { scope: Scope }) {
  const d = scope.data;
  const groups = [
    ["Decisions", d.decisions],
    ["Assumptions", d.assumptions],
    ["Dependencies", d.dependencies],
    ["Open questions", d.open_questions],
  ] as const;
  return (
    <div className="card-body stack">
      {d.warnings.length > 0 && (
        <div className="alert alert-warn">
          <ul>
            {d.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      <div className="grid-2">
        {groups.map(([title, items]) => (
          <div key={title}>
            <h3 style={{ marginBottom: 6 }}>{title}</h3>
            {items.length ? (
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {items.map((i) => (
                  <li key={i.text} className="small" style={{ marginBottom: 4 }}>
                    {i.text} {!i.quote_verified && <Badge tone="red">unverified</Badge>}
                  </li>
                ))}
              </ul>
            ) : (
              <div className="muted small">None identified</div>
            )}
          </div>
        ))}
      </div>
      <div className="small muted">
        Agents: <ModeBadge mode={d.llm.mode} /> · {d.llm.calls ?? 0} model calls · {(d.llm.input_tokens ?? 0) + (d.llm.output_tokens ?? 0)} tokens
        {d.llm.cost_usd !== null && d.llm.cost_usd !== undefined ? ` · $${d.llm.cost_usd.toFixed(4)}` : ""}
        {d.llm.segments && d.llm.segments > 1 ? ` · transcript processed in ${d.llm.segments} segments` : ""}
      </div>
    </div>
  );
}

function ProposalTab({ scope, reviewer, onChanged }: { scope: Scope; reviewer: string; onChanged: () => void }) {
  const [proposalId, setProposalId] = useState<number | null>(null);
  const proposal = useAsync(() => (proposalId ? scopeApi<Proposal>(`/proposals/${proposalId}`) : Promise.resolve(null)), [proposalId]);
  const [editing, setEditing] = useState(false);
  const [md, setMd] = useState("");
  const action = useAction();
  const p = proposal.data;

  const generate = async () => {
    const r = await action.run("gen", () => scopeApi<Proposal>(`/scopes/${scope.id}/proposal`, { method: "POST" }));
    if (r) {
      setProposalId(r.id);
      onChanged();
    }
  };
  const act = async (key: string, path: string, method: string, json: unknown) => {
    const r = await action.run(key, () => scopeApi<Proposal>(path, { method, json }));
    if (r) {
      proposal.setData(r);
      setEditing(false);
      onChanged();
    }
  };

  return (
    <div className="card-body stack">
      <div className="row-between">
        <div className="row">
          {p ? (
            <>
              <StatusBadge status={p.status} />
              {p.ready_to_send ? <Badge tone="green">Ready to send</Badge> : <Badge tone="amber">Not approved: cannot be sent</Badge>}
              {p.edited ? <Badge>edited by a human</Badge> : null}
            </>
          ) : (
            <span className="muted">No proposal selected for this version.</span>
          )}
        </div>
        <div className="row">
          <button className="btn" disabled={!!action.busy} onClick={() => void generate()}>
            {action.busy === "gen" ? "Writing…" : p ? "Regenerate" : "Generate proposal"}
          </button>
          {p?.status === "draft" && !editing && (
            <>
              <button className="btn" onClick={() => (setMd(p.markdown), setEditing(true))}>
                Edit
              </button>
              <button className="btn btn-danger" disabled={!!action.busy} onClick={() => void act("reject", `/proposals/${p.id}/reject`, "POST", { reviewer, reason: "Rejected in review" })}>
                Reject
              </button>
              <button
                className="btn btn-success"
                disabled={!!action.busy || scope.status !== "approved"}
                title={scope.status !== "approved" ? "Approve this scope version first" : ""}
                onClick={() => void act("approve", `/proposals/${p.id}/approve`, "POST", { approver: reviewer })}
              >
                Approve as ready to send
              </button>
            </>
          )}
        </div>
      </div>
      {scope.status !== "approved" && p?.status === "draft" && (
        <div className="alert alert-info">Approve the scope version (top right) before this proposal can be marked ready to send.</div>
      )}
      <ErrorAlert error={action.error ?? proposal.error} />
      {editing ? (
        <div className="stack-sm">
          <textarea rows={24} value={md} onChange={(e) => setMd(e.target.value)} className="mono" />
          <div className="row">
            <button className="btn btn-primary" disabled={!!action.busy} onClick={() => void act("save", `/proposals/${p?.id}`, "PUT", { markdown: md, editor: reviewer })}>
              Save changes
            </button>
            <button className="btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : p ? (
        <div className="card card-body">
          <Markdown text={p.markdown} />
        </div>
      ) : (
        <Empty title="No proposal yet">The proposal writer turns this scope into a client-ready document. Numbers come straight from the estimate.</Empty>
      )}
    </div>
  );
}

function ScopeView({ scopeId, reviewer, onChanged }: { scopeId: number; reviewer: string; onChanged: () => void }) {
  const scope = useAsync(() => scopeApi<Scope>(`/scopes/${scopeId}`), [scopeId]);
  const [tab, setTab] = useState("requirements");
  const action = useAction();
  useEffect(() => setTab((t) => (t === "changes" ? "requirements" : t)), [scopeId]);
  if (scope.loading && !scope.data) return <Loading />;
  if (!scope.data) return <ErrorAlert error={scope.error} onRetry={scope.reload} />;
  const s = scope.data;
  const approve = async () => {
    const r = await action.run("approve", () => scopeApi<Scope>(`/scopes/${s.id}/approve`, { method: "POST", json: { approver: reviewer } }));
    if (r) {
      scope.setData(r);
      onChanged();
    }
  };
  const tabs = ["requirements", "estimate", ...(s.change_report ? ["changes"] : []), "details", "proposal"];
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>
            Scope v{s.version} <StatusBadge status={s.status} /> <ModeBadge mode={s.data.llm.mode} />
          </h2>
          <p>
            {s.data.requirements.length} requirements · likely {hours(s.data.estimate.totals.likely)} ({hours(s.data.estimate.totals.low)}–
            {hours(s.data.estimate.totals.high)}) · created {fmtDate(s.created_at)}
            {s.approved_by ? ` · approved by ${s.approved_by}` : ""}
          </p>
        </div>
        {s.status === "draft" && (
          <button className="btn btn-success" disabled={!!action.busy} onClick={() => void approve()}>
            Approve as baseline
          </button>
        )}
      </div>
      <ErrorAlert error={action.error} />
      <div className="tabs">
        {tabs.map((t) => (
          <button key={t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => setTab(t)}>
            {t === "changes" ? "Changes vs baseline" : label(t).replace(/^\w/, (c) => c.toUpperCase())}
          </button>
        ))}
      </div>
      {tab === "requirements" && <RequirementsTab scope={s} />}
      {tab === "estimate" && <EstimateTab scope={s} />}
      {tab === "changes" && <ChangesTab scope={s} />}
      {tab === "details" && <DetailsTab scope={s} />}
      {tab === "proposal" && <ProposalTab key={s.id} scope={s} reviewer={reviewer} onChanged={onChanged} />}
    </div>
  );
}

function ProjectView({ projectId, reviewer, onChanged }: { projectId: number; reviewer: string; onChanged: () => void }) {
  const project = useAsync(() => scopeApi<ProjectDetail>(`/projects/${projectId}`), [projectId]);
  const [scopeId, setScopeId] = useState<number | null>(null);
  const [adding, setAdding] = useState(false);
  const action = useAction();
  const p = project.data;

  useEffect(() => {
    if (p && (scopeId === null || !p.scopes.some((s) => s.id === scopeId))) setScopeId(p.scopes[0]?.id ?? null);
  }, [p, scopeId]);

  if (project.loading && !p) return <Loading />;
  if (!p) return <ErrorAlert error={project.error} onRetry={project.reload} />;
  const baseline = p.scopes.find((s) => s.status === "approved");

  const analyze = async (transcriptId: number) => {
    const r = await action.run(`analyze-${transcriptId}`, () => scopeApi<Scope>(`/projects/${projectId}/analyze`, { method: "POST", json: { transcript_id: transcriptId } }));
    if (r) {
      setScopeId(r.id);
      await project.reload();
      onChanged();
    }
  };
  const refresh = () => {
    void project.reload();
    onChanged();
  };

  return (
    <div className="stack">
      <div className="card">
        <div className="card-header">
          <div>
            <h2>{p.project.name}</h2>
            <p>
              Client: {p.project.client} · {baseline ? `approved baseline: v${baseline.version}` : "no approved baseline yet"}
            </p>
          </div>
          <button className="btn" onClick={() => setAdding(true)}>
            Add transcript
          </button>
        </div>
        <ErrorAlert error={action.error} />
        {action.busy?.startsWith("analyze") && (
          <div className="alert alert-info" style={{ margin: 12 }}>
            <span className="spinner" /> Agents at work: extracting requirements, checking each one against the Knowledge Brain, estimating… This can take a
            minute with a live model.
          </div>
        )}
        {p.transcripts.length === 0 ? (
          <Empty title="No transcripts">Add a meeting transcript (or a sample) to start.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Transcript</th>
                  <th>Type</th>
                  <th className="num">Length</th>
                  <th>Versions</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {p.transcripts.map((t) => {
                  const versions = p.scopes.filter((s) => s.transcript_id === t.id);
                  const blocked = t.kind === "follow_up" && !baseline;
                  return (
                    <tr key={t.id}>
                      <td>{t.title}</td>
                      <td>{t.kind === "follow_up" ? <Badge tone="amber">follow-up</Badge> : <Badge>initial</Badge>}</td>
                      <td className="num">{t.chars.toLocaleString()} chars</td>
                      <td>{versions.map((v) => `v${v.version}`).join(", ") || <span className="muted">–</span>}</td>
                      <td className="right">
                        <button
                          className="btn btn-sm btn-primary"
                          disabled={!!action.busy || blocked}
                          title={blocked ? "Approve a baseline scope first" : ""}
                          onClick={() => void analyze(t.id)}
                        >
                          {t.kind === "follow_up" ? "Compare with baseline" : "Analyse"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {p.scopes.length > 0 && (
        <div className="row">
          <span className="small muted">Scope versions:</span>
          {p.scopes.map((s) => (
            <button key={s.id} className={`btn btn-sm ${s.id === scopeId ? "btn-primary" : ""}`} onClick={() => setScopeId(s.id)}>
              v{s.version} · {s.status}
              {s.is_change ? " · change" : ""}
            </button>
          ))}
        </div>
      )}
      {scopeId && <ScopeView key={scopeId} scopeId={scopeId} reviewer={reviewer} onChanged={refresh} />}

      {p.audit.length > 0 && (
        <details className="card card-body">
          <summary>
            <strong>Audit trail</strong> <span className="muted small">({p.audit.length} events)</span>
          </summary>
          <div style={{ marginTop: 8 }}>
            {p.audit.map((a) => (
              <div key={a.id} className="small">
                {fmtDate(a.created_at)} · {a.entity} #{a.entity_id} · <strong>{label(a.action)}</strong>
                {a.actor ? ` by ${a.actor}` : ""}
                {a.note ? <span className="muted"> · {a.note}</span> : null}
              </div>
            ))}
          </div>
        </details>
      )}
      {adding && <AddTranscript projectId={projectId} onClose={() => setAdding(false)} onAdded={() => (setAdding(false), refresh())} />}
    </div>
  );
}

export default function ScopeBuilder({ reviewer }: { reviewer: string }) {
  const projects = useAsync(() => scopeApi<ProjectSummary[]>("/projects"), []);
  const [selected, setSelected] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const action = useAction();

  useEffect(() => {
    if (selected === null && projects.data?.[0]) setSelected(projects.data[0].id);
  }, [projects.data, selected]);

  const seed = async () => {
    const r = await action.run("seed", () => scopeApi<{ project_ids: number[] }>("/demo/seed", { method: "POST" }));
    await projects.reload();
    if (r?.project_ids[0]) setSelected(r.project_ids[0]);
  };

  return (
    <div className="scope-layout">
      <div className="card">
        <div className="card-header">
          <h2>Projects</h2>
          <button className="btn btn-sm btn-primary" onClick={() => setCreating(true)}>
            New
          </button>
        </div>
        <ErrorAlert error={projects.error ?? action.error} onRetry={projects.reload} />
        {projects.loading && !projects.data ? (
          <Loading />
        ) : !projects.data?.length ? (
          <Empty title="No projects yet">
            <p>Load the sample meetings (two clients, one follow-up that changes the scope).</p>
            <button className="btn btn-primary" disabled={!!action.busy} onClick={() => void seed()}>
              Load demo projects
            </button>
          </Empty>
        ) : (
          projects.data.map((p) => (
            <div key={p.id} className={`list-item ${selected === p.id ? "active" : ""}`} onClick={() => setSelected(p.id)}>
              <div style={{ fontWeight: 600 }}>{p.name}</div>
              <div className="small muted">{p.client}</div>
              <div className="row" style={{ gap: 4, marginTop: 4 }}>
                <Badge>{p.transcripts} transcripts</Badge>
                {p.baseline_version ? <Badge tone="green">baseline v{p.baseline_version}</Badge> : null}
                {p.approved_proposals ? <Badge tone="green">{p.approved_proposals} ready to send</Badge> : null}
              </div>
            </div>
          ))
        )}
      </div>
      {selected ? (
        <ProjectView key={selected} projectId={selected} reviewer={reviewer} onChanged={() => void projects.reload()} />
      ) : (
        <div className="card">
          <Empty title="Select or create a project" />
        </div>
      )}
      {creating && (
        <NewProject
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            setSelected(id);
            void projects.reload();
          }}
        />
      )}
    </div>
  );
}
