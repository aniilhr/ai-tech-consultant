import { useState } from "react";
import { brainApi, type AskResponse, type Escalation } from "../api";
import { Badge, Empty, ErrorAlert, Loading, Markdown, ModeBadge, fmtDate, useAction, useAsync } from "../ui";

const SUGGESTIONS = [
  "How do I add a new membership plan?",
  "How do I connect Stripe to accept payments?",
  "How do I point my own domain to my site?",
  "Can members leave reviews, and can I moderate them?",
  "Does the platform integrate with SAP ERP?",
];

function ImageThumb({ src, alt }: { src: string; alt: string }) {
  const [failed, setFailed] = useState(false);
  return failed ? <div className="img-missing">Image unavailable</div> : <img src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}

interface Turn {
  question: string;
  response?: AskResponse;
  error?: string;
}

function AnswerCard({ r }: { r: AskResponse }) {
  return (
    <div className="card answer">
      <div className="answer-meta">
        {r.answered ? <Badge tone="green">Answered from help center</Badge> : <Badge tone="amber">Not enough evidence</Badge>}
        <ModeBadge mode={r.llm.mode} model={r.llm.model} />
        <Badge title="Retrieval confidence (embedding similarity or keyword coverage)">confidence {Math.round(r.confidence * 100)}%</Badge>
        <Badge>{r.retrieval_mode === "hybrid" ? "semantic + keyword search" : "keyword search"}</Badge>
        {r.escalation_id && <Badge tone="purple">Escalated #{r.escalation_id}</Badge>}
        <span className="muted small">
          {r.latency_ms} ms{r.llm.cost_usd !== null ? ` · $${r.llm.cost_usd.toFixed(4)}` : ""}
        </span>
      </div>
      <Markdown text={r.answer} />
      {r.warnings.length > 0 && (
        <div className="alert alert-warn" style={{ marginTop: 10 }}>
          <ul>
            {r.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      )}
      {(r.answered ? r.citations : r.related).length > 0 && (
        <div className="citations">
          <div className="small muted">{r.answered ? "Sources" : "Closest passages found (not sufficient to answer)"}</div>
          {(r.answered ? r.citations : r.related).map((c) => (
            <div className="citation" key={c.article_id}>
              <div className="row-between">
                <span className="title">
                  {c.url ? (
                    <a href={c.url} target="_blank" rel="noreferrer">
                      {c.title}
                    </a>
                  ) : (
                    c.title
                  )}
                  {c.heading && <span className="muted"> › {c.heading}</span>}
                </span>
                {c.source !== "helpcenter" && <Badge tone="purple">{c.source === "expert" ? "Expert answer" : "Custom document"}</Badge>}
              </div>
              <div className="snippet">{c.snippet}</div>
            </div>
          ))}
        </div>
      )}
      {r.images.length > 0 && (
        <div className="images">
          {r.images.map((img) => (
            <a className="image-ref" key={img.image_id} href={img.url} target="_blank" rel="noreferrer" style={{ textDecoration: "none" }}>
              <ImageThumb src={img.url} alt={img.alt || img.article_title} />
              <div>{img.description ?? <span className="muted">Screenshot from “{img.article_title}” (not analysed yet)</span>}</div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

function Escalations({ reviewer }: { reviewer: string }) {
  const [status, setStatus] = useState("open");
  const { data, loading, error, reload } = useAsync(() => brainApi<{ items: Escalation[] }>(`/v1/escalations?status=${status}`), [status]);
  const [answering, setAnswering] = useState<number | null>(null);
  const [text, setText] = useState("");
  const action = useAction();

  const submit = async (id: number) => {
    const ok = await action.run(`answer-${id}`, () =>
      brainApi(`/v1/escalations/${id}/answer`, { method: "POST", json: { answer: text, expert_name: reviewer } }),
    );
    if (ok) {
      setAnswering(null);
      setText("");
      await reload();
    }
  };

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>Expert queue</h2>
          <p>Questions the Brain could not answer. Your answer becomes knowledge.</p>
        </div>
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 110 }}>
          <option value="open">Open</option>
          <option value="answered">Answered</option>
          <option value="dismissed">Dismissed</option>
        </select>
      </div>
      <ErrorAlert error={error ?? action.error} />
      {loading && !data ? (
        <Loading />
      ) : !data?.items.length ? (
        <Empty title={status === "open" ? "No open escalations" : "Nothing here yet"}>Unanswerable questions from chat, inbox and scoping appear here.</Empty>
      ) : (
        <div>
          {data.items.map((e) => (
            <div key={e.id} className="list-item" style={{ cursor: "default" }}>
              <div className="row-between">
                <strong>#{e.id}</strong>
                <span className="row">
                  <Badge>{e.source}</Badge>
                  {e.times_asked > 1 && <Badge tone="amber">asked {e.times_asked}×</Badge>}
                </span>
              </div>
              <div style={{ margin: "4px 0" }}>{e.question}</div>
              <div className="muted small">{fmtDate(e.created_at)}</div>
              {e.expert_answer && (
                <div className="quote">
                  {e.expert_answer} — {e.expert_name}
                </div>
              )}
              {e.status === "open" &&
                (answering === e.id ? (
                  <div className="stack-sm" style={{ marginTop: 8 }}>
                    <textarea rows={4} value={text} onChange={(ev) => setText(ev.target.value)} placeholder="Write the verified answer…" />
                    <div className="row">
                      <button className="btn btn-primary btn-sm" disabled={text.trim().length < 5 || !!action.busy} onClick={() => void submit(e.id)}>
                        {action.busy ? "Saving…" : "Save to knowledge"}
                      </button>
                      <button className="btn btn-sm" onClick={() => setAnswering(null)}>
                        Cancel
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="row" style={{ marginTop: 8 }}>
                    <button className="btn btn-sm" onClick={() => setAnswering(e.id)}>
                      Answer
                    </button>
                    <button
                      className="btn btn-sm btn-ghost"
                      onClick={() => void action.run("dismiss", () => brainApi(`/v1/escalations/${e.id}/dismiss`, { method: "POST" })).then(reload)}
                    >
                      Dismiss
                    </button>
                  </div>
                ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function KnowledgeChat({ reviewer }: { reviewer: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [escalationKey, setEscalationKey] = useState(0);

  const ask = async (q: string) => {
    const text = q.trim();
    if (text.length < 3 || busy) return;
    setQuestion("");
    setBusy(true);
    setTurns((t) => [...t, { question: text }]);
    try {
      const response = await brainApi<AskResponse>("/v1/ask", { method: "POST", json: { question: text, source: "chat" } });
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, response } : x)));
      if (response.escalation_id) setEscalationKey((k) => k + 1);
    } catch (e) {
      setTurns((t) => t.map((x, i) => (i === t.length - 1 ? { ...x, error: e instanceof Error ? e.message : String(e) } : x)));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="chat-layout">
      <div className="card">
        <div className="card-body chat-thread">
          {turns.length === 0 && (
            <Empty title="Ask anything about the product">
              <p>Answers come only from the help center and cite their sources. If the evidence is missing, the Brain says so and creates an expert task.</p>
              <div className="suggestions" style={{ justifyContent: "center", marginTop: 12 }}>
                {SUGGESTIONS.map((s) => (
                  <button key={s} className="chip" onClick={() => void ask(s)}>
                    {s}
                  </button>
                ))}
              </div>
            </Empty>
          )}
          {turns.map((t, i) => (
            <div key={i} className="stack-sm">
              <div className="msg-user">{t.question}</div>
              <div className="msg-bot">
                {t.response ? <AnswerCard r={t.response} /> : t.error ? <ErrorAlert error={t.error} /> : <Loading label="Searching the help center…" />}
              </div>
            </div>
          ))}
        </div>
        <form
          className="composer"
          onSubmit={(e) => {
            e.preventDefault();
            void ask(question);
          }}
        >
          <input type="text" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder="Ask a question about the product…" maxLength={2000} />
          <button className="btn btn-primary" disabled={busy || question.trim().length < 3}>
            {busy ? "Asking…" : "Ask"}
          </button>
        </form>
      </div>
      <Escalations key={escalationKey} reviewer={reviewer} />
    </div>
  );
}
