import { useEffect, useRef, useState, type ReactNode } from "react";
import { STATE_META, type Tone } from "../lib/format";
import type { InstanceState } from "../api/client";

/* —— 统一线宽 1.5px 的手绘 SVG 图标集 —— */
export function Icon({ name, size = 14 }: { name: string; size?: number }) {
  const p = { width: size, height: size, viewBox: "0 0 16 16", fill: "none",
    stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round" as const, strokeLinejoin: "round" as const,
    "aria-hidden": true };
  switch (name) {
    case "round":    return <svg {...p}><circle cx="8" cy="8" r="3.2" fill="currentColor" stroke="none" /></svg>;
    case "triangle": return <svg {...p}><path d="M8 3.4 13 12H3Z" fill="currentColor" stroke="none" /></svg>;
    case "cross":    return <svg {...p}><path d="M4.6 4.6l6.8 6.8M11.4 4.6l-6.8 6.8" /></svg>;
    case "hollow":   return <svg {...p}><circle cx="8" cy="8" r="3.4" /></svg>;
    case "half":     return <svg {...p}><circle cx="8" cy="8" r="3.4" /><path d="M8 4.6a3.4 3.4 0 0 1 0 6.8Z" fill="currentColor" stroke="none" /></svg>;
    case "check":    return <svg {...p}><path d="M3 8.5 6.3 11.8 13 4.6" /></svg>;
    case "copy":     return <svg {...p}><rect x="5.5" y="5.5" width="8" height="8" rx="1.2" /><path d="M10.5 3.2H3.7a1 1 0 0 0-1 1v6.8" /></svg>;
    case "eye":      return <svg {...p}><path d="M1.8 8S4.2 3.8 8 3.8 14.2 8 14.2 8 11.8 12.2 8 12.2 1.8 8 1.8 8Z" /><circle cx="8" cy="8" r="1.9" /></svg>;
    case "eye-off":  return <svg {...p}><path d="M2.4 4.9c-.4.5-1.6 2.1-1.6 3.1s2.5 4.2 7.2 4.2c1 0 1.9-.2 2.7-.5M6.2 4.1A6.8 6.8 0 0 1 8 3.8c3.8 0 6.2 4.2 6.2 4.2s-.5.9-1.4 1.9M3 3l10 10" /></svg>;
    case "pencil":   return <svg {...p}><path d="M11.2 2.9 13.1 4.8 5.3 12.6l-2.8.8.8-2.8Z" /></svg>;
    case "help":     return <svg {...p}><circle cx="8" cy="8" r="5.8" /><path d="M6.5 6.3c0-1 .7-1.7 1.6-1.7s1.5.7 1.5 1.5c0 1.3-1.5 1.3-1.5 2.7" /><circle cx="8" cy="11.6" r=".4" fill="currentColor" /></svg>;
    case "close":    return <svg {...p}><path d="M4 4l8 8M12 4l-8 8" /></svg>;
    case "chevron":  return <svg {...p}><path d="M5.8 3.6 10.2 8l-4.4 4.4" /></svg>;
    case "back":     return <svg {...p}><path d="M9.8 3.6 5.4 8l4.4 4.4" /></svg>;
    case "plus":     return <svg {...p}><path d="M8 3.2v9.6M3.2 8h9.6" /></svg>;
    case "refresh":  return <svg {...p}><path d="M12.8 8a4.8 4.8 0 1 1-1.5-3.5M12.8 2.6v2.4h-2.4" /></svg>;
    case "power":    return <svg {...p}><path d="M8 2.6v5" /><path d="M4.4 4.6a4.8 4.8 0 1 0 7.2 0" /></svg>;
    case "search":   return <svg {...p}><circle cx="7" cy="7" r="4.4" /><path d="M10.4 10.4 14 14" /></svg>;
    case "release":  return <svg {...p}><path d="M8 2.8v6.6" /><path d="M5.4 6.8 8 9.4l2.6-2.6" /><path d="M3 12.6h10" /></svg>;
    default:         return null;
  }
}

export function StateGlyph({ state }: { state: InstanceState }) {
  return <Icon name={STATE_META[state]?.glyph ?? "hollow"} size={12} />;
}

/** 印章式状态标签：直角细框 + 宽字距 + 几何点（色 + 形 + 文案三重冗余）。 */
export function StateChip({ state, size = "md" }: { state: InstanceState; size?: "sm" | "md" }) {
  const m = STATE_META[state] ?? { zh: state, tone: "wait" as Tone, glyph: "hollow" };
  return (
    <span className={`chip tone-${m.tone}${size === "sm" ? " chip-sm" : ""}`}>
      <StateGlyph state={state} />
      {m.zh}
    </span>
  );
}

export function ToneChip({ tone, children }: { tone: Tone; children: ReactNode }) {
  return <span className={`chip tone-${tone}`}>{children}</span>;
}

