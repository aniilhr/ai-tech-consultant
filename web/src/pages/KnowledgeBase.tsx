import { useEffect, useState } from "react";
import { brainApi, type Article, type BrainStats, type SyncRun } from "../api";
import { Badge, Empty, ErrorAlert, Loading, Stat, StatusBadge, fmtDate, useAction, useAsync } from "../ui";

function RunStats({ run }: { run: SyncRun }) {
  const s = run.stats ? (JSON.parse(run.stats) as Record<string, number | boolean | string[]>) : null;
  if (!s) return <span className="muted">–</span>;
  const warnings = (s.warnings as string[] | undefined) ?? [];
  return (
    <div className="small">
      {s.sitemap_urls} in sitemap · {s.new} new · {s.changed} changed · {s.removed} removed · {s.succeeded} fetched · {s.failed} failed ·{" "}
      {s.remaining} remaining · {s.images_described} screenshots · {s.chunks_embedded} embedded
      {warnings.map((w) => (
        <div key={w} className="muted">
          ⚠ {w}
        </div>
      ))}
    </div>
  );
}

function SyncPanel({ onDone }: { onDone: () => void }) {
  const [limit, setLimit] = useState(25);
  const [images, setImages] = useState(true);
  const runs = useAsync(() => brainApi<{ running: boolean; items: SyncRun[] }>("/v1/sync/runs?limit=8"), []);
  const action = useAction();
  const running = runs.data?.running ?? false;

  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => {
      void runs.reload().then(onDone);
    }, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running]);

  const start = async () => {
    await action.run("sync", () => brainApi("/v1/sync", { method: "POST", json: { limit: limit || null, describe_images: images, max_images: images ? 50 : 0 } }));
    await runs.reload();
  };

  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>Incremental sync</h2>
          <p>Reads the sitemap politely (about 1 request per second), fetches only new or changed articles and removes deleted ones. Safe to stop and resume.</p>
        </div>
      </div>
      <div className="card-body stack-sm">
        <div className="row">
          <div className="field" style={{ width: 160, marginBottom: 0 }}>
            <label htmlFor="limit">Articles this run</label>
            <input id="limit" type="number" min={1} max={5000} value={limit} onChange={(e) => setLimit(Number(e.target.value))} />
          </div>
          <label className="row small" style={{ marginTop: 18 }}>
            <input type="checkbox" checked={images} onChange={(e) => setImages(e.target.checked)} /> Describe screenshots with the vision model
          </label>
          <button className="btn btn-primary" style={{ marginTop: 18 }} disabled={running || !!action.busy} onClick={() => void start()}>
            {running ? (
              <>
                <span className="spinner" /> Sync running…
              </>
            ) : (
              "Run sync"
            )}
          </button>
        </div>
        <ErrorAlert error={action.error ?? runs.error} />
      </div>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Status</th>
              <th>Started</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {runs.data?.items.map((r) => (
              <tr key={r.id}>
                <td>#{r.id}</td>
                <td>
                  <StatusBadge status={r.status} />
                </td>
                <td className="nowrap">{fmtDate(r.started_at)}</td>
                <td>
                  <RunStats run={r} />
                  {r.error && <div className="small" style={{ color: "var(--red)" }}>{r.error}</div>}
                </td>
              </tr>
            ))}
            {!runs.data?.items.length && (
              <tr>
                <td colSpan={4} className="muted">
                  No sync has run yet. Start with 25 articles to verify the pipeline, then increase the batch.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function AddDocument({ onAdded }: { onAdded: () => void }) {
  const [title, setTitle] = useState("");
  const [text, setText] = useState("");
  const action = useAction();
  const save = async () => {
    const ok = await action.run("doc", () => brainApi("/v1/documents", { method: "POST", json: { title, text, category: "Custom" } }));
    if (ok) {
      setTitle("");
      setText("");
      onAdded();
    }
  };
  return (
    <div className="card">
      <div className="card-header">
        <div>
          <h2>Add internal knowledge</h2>
          <p>Policies or notes that are not in the public help center (any business can plug in its own knowledge).</p>
        </div>
      </div>
      <div className="card-body">
        <div className="field">
          <label>Title</label>
          <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Refund policy for annual plans" />
        </div>
        <div className="field">
          <label>Content</label>
          <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} />
        </div>
        <ErrorAlert error={action.error} />
        <button className="btn" disabled={title.length < 3 || text.length < 10 || !!action.busy} onClick={() => void save()}>
          {action.busy ? "Saving…" : "Add document"}
        </button>
      </div>
    </div>
  );
}

export default function KnowledgeBase() {
  const stats = useAsync(() => brainApi<BrainStats>("/v1/stats"), []);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(0);
  const articles = useAsync(
    () => brainApi<{ total: number; items: Article[] }>(`/v1/articles?limit=25&offset=${page * 25}&q=${encodeURIComponent(q)}&status=${status}`),
    [q, status, page],
  );
  const s = stats.data;
  const refresh = () => {
    void stats.reload();
    void articles.reload();
  };

  return (
    <div className="stack">
      <ErrorAlert error={stats.error} onRetry={stats.reload} />
      {s && (
        <div className="stats">
          <Stat label="Active articles" value={s.articles.active ?? 0} sub={`${s.articles.pending ?? 0} pending · ${s.articles.failed ?? 0} failed`} />
          <Stat label="Removed (stale)" value={s.articles.removed ?? 0} sub="no longer used in answers" />
          <Stat label="Chunks" value={s.chunks} sub={`${s.chunks_embedded} embedded (${s.llm.embeddings ?? "keyword search only"})`} />
          <Stat label="Screenshots" value={s.images.described ?? 0} sub={`${s.images.pending ?? 0} pending · ${s.images.skipped ?? 0} skipped`} />
          <Stat label="Expert answers" value={s.expert_answers} sub={`${s.documents} custom documents`} />
        </div>
      )}
      <div className="grid-2">
        <SyncPanel onDone={refresh} />
        <AddDocument onAdded={refresh} />
      </div>
      <div className="card">
        <div className="card-header">
          <h2>Articles</h2>
          <div className="row">
            <input type="text" placeholder="Search title or URL" value={q} onChange={(e) => (setPage(0), setQ(e.target.value))} style={{ width: 220 }} />
            <select value={status} onChange={(e) => (setPage(0), setStatus(e.target.value))} style={{ width: 130 }}>
              <option value="">All statuses</option>
              <option value="active">Active</option>
              <option value="pending">Pending</option>
              <option value="failed">Failed</option>
              <option value="removed">Removed</option>
            </select>
          </div>
        </div>
        <ErrorAlert error={articles.error} />
        {articles.loading && !articles.data ? (
          <Loading />
        ) : !articles.data?.items.length ? (
          <Empty title="No articles yet">Run a sync to read the help center.</Empty>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Category › Folder</th>
                  <th>Status</th>
                  <th className="num">Chunks</th>
                  <th className="num">Images</th>
                  <th>Fetched</th>
                </tr>
              </thead>
              <tbody>
                {articles.data.items.map((a) => (
                  <tr key={a.id}>
                    <td>
                      {a.url.startsWith("http") ? (
                        <a href={a.url} target="_blank" rel="noreferrer">
                          {a.title ?? a.url}
                        </a>
                      ) : (
                        a.title
                      )}
                      {a.source !== "helpcenter" && (
                        <>
                          {" "}
                          <Badge tone="purple">{a.source}</Badge>
                        </>
                      )}
                      {a.last_error && <div className="small" style={{ color: "var(--red)" }}>{a.last_error}</div>}
                    </td>
                    <td className="small">{[a.category, a.folder].filter(Boolean).join(" › ") || "–"}</td>
                    <td>
                      <StatusBadge status={a.status} />
                    </td>
                    <td className="num">{a.chunks}</td>
                    <td className="num">{a.images}</td>
                    <td className="nowrap small">{fmtDate(a.fetched_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="row-between card-body">
              <span className="muted small">
                {articles.data.total} articles · page {page + 1} of {Math.max(1, Math.ceil(articles.data.total / 25))}
              </span>
              <div className="row">
                <button className="btn btn-sm" disabled={page === 0} onClick={() => setPage(page - 1)}>
                  Previous
                </button>
                <button className="btn btn-sm" disabled={(page + 1) * 25 >= articles.data.total} onClick={() => setPage(page + 1)}>
                  Next
                </button>
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
