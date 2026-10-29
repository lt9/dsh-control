import { useCallback, useEffect, useRef, useState } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";
import { api } from "./api/client";
import { fmtFull } from "./lib/format";
import LoginPage from "./pages/LoginPage";
import { Icon } from "./components/ui";

/** 顶栏右侧的账户下拉。这颗面板还没有个人中心，所以头像点开来是
 *  「这次登录还有多久 + 设置 + 退出」——站点级入口不该跟台账/新建并排在业务区里。 */
function AccountMenu({ expires, onLogout }: { expires: string | null; onLogout: () => void }) {
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const btnRef = useRef<HTMLButtonElement>(null);
  const loc = useLocation();
  const onSettings = loc.pathname === "/settings";

  useEffect(() => {
    if (!open) return;
    // 外点即收；Esc 收完把焦点还给账户按钮——键盘用户不该被丢在空气里
    const away = (e: PointerEvent) => { if (!boxRef.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { setOpen(false); btnRef.current?.focus(); return; }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const items = [...(boxRef.current?.querySelectorAll<HTMLElement>("[role=menuitem]") ?? [])];
      if (!items.length) return;
      e.preventDefault();
      const at = items.indexOf(document.activeElement as HTMLElement);
      const next = e.key === "ArrowDown"
        ? (at < 0 ? 0 : (at + 1) % items.length)
        : (at <= 0 ? items.length - 1 : at - 1);
      items[next].focus();
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("pointerdown", away); document.removeEventListener("keydown", key); };
  }, [open]);

  return (
    <div className="acct" ref={boxRef}>
      <button type="button" ref={btnRef} className={`acct-btn${open ? " on" : ""}`}
        aria-haspopup="menu" aria-expanded={open} aria-label="账户菜单"
        onClick={() => setOpen((o) => !o)}>
        <Icon name="person" size={15} />
      </button>
      {open && (
        <div className="acct-menu" role="menu" aria-label="账户">
          <div className="acct-head">
            {expires ? <>登录态到 <b className="mono">{fmtFull(expires).slice(0, 16)}</b></> : "登录态有效"}
          </div>
          <a href="/settings" role="menuitem" className={`acct-item${onSettings ? " on" : ""}`}
            aria-current={onSettings ? "page" : undefined} onClick={() => setOpen(false)}>
            <Icon name="gear" size={13} />设置
          </a>
          <div className="acct-sep" role="separator" />
          <button type="button" role="menuitem" className="acct-item" onClick={onLogout}>退出</button>
        </div>
      )}
    </div>
  );
}

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

  const signOut = async () => {
    try { await api.logout(); } catch { /* 尽力而为 */ }
    setAuth("out"); nav("/login");
  };

  return (
    <>
      <header className="topbar">
        <a className="wordmark" href="/"><b>dsh<i>·</i>control</b><span>实例控制台账</span></a>
        <nav className="topnav" aria-label="主导航">
          <a href="/" className={loc.pathname === "/" ? "on" : ""}>台账</a>
          <a href="/instances/new" className={loc.pathname === "/instances/new" ? "on" : ""}>新建</a>
          <a href="/memory" className={loc.pathname === "/memory" ? "on" : ""}>记忆</a>
          <a href="/capabilities" className={loc.pathname === "/capabilities" ? "on" : ""}>能力</a>
        </nav>
        <div className="topbar-right">
          <AccountMenu expires={expires} onLogout={() => void signOut()} />
        </div>
      </header>
      <Outlet />
    </>
  );
}
