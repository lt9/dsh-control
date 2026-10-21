import { useCallback, useEffect, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api/client";
import { fmtFull } from "./lib/format";
import LoginPage from "./pages/LoginPage";

export default function App() {
  const [auth, setAuth] = useState<"unknown" | "in" | "out">("unknown");
  const [expires, setExpires] = useState<string | null>(null);
  const nav = useNavigate();
  const loc = useLocation();

  const probe = useCallback(async () => {
    try {
      const s = await api.session();
      setAuth(s.ok ? "in" : "out");
      setExpires(s.expires_at ?? null);
    } catch {
      setAuth("out");
    }
  }, []);

  useEffect(() => { void probe(); }, [probe]);
  useEffect(() => {
    const off = () => { setAuth("out"); if (loc.pathname !== "/login") nav("/login"); };
    window.addEventListener("dshc:unauthorized", off);
    return () => window.removeEventListener("dshc:unauthorized", off);
  }, [nav, loc.pathname]);

  if (auth === "unknown") {
    return <div className="shell" aria-busy="true">
      <div className="skeleton sk-line" style={{ width: "38%" }} />
      <div className="skeleton sk-line" />
      <div className="skeleton sk-line" style={{ width: "72%" }} />
    </div>;
  }
  if (auth === "out") {
    return loc.pathname === "/login"
      ? <LoginPage onDone={() => { void probe(); nav("/"); }} />
      : <Navigate to="/login" replace />;
  }
  if (loc.pathname === "/login") return <Navigate to="/" replace />;

  return (
    <>
      <header className="topbar">
        <a className="wordmark" href="/"><b>dsh<i>·</i>control</b><span>实例控制台账</span></a>
        <nav className="topnav" aria-label="主导航">
          <a href="/" className={loc.pathname === "/" ? "on" : ""}>台账</a>
          <a href="/instances/new" className={loc.pathname === "/instances/new" ? "on" : ""}>新建</a>
        </nav>
        <div className="topbar-right">
          {expires && <span>登录态到 {fmtFull(expires).slice(0, 16)}</span>}
          <button type="button" className="link-dark" onClick={async () => {
            try { await api.logout(); } catch { /* 尽力而为 */ }
            setAuth("out"); nav("/login");
          }}>退出</button>
        </div>
      </header>
      <Outlet />
    </>
  );
}
