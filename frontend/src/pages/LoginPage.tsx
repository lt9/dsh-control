import { useState } from "react";
import { api } from "../api/client";
import { Icon } from "../components/ui";

export default function LoginPage({ onDone }: { onDone: () => void }) {
  const [key, setKey] = useState("");
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy || !key) return;
    setBusy(true);
    setError(null);
    try {
      await api.login(key);
      onDone();
    } catch (err) {
      // 失败绝不清空已粘贴内容（§9 登录页要求）
      setError(err instanceof Error ? err.message : "登录失败");
      setBusy(false);
    }
  };

  return (
    <div className="gate">
      <form className="gate-card" onSubmit={submit}>
        <div className="wordmark"><b>dsh<i>·</i>control</b><span>实例控制台账</span></div>
        <label className="field-label" htmlFor="opkey">运维密钥</label>
        <div className="gate-input">
          <input id="opkey" type={show ? "text" : "password"} value={key}
            onChange={(e) => setKey(e.target.value)}
            onPaste={(e) => {
              const t = e.clipboardData.getData("text").trim();
              if (t) { e.preventDefault(); setKey(t); }   // 粘贴即所用，去首尾空白
            }}
            placeholder="整串粘贴进来即可，不需要用户名" autoFocus autoComplete="off" spellCheck={false} />
          <button type="button" className="eye" onClick={() => setShow((v) => !v)}
            aria-label={show ? "隐藏密钥" : "显示密钥"}>
            <Icon name={show ? "eye-off" : "eye"} size={16} />
          </button>
        </div>
        {error && (
          <div className="alert alert-err" role="alert">
            <Icon name="cross" />
            <span>{error.replace(/[。.]$/, "")}——密钥原样留着，改完直接再试。</span>
          </div>
        )}
        <button type="submit" className="btn btn-primary btn-lg" disabled={busy || !key}>
          {busy ? "正在核对…" : "进入台账"}
        </button>
        <p className="gate-hint">登录后 7 天内免登录（本机 cookie）。连错 5 次会限流一分钟。</p>
      </form>
    </div>
  );
}
