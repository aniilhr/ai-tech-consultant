import { useEffect, useState } from "react";
import { Icon, useReviewer } from "./ui";
import Overview from "./pages/Overview";
import KnowledgeChat from "./pages/KnowledgeChat";
import KnowledgeBase from "./pages/KnowledgeBase";
import Inbox from "./pages/Inbox";
import Learning from "./pages/Learning";
import ScopeBuilder from "./pages/ScopeBuilder";

const ROUTES = [
  { path: "overview", label: "Overview", icon: "home", section: "Workspace", title: "Overview", sub: "Health and activity across the three modules" },
  { path: "chat", label: "Knowledge Chat", icon: "chat", section: "Knowledge Brain", title: "Knowledge Chat", sub: "Ask the help center. Every answer cites its sources; gaps go to an expert." },
  { path: "kb", label: "Knowledge Base", icon: "book", section: "Knowledge Brain", title: "Knowledge Base", sub: "Ingestion, freshness and the articles the Brain knows" },
  { path: "inbox", label: "Inbox Review", icon: "inbox", section: "Inbox Agent", title: "Inbox Review", sub: "AI drafts grounded in the Knowledge Brain. A human approves every reply." },
  { path: "learning", label: "Learning & Trust", icon: "spark", section: "Inbox Agent", title: "Learning & Trust", sub: "How human edits improve the agent, and which email kinds have earned autonomy" },
  { path: "scope", label: "Scope Builder", icon: "doc", section: "Meetings to Scope", title: "Scope Builder", sub: "Meeting transcripts to scope of work, estimate and proposal, with human approval" },
] as const;

function useHashRoute(): string {
  const read = () => window.location.hash.replace(/^#\/?/, "").split("?")[0] || "overview";
  const [route, setRoute] = useState(read);
  useEffect(() => {
    const onChange = () => setRoute(read());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export default function App() {
  const route = useHashRoute();
  const [reviewer, setReviewer] = useReviewer();
  const current = ROUTES.find((r) => r.path === route) ?? ROUTES[0];
  let section = "";

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 18 12 6l7 12" />
            </svg>
          </div>
          <div>
            AI Tech Consultant
            <small>Support · Knowledge · Scoping</small>
          </div>
        </div>
        <nav className="nav">
          {ROUTES.map((r) => {
            const header = r.section !== section ? <div className="nav-section">{(section = r.section)}</div> : null;
            return (
              <div key={r.path}>
                {header}
                <a href={`#/${r.path}`} className={current.path === r.path ? "active" : ""}>
                  <Icon name={r.icon} /> {r.label}
                </a>
              </div>
            );
          })}
        </nav>
        <div className="sidebar-footer">
          <label htmlFor="reviewer">Reviewing as</label>
          <input id="reviewer" value={reviewer} onChange={(e) => setReviewer(e.target.value)} />
        </div>
      </aside>
      <div className="main">
        <nav className="mobile-nav">
          {ROUTES.map((r) => (
            <a key={r.path} href={`#/${r.path}`} className={current.path === r.path ? "active" : ""}>
              {r.label}
            </a>
          ))}
        </nav>
        <header className="topbar">
          <div>
            <h1>{current.title}</h1>
            <p>{current.sub}</p>
          </div>
        </header>
        <main className="content">
          {current.path === "overview" && <Overview />}
          {current.path === "chat" && <KnowledgeChat reviewer={reviewer} />}
          {current.path === "kb" && <KnowledgeBase />}
          {current.path === "inbox" && <Inbox reviewer={reviewer} />}
          {current.path === "learning" && <Learning />}
          {current.path === "scope" && <ScopeBuilder reviewer={reviewer} />}
        </main>
      </div>
    </div>
  );
}
