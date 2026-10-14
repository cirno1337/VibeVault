import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { api, getToken, setToken } from "./api";
import Dashboard from "./pages/Dashboard";
import EventDetail from "./pages/EventDetail";
import Jobs from "./pages/Jobs";
import { ProjectDetail, Projects } from "./pages/Projects";
import SearchPage from "./pages/Search";
import SessionDetail from "./pages/SessionDetail";
import Sessions from "./pages/Sessions";
import Sources from "./pages/Sources";

function Login({ onDone, initialError }: { onDone: () => void; initialError?: string }) {
  const [t, setT] = useState("");
  const [err, setErr] = useState(initialError ?? "");
  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    setToken(t.trim());
    try {
      const me = await api<{ scope: string }>("/me");
      if (me.scope !== "admin") { setToken(null); setErr("This is an ingest-only token. The UI needs the admin token."); return; }
      onDone();
    } catch (e) { setToken(null); setErr(e instanceof Error ? e.message : "Login failed"); }
  }
  return (
    <main className="login">
      <h1>VibeVault</h1>
      <p className="muted">Private archive of AI-assisted development. Sign in through your account, or enter the admin token (<code>VIBEVAULT_ADMIN_TOKEN</code>). It is kept in this tab's session storage only.</p>
      <form onSubmit={submit}>
        <label>Admin token<input type="password" autoComplete="off" value={t} onChange={(e) => setT(e.target.value)} autoFocus /></label>
        <button type="submit" disabled={!t.trim()}>Sign in</button>
        {err && <div className="error" role="alert">{err}</div>}
      </form>
    </main>
  );
}

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());
  const [checking, setChecking] = useState(!getToken());
  const [msg, setMsg] = useState("");
  // Behind the reverse proxy (Authelia login) the proxy supplies the credentials, so a request
  // without a stored token can already succeed; only then fall back to the token form.
  useEffect(() => {
    if (!checking) return;
    api<{ scope: string }>("/me")
      .then((me) => setAuthed(me.scope === "admin"))
      .catch(() => { /* not signed in: show the token form */ })
      .finally(() => setChecking(false));
  }, [checking]);
  useEffect(() => {
    const h = () => { const had = !!getToken(); setToken(null); setAuthed(false); if (had) setMsg("Session expired or token invalid."); };
    window.addEventListener("vibevault:unauthorized", h);
    return () => window.removeEventListener("vibevault:unauthorized", h);
  }, []);
  if (checking) return <main className="login"><p className="muted">Loading…</p></main>;
  if (!authed) return <Login onDone={() => { setMsg(""); setAuthed(true); }} initialError={msg} />;
  const link = ({ isActive }: { isActive: boolean }) => (isActive ? "active" : "");
  return (
    <div className="shell">
      <nav aria-label="Main">
        <div className="brand">▣ VibeVault</div>
        <NavLink to="/" end className={link}>Dashboard</NavLink>
        <NavLink to="/projects" className={link}>Projects</NavLink>
        <NavLink to="/sessions" className={link}>Sessions</NavLink>
        <NavLink to="/search" className={link}>Search</NavLink>
        <NavLink to="/jobs" className={link}>Processing</NavLink>
        <NavLink to="/sources" className={link}>Sources &amp; AI</NavLink>
        <button className="linklike" onClick={() => { setToken(null); setAuthed(false); }}>Sign out</button>
      </nav>
      <main>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/projects" element={<Projects />} />
          <Route path="/projects/:id" element={<ProjectDetail />} />
          <Route path="/sessions" element={<Sessions />} />
          <Route path="/sessions/:id" element={<SessionDetail />} />
          <Route path="/events/:id" element={<EventDetail />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/sources" element={<Sources />} />
          <Route path="*" element={<p>Not found.</p>} />
        </Routes>
      </main>
    </div>
  );
}