/** 一键复制：成功后按钮文案变化 + 绿色成功态 1.6s（§9 明确反馈）。 */
export function CopyButton({ text, label = "复制", big = false, title, provide }: {
  text: string | null; label?: string; big?: boolean; title?: string;
  provide?: () => Promise<string | null>;
}) {
  const [ok, setOk] = useState(false);
  const [fail, setFail] = useState(false);
  const [busy, setBusy] = useState(false);
  const timer = useRef<number>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    // 打码串绝不能进剪贴板：屏上显示的是 aftb…FFI，复制出来也必须是 aftb…FFI
    // 那种事绝对不能发生（他会把剪贴板里的东西直接粘给对方）。所以没有明文时
    // 先走 provide 现取一次，取不到就什么都不做，而不是把打码串塞出去。
    let val = text;
    if (!val && provide) {
      setBusy(true);
      try { val = await provide(); } finally { setBusy(false); }
    }
    if (!val) return;
    let done = false;
    try {
      await navigator.clipboard.writeText(val);
      done = true;
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = val; ta.style.position = "fixed"; ta.style.opacity = "0";
        document.body.appendChild(ta); ta.select();
        done = document.execCommand("copy");
        ta.remove();
      } catch { done = false; }
    }
    setOk(done); setFail(!done);
    clearTimeout(timer.current);
    timer.current = window.setTimeout(() => { setOk(false); setFail(false); }, 1800);
  };
  return (
    <button type="button"
      className={`btn btn-copy${big ? " btn-lg" : ""}${ok ? " is-ok" : ""}${fail ? " is-fail" : ""}`}
      onClick={copy} disabled={(!text && !provide) || busy} title={title}>
      <Icon name={ok ? "check" : fail ? "cross" : "copy"} />
      {fail ? "复制失败" : ok ? "已复制" : busy ? "现取中…" : label}
    </button>
  );
}

/** 按住确认：1.5s 长按触发，松手即取消；按钮上是按压进度。 */
export function HoldButton({ ms = 1500, disabled, onHold, children, holdLabel = "继续按住…" }: {
  ms?: number; disabled?: boolean; onHold: () => void; children: ReactNode; holdLabel?: string;
}) {
  const [holding, setHolding] = useState(false);
  const [busy, setBusy] = useState(false);
  const startAt = useRef(0);
  const raf = useRef(0);
  const done = useRef(false);
  const barRef = useRef<HTMLSpanElement>(null);
  const setP = (v: number | null) => {
    const el = barRef.current; if (!el) return;
    if (v == null) el.style.transform = "scaleX(0)";
    else el.style.transform = `scaleX(${v})`;
  };
  const stop = () => {
    cancelAnimationFrame(raf.current);
    setP(null); setHolding(false); done.current = false;
    const el = barRef.current;
    if (el) el.style.transition = "none";
  };
  const start = () => {
    if (disabled || busy || done.current) return;
    done.current = false;
    startAt.current = performance.now();
    const el = barRef.current;
    if (el) { el.style.transition = "none"; setP(0); }
    setHolding(true);
    const step = (now: number) => {
      if (done.current) return;
      const p = Math.min(1, (now - startAt.current) / ms);
      setP(p);
      if (p >= 1) {
        done.current = true;
        setHolding(false);
        setBusy(true);
        setP(null);
        onHold();
        return;
      }
      raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
  };
  useEffect(() => () => cancelAnimationFrame(raf.current), []);
  return (
    <button type="button"
      className={`btn btn-danger btn-hold${holding ? " is-holding" : ""}`}
      disabled={disabled || busy}
      onPointerDown={(e) => { if (e.button === 0) start(); }}
      onPointerUp={stop} onPointerLeave={stop} onPointerCancel={stop}
      onKeyDown={(e) => { if ((e.key === " " || e.key === "Enter") && !e.repeat) { e.preventDefault(); start(); } }}
      onKeyUp={(e) => { if (e.key === " " || e.key === "Enter") stop(); }}
      aria-label="按住 1.5 秒确认释放">
      <span className="hold-bar" ref={barRef} aria-hidden />
      <span className="hold-label">{holding ? holdLabel : busy ? "已签字，正在执行…" : children}</span>
    </button>
  );
}

/** 单层弹窗：原生 dialog，Esc 关闭、焦点收拢都是浏览器原生行为（不弹窗套弹窗）。 */
export function Modal({ title, onClose, children, wide = false }: {
  title: string; onClose: () => void; children: ReactNode; wide?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    if (d && !d.open) d.showModal();
  });
  return (
    <dialog ref={ref} className={`modal${wide ? " modal-wide" : ""}`}
      onClick={(e) => { if (e.target === ref.current) onClose(); }}
      onCancel={(e) => { e.preventDefault(); onClose(); }}>
      <div className="modal-head">
        <h2>{title}</h2>
        <button type="button" className="btn btn-icon" onClick={onClose} aria-label="关闭（Esc）">
          <Icon name="close" />
        </button>
      </div>
      <div className="modal-body">{children}</div>
    </dialog>
  );
}
