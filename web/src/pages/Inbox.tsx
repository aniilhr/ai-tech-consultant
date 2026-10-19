import { useEffect, useState } from "react";
import { inboxApi, type EmailDetail, type EmailListItem } from "../api";
import { Badge, Empty, ErrorAlert, Loading, Modal, ModeBadge, Progress, StatusBadge, fmtDate, label, pct, useAction, useAsync } from "../ui";

const CATEGORIES = [
  "how_to", "technical_issue", "billing_question", "refund_request", "cancellation", "account_access",
  "feature_request", "new_project_quote", "complaint", "follow_up", "sales_inquiry", "other",
];

function NewEmail({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const [form, setForm] = useState({ from_name: "", from_email: "", subject: "", body: "" });
  const action = useAction();
  const set = (k: keyof typeof form) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const submit = async () => {
    const r = await action.run("create", () => inboxApi<EmailDetail>("/emails", { method: "POST", json: form }));
    if (r) onCreated(r.email.id);
  };
  return (
    <Modal title="Add a customer email" onClose={onClose}>
      <div className="grid-2">
        <div className="field">
          <label>Customer name</label>
          <input type="text" value={form.from_name} onChange={set("from_name")} />
        </div>
        <div className="field">
          <label>Customer email</label>
          <input type="email" value={form.from_email} onChange={set("from_email")} />
        </div>
      </div>
      <div className="field">
        <label>Subject</label>
        <input type="text" value={form.subject} onChange={set("subject")} />
      </div>
      <div className="field">
        <label>Message</label>
        <textarea rows={8} value={form.body} onChange={set("body")} />
      </div>
      <ErrorAlert error={action.error} />
      <div className="row" style={{ justifyContent: "flex-end", marginTop: 8 }}>
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button className="btn btn-primary" disabled={!!action.busy} onClick={() => void submit()}>
          {action.busy ? "Adding…" : "Add email"}
        </button>
      </div>
    </Modal>
  );
}

function EvidencePanel({ detail }: { detail: EmailDetail }) {
  const ev = detail.drafts[0]?.evidence;
  if (!ev) return null;
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h3>Knowledge Brain evidence</h3>
          <p>{ev.question ? <>Asked: “{ev.question.slice(0, 160)}{ev.question.length > 160 ? "…" : ""}”</> : "No product facts needed for this email"}</p>
        </div>
        {ev.question &&
          (!ev.available ? (
            <Badge tone="red">Brain unreachable</Badge>
          ) : ev.answered ? (
            <Badge tone="green">Answered</Badge>
          ) : (
            <Badge tone="amber">Insufficient evidence</Badge>
          ))}
      </div>
      {ev.question && (
        <div className="card-body stack-sm">
          {ev.error && <div className="alert alert-error">{ev.error}</div>}
          {ev.answer && <div className="small pre">{ev.answer}</div>}
          {ev.escalation_id && <Badge tone="purple">Escalated to expert #{ev.escalation_id}</Badge>}
          {ev.citations.map((c) => (
            <div className="citation" key={c.title}>
              <div className="title">{c.url ? <a href={c.url} target="_blank" rel="noreferrer">{c.title}</a> : c.title}</div>
              <div className="snippet">{c.snippet}</div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function DraftPanel({ detail, reviewer, onChange }: { detail: EmailDetail; reviewer: string; onChange: (d: EmailDetail) => void }) {
  const draft = detail.drafts[0];
  const [body, setBody] = useState(draft?.body ?? "");
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState("");
  const [ownReply, setOwnReply] = useState(false);
  const action = useAction();
  useEffect(() => {
    setBody(draft?.body ?? "");
    setRejecting(false);
    setOwnReply(false);
  }, [draft?.id, draft?.body]);

  const id = detail.email.id;
  const status = detail.email.status;
  const call = async (key: string, path: string, json?: unknown) => {
    const r = await action.run(key, () => inboxApi<EmailDetail>(path, { method: "POST", json }));
    if (r) onChange(r);
  };
  const edited = draft ? body.trim() !== draft.body.trim() : false;
  const done = ["approved", "replied_manually", "auto_sent"].includes(status);

  if (!draft && !ownReply) {
    return (
      <div className="card">
        <div className="card-body">
          <Empty title={status === "new" ? "No draft yet" : "No draft"}>
            <p>The agent will classify the email, ask the Knowledge Brain for facts, then write a draft for your review.</p>
            <div className="row" style={{ justifyContent: "center" }}>
              <button className="btn btn-primary" disabled={!!action.busy} onClick={() => void call("process", `/emails/${id}/process`)}>
                {action.busy === "process" ? (
                  <>
                    <span className="spinner" /> Drafting…
                  </>
                ) : (
                  "Generate draft"
                )}
              </button>
              {!done && (
                <button className="btn" onClick={() => setOwnReply(true)}>
                  Write my own reply
                </button>
              )}
            </div>
          </Empty>
          <ErrorAlert error={action.error} />
        </div>
      </div>
    );
  }

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h3>{ownReply ? "Your reply" : "Draft reply"}</h3>
          {draft && !ownReply && (
            <p>
              {draft.subject} · prompt {draft.prompt_version}
              {draft.rules_version ? ` · learned rules v${draft.rules_version}` : ""}
            </p>
          )}
        </div>
        {draft && !ownReply && (
          <div className="row">
            <ModeBadge mode={draft.llm.mode} model={draft.llm.model} />
            <Badge>confidence {pct(draft.confidence)}</Badge>
          </div>
        )}
      </div>
      <div className="card-body stack-sm">
        {draft?.llm.warning && <div className="alert alert-warn">{draft.llm.warning}</div>}
        {draft && draft.policy.violations.length > 0 && (
          <div className="alert alert-error">
            <div>
              <strong>Policy guard:</strong> this draft makes commitments a human must check.
              <ul>
                {draft.policy.violations.map((v) => (
                  <li key={v}>{v}</li>
                ))}
              </ul>
            </div>
          </div>
        )}
        {draft && draft.needs_human.length > 0 && (
          <div className="alert alert-warn">
            <div>
              <strong>Check before sending:</strong>
              <ul>
                {draft.needs_human.map((v) => (
                  <li key={v}>{v}</li>
                ))}
              </ul>
            </div>
          </div>
        )}
        <textarea className="draft-editor" value={body} onChange={(e) => setBody(e.target.value)} disabled={done} rows={14} />
        {edited && !ownReply && <div className="small muted">Edited: your changes are stored and used to improve future drafts. This resets the trust streak for this kind.</div>}
        <ErrorAlert error={action.error} />
        {done ? (
          <div className="alert alert-success">
            {status === "auto_sent" ? "Sent automatically (trust earned)." : "Reply approved and placed in the outbox."} Email sending is simulated; nothing left
            this system.
            {status === "auto_sent" && (
              <button className="btn btn-sm btn-danger" style={{ marginLeft: "auto" }} onClick={() => void call("flag", `/emails/${id}/flag`, { reason: "Reviewer flagged auto-sent reply", reviewer })}>
                Flag as wrong
              </button>
            )}
          </div>
        ) : rejecting ? (
          <div className="stack-sm">
            <input type="text" placeholder="Why is this draft wrong?" value={reason} onChange={(e) => setReason(e.target.value)} />
            <div className="row">
              <button className="btn btn-danger" disabled={reason.trim().length < 3 || !!action.busy} onClick={() => void call("reject", `/emails/${id}/reject`, { draft_id: draft?.id, reason, reviewer })}>
                Reject draft
              </button>
              <button className="btn" onClick={() => setRejecting(false)}>
                Cancel
              </button>
            </div>
          </div>
        ) : ownReply ? (
          <div className="row">
            <button className="btn btn-success" disabled={body.trim().length < 5 || !!action.busy} onClick={() => void call("own", `/emails/${id}/own-reply`, { body, reviewer })}>
              Send my reply
            </button>
            <button className="btn" onClick={() => setOwnReply(false)}>
              Cancel
            </button>
          </div>
        ) : (
          <div className="row">
            <button
              className="btn btn-success"
              disabled={!!action.busy || status !== "drafted" || body.trim().length < 5}
              onClick={() => void call("approve", `/emails/${id}/approve`, { draft_id: draft?.id, final_body: body, reviewer })}
            >
              {edited ? "Approve edited reply" : "Approve & send"}
            </button>
            {status === "drafted" && (
              <button className="btn btn-danger" onClick={() => setRejecting(true)}>
                Reject
              </button>
            )}
            <button className="btn" disabled={!!action.busy} onClick={() => void call("process", `/emails/${id}/process`)}>
              {action.busy === "process" ? "Regenerating…" : "Regenerate"}
            </button>
            <button className="btn btn-ghost" onClick={() => (setOwnReply(true), setBody(""))}>
              Write my own
            </button>
          </div>
        )}
        {status === "rejected" && <div className="small muted">Draft rejected. Regenerate or write your own reply.</div>}
      </div>
      {detail.kind_status && (
        <div className="card-body" style={{ borderTop: "1px solid var(--border)" }}>
          <div className="row-between small">
            <span>
              Trust for <strong>{label(detail.kind_status.kind)}</strong>: {detail.kind_status.streak}/{detail.kind_status.required} unchanged approvals in a row
            </span>
            {detail.kind_status.blocked_reason ? <Badge tone="red">Always human</Badge> : detail.kind_status.eligible ? <Badge tone="green">Eligible</Badge> : <Badge>Learning</Badge>}
          </div>
          <div style={{ marginTop: 6 }}>
            <Progress value={detail.kind_status.streak} max={detail.kind_status.required} />
          </div>
          {detail.auto_send && !detail.auto_send.allowed && (
            <details className="small muted" style={{ marginTop: 6 }}>
              <summary>Why this was not sent automatically</summary>
              <ul>
                {detail.auto_send.reasons.map((r) => (
                  <li key={r}>{r}</li>
                ))}
              </ul>
            </details>
          )}
        </div>
      )}
      {detail.reviews.length > 0 && (
        <div className="card-body" style={{ borderTop: "1px solid var(--border)" }}>
          <h3 style={{ marginBottom: 6 }}>History</h3>
          {detail.reviews.map((r) => (
            <div key={r.id} className="small" style={{ marginBottom: 4 }}>
              <StatusBadge status={r.action} /> {r.reviewer} · {fmtDate(r.created_at)}
              {r.action === "approved" && (r.unchanged ? " · unchanged" : ` · edited ${pct(r.edit_ratio)} of words`)}
              {r.reason && <span className="muted"> · {r.reason}</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function Inbox({ reviewer }: { reviewer: string }) {
  const [status, setStatus] = useState("");
  const [category, setCategory] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const list = useAsync(() => inboxApi<EmailListItem[]>(`/emails?status=${status}&category=${category}&q=${encodeURIComponent(q)}`), [status, category, q]);
  const detail = useAsync(() => (selected ? inboxApi<EmailDetail>(`/emails/${selected}`) : Promise.resolve(null)), [selected]);
  const batch = useAction();

  useEffect(() => {
    if (!selected && list.data?.[0]) setSelected(list.data[0].id);
  }, [list.data, selected]);

  const onChange = (d: EmailDetail) => {
    detail.setData(d);
    void list.reload();
  };

  return (
    <div className="stack">
      <div className="row-between">
        <div className="row">
          <input type="text" placeholder="Search emails" value={q} onChange={(e) => setQ(e.target.value)} style={{ width: 200 }} />
          <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 150 }}>
            <option value="">All statuses</option>
            {["new", "drafted", "approved", "rejected", "replied_manually", "auto_sent"].map((s) => (
              <option key={s} value={s}>
                {label(s)}
              </option>
            ))}
          </select>
          <select value={category} onChange={(e) => setCategory(e.target.value)} style={{ width: 170 }}>
            <option value="">All categories</option>
            {CATEGORIES.map((c) => (
              <option key={c} value={c}>
                {label(c)}
              </option>
            ))}
          </select>
        </div>
        <div className="row">
          <button
            className="btn"
            disabled={!!batch.busy}
            onClick={() => void batch.run("batch", () => inboxApi("/emails/process-batch", { method: "POST", json: { limit: 5 } })).then(() => list.reload())}
          >
            {batch.busy ? (
              <>
                <span className="spinner" /> Drafting 5…
              </>
            ) : (
              "Draft next 5 new emails"
            )}
          </button>
          <button className="btn btn-primary" onClick={() => setCreating(true)}>
            Add email
          </button>
        </div>
      </div>
      <ErrorAlert error={batch.error ?? list.error} onRetry={list.reload} />
      <div className="inbox-layout">
        <div className="card mail-list">
          {list.loading && !list.data ? (
            <Loading />
          ) : !list.data?.length ? (
            <Empty title="No emails">Try another filter, or add one.</Empty>
          ) : (
            list.data.map((m) => (
              <div key={m.id} className={`mail-item ${selected === m.id ? "active" : ""}`} onClick={() => setSelected(m.id)}>
                <div className="row-between">
                  <span className="from">{m.from_name}</span>
                  <span className="muted small nowrap">{fmtDate(m.received_at)}</span>
                </div>
                <div className="subject">{m.subject}</div>
                <div className="preview">{m.preview}</div>
                <div className="row" style={{ marginTop: 6, gap: 4 }}>
                  <StatusBadge status={m.status} />
                  {m.category && <Badge>{label(m.category)}</Badge>}
                  {m.urgency && ["urgent", "high"].includes(m.urgency) && <StatusBadge status={m.urgency} />}
                  {m.risk_flags.length > 0 && <Badge tone="red">{m.risk_flags.length} risk</Badge>}
                </div>
              </div>
            ))
          )}
        </div>

        {detail.loading && !detail.data ? (
          <div className="card">
            <Loading />
          </div>
        ) : !detail.data ? (
          <div className="card">
            <Empty title="Select an email" />
          </div>
        ) : (
          <>
            <div className="stack">
              <div className="card">
                <div className="card-header">
                  <div>
                    <h2>{detail.data.email.subject}</h2>
                    <p>
                      {detail.data.email.from_name} &lt;{detail.data.email.from_email}&gt; · {fmtDate(detail.data.email.received_at)}
                    </p>
                  </div>
                  <StatusBadge status={detail.data.email.status} />
                </div>
                <div className="card-body">
                  <div className="mail-body">{detail.data.email.body}</div>
                </div>
              </div>
              {detail.data.triage && (
                <div className="card">
                  <div className="card-header">
                    <h3>Triage</h3>
                    <ModeBadge mode={detail.data.triage.llm.mode} model={detail.data.triage.llm.model} />
                  </div>
                  <div className="card-body">
                    <dl className="kv">
                      <dt>Category</dt>
                      <dd>
                        <Badge tone="blue">{label(detail.data.triage.category)}</Badge>
                        {detail.data.email.expected && detail.data.email.expected.category !== detail.data.triage.category && (
                          <span className="small muted"> (labelled: {label(detail.data.email.expected.category)})</span>
                        )}
                      </dd>
                      <dt>Urgency</dt>
                      <dd>
                        <StatusBadge status={detail.data.triage.urgency} />
                      </dd>
                      <dt>Risk</dt>
                      <dd className="row" style={{ gap: 4 }}>
                        {detail.data.triage.risk_flags.length ? detail.data.triage.risk_flags.map((r) => <Badge key={r} tone="red">{label(r)}</Badge>) : <span className="muted">none</span>}
                      </dd>
                      <dt>Sentiment</dt>
                      <dd>{detail.data.triage.sentiment}</dd>
                      <dt>Summary</dt>
                      <dd>{detail.data.triage.summary}</dd>
                    </dl>
                    {detail.data.triage.llm.warning && <div className="alert alert-warn" style={{ marginTop: 10 }}>{detail.data.triage.llm.warning}</div>}
                  </div>
                </div>
              )}
              <EvidencePanel detail={detail.data} />
            </div>
            <DraftPanel detail={detail.data} reviewer={reviewer} onChange={onChange} />
          </>
        )}
      </div>
      {creating && (
        <NewEmail
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            setSelected(id);
            void list.reload();
          }}
        />
      )}
    </div>
  );
}
