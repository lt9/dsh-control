import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type TokenResp } from "../api/client";
import { fmtBytes, fmtFull, fmtRel, maskMiddle, maskUrlToken } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { CopyButton, HoldButton, Icon, Modal, StateChip } from "../components/ui";

export default function DetailPage() {
  const { id } = useParams();
  const nav = useNavigate();
  const [cfgOpen, setCfgOpen] = useState(false);
  const [slipOpen, setSlipOpen] = useState(false);
  const [showToken, setShowToken] = useState(false);
  const [renameFor, setRenameFor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  const det = useAsync(() => api.instance(id!), [id]);
  const events = useAsync(() => api.events(id!), [id]);
  const [tok, setTok] = useState<TokenResp | null>(null);
  const [tokErr, setTokErr] = useState<string | null>(null);
  const [tokBusy, setTokBusy] = useState(false);

  // §9：每次进入 / 手动刷新都走 /token 强制现取——重启后密钥会变，这里是当前最新的。
  const fetchToken = useCallback(async () => {
    if (!id) return;
    setTokBusy(true); setTokErr(null);
    try {
      const t = await api.token(id);
      setTok({ token: t.token ?? null, url: t.url ?? null, captured_at: t.captured_at ?? null });
    } catch (e) {
      setTokErr(e instanceof Error ? e.message : "取不到");
    } finally { setTokBusy(false); }
  }, [id]);
  useEffect(() => { void fetchToken(); }, [fetchToken]);

  const refreshAll = () => { void det.reload(); void events.reload(); void fetchToken(); };
  const d = det.data;
  const released = d?.state === "released";

  const doRestart = async () => {
    if (!d) return;
    setBusy(true); setOpError(null);
    try { const r = await api.restart(d.id); nav(`/jobs/${r.job_id}?instance=${d.id}&kind=restart`); }
    catch (e) { setOpError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  const doRelease = async () => {
    if (!d) return;
    try { const r = await api.release(d.id, d.slug); nav(`/jobs/${r.job_id}?instance=${d.id}&kind=release`); }
    catch (e) { setOpError(e instanceof Error ? e.message : String(e)); setSlipOpen(false); }
  };

  if (det.loading && !d) {
    return <main className="shell" aria-busy="true">
      <div className="skeleton sk-line" style={{ width: "30%" }} />
      <div className="skeleton sk-line" style={{ height: 90 }} />
      <div className="skeleton sk-line" style={{ height: 160 }} />
    </main>;
  }
  if (!d) {
    return <main className="shell">
      <div className="alert alert-err" role="alert">
        <Icon name="cross" /><span>详情拉不起来：{det.error}
          <button className="btn btn-ghost" onClick={refreshAll}>重试</button></span>
      </div>
    </main>;
  }

  const fullToken = tok?.token ?? null;
  const fullUrl = tok?.url ?? null;
  // 默认打码，点一下才展开（§9）；复制永远复制完整值。
  const shownUrl = fullUrl ? (showToken ? fullUrl : maskUrlToken(fullUrl)) : (d.access?.url ?? null);
  const shownToken = fullToken ? (showToken ? fullToken : maskMiddle(fullToken)) : (d.access?.token_masked ?? null);
  const label = d.owner_label;
  const cfgItems = d.config?.items ?? [];

  return (
    <main className="shell">
      <button type="button" className="btn btn-ghost" style={{ marginBottom: 4 }} onClick={() => nav("/")}>
        <Icon name="back" size={13} />回台账
      </button>
      <div className="detail-head">
        <span className="id">{d.slug}</span>
        {renameFor === null
          ? <span className="label">
              {label ?? <span className="mut small">未备注 · 点铅笔给他起个名</span>}
              {!released && (
                <button type="button" className="btn btn-ghost btn-icon" aria-label="改备注名"
                  onClick={() => setRenameFor(label ?? "")}><Icon name="pencil" size={13} /></button>
              )}
            </span>
          : <RenameBox value={renameFor} busy={busy}
              onCancel={() => setRenameFor(null)}
              onSave={async (v) => {
                setBusy(true);
                try { await api.rename(d.id, v.trim()); det.setData({ ...d, owner_label: v.trim() || null }); setRenameFor(null); }
                catch (e) { setOpError(e instanceof Error ? e.message : String(e)); }
                finally { setBusy(false); }
              }} />}
        <StateChip state={d.state} />
        <span className="mut small">{d.host?.name ?? "主机未知"}</span>
        <span className="spacer" />
        <button type="button" className="btn" onClick={refreshAll}><Icon name="refresh" size={13} />刷新</button>
      </div>

      {opError && <div className="alert alert-err" role="alert"><Icon name="cross" /><span>{opError}</span></div>}

      <div className="grid2">
        <div className="stack">
          {/* —— 一键登录 —— */}
          <section className="card" aria-label="一键登录">
            <div className="card-head"><h2>一键登录</h2><span className="spacer" />
              {d.access?.captured_at && <span className="mut small mono" title={fmtFull(d.access.captured_at)}>上次取到 {fmtRel(d.access.captured_at)}</span>}
            </div>
            <div className="card-body">
              {released ? (
                <p className="mut small">实例已释放，公网入口已注销，没有可用的登录地址。</p>
              ) : (<>
                {tokErr && !fullUrl && <div className="alert alert-warn"><Icon name="triangle" /><span>现取失败：{tokErr}。下面是库里最后一次取到的（打码）。</span></div>}
                <div className="secret-row">
                  <span className="cap">地址</span>
                  <span className="val mono">{shownUrl ?? <span className="mut">还没抓到过</span>}</span>
                  <span className="ops">
                    {fullUrl && <button type="button" className="btn btn-icon" aria-label={showToken ? "隐藏 token" : "展开完整地址"}
                      onClick={() => setShowToken((v) => !v)}><Icon name={showToken ? "eye-off" : "eye"} size={15} /></button>}
                    <CopyButton text={fullUrl ?? shownUrl} label="复制地址" title="发给他，打开就用" />
                  </span>
                </div>
                <div className="secret-row">
                  <span className="cap">密钥</span>
                  <span className="val mono">{shownToken ?? <span className="mut">—</span>}</span>
                  <span className="ops">
                    <CopyButton text={fullToken ?? shownToken} label="复制密钥" />
                  </span>
                </div>
                <div className="fresh-note">
                  <span>重启后密钥会变——这里每次进页都重新现取，是<strong>当前最新</strong>的一份。默认打码，点眼睛才展开。</span>
                  <button type="button" className="btn" onClick={() => void fetchToken()} disabled={tokBusy}>
                    <Icon name="refresh" size={13} />{tokBusy ? "现取中…" : "再取一次"}
                  </button>
                </div>
              </>)}
            </div>
          </section>

          {/* —— 运行态 —— */}
          <section className="card" aria-label="运行态">
            <div className="card-head"><h2>运行态</h2><span className="spacer" />
              {d.runtime?.captured_at && <span className="mut small" title={fmtFull(d.runtime.captured_at)}>采集于 {fmtRel(d.runtime.captured_at)}</span>}
            </div>
            <div className="card-body">
              {released || !d.runtime ? (
                <p className="mut small">{released ? "已释放：容器只停不删，不再采集。" : "还没采到运行态。"}</p>
              ) : (
                <dl className="kv">
                  <dt>容器</dt><dd>{d.runtime.docker_state === "running" ? "在跑" : d.runtime.docker_state === "exited" ? "已停止" : d.runtime.docker_state}</dd>
                  <dt>健康</dt><dd>{d.runtime.health === "healthy" ? "健康" : d.runtime.health === "unhealthy" ? "不健康" : (d.runtime.health ?? "未知")}</dd>
                  <dt>重启次数</dt><dd className="num">{d.runtime.restart_count ?? "—"}</dd>
                  <dt>CPU</dt><dd className="num">{d.runtime.cpu_pct != null ? `${d.runtime.cpu_pct.toFixed(1)} %` : "—"}</dd>
                  <dt>内存</dt><dd>
                    <span className="num">{fmtBytes(d.runtime.mem_usage_bytes)} / {fmtBytes(d.runtime.mem_limit_bytes)}</span>
                    <div className="mem-bar" role="img" aria-label={`内存占用 ${d.runtime.mem_usage_bytes != null && d.runtime.mem_limit_bytes ? Math.round(d.runtime.mem_usage_bytes / d.runtime.mem_limit_bytes * 100) : 0}%`}>
                      <i style={{ width: `${d.runtime.mem_usage_bytes != null && d.runtime.mem_limit_bytes ? Math.min(100, d.runtime.mem_usage_bytes / d.runtime.mem_limit_bytes * 100) : 0}%` }} />
                    </div>
                  </dd>
                </dl>
              )}
            </div>
          </section>

          {/* —— 配置 —— */}
          <section className="card" aria-label="配置">
            <div className="card-head">
              <h2>配置</h2>
              <span className="mono mut small">{d.config_version ?? "—"}</span>
              <span className="spacer" />
              <button type="button" className="btn btn-icon" aria-label="逐项解释配置" onClick={() => setCfgOpen(true)}>
                <Icon name="help" size={16} />
              </button>
            </div>
            <div className="card-body">
              {cfgItems.length === 0 ? <p className="mut small">没有配置快照。</p> : (
                <dl className="kv">
                  {cfgItems.map((it) => <div key={it.key} style={{ display: "contents" }}>
                    <dt>{it.label}</dt><dd className="mono">{it.value}</dd>
                  </div>)}
                </dl>
              )}
            </div>
          </section>

          {/* —— 次要：台账明细（用不上的原始值收在这） —— */}
          <details className="card" style={{ padding: "10px 16px" }}>
            <summary className="mut small" style={{ cursor: "pointer", letterSpacing: ".08em" }}>台账明细（给排障用的原始值）</summary>
            <dl className="kv" style={{ marginTop: 10 }}>
              <dt>镜像 id</dt><dd className="mono">{d.image_id ?? "—"}</dd>
              <dt>frp 隧道</dt><dd className="mono">{d.network?.frp_remote_port ?? "—"} · 记录 id {d.network?.frp_proxy_id ?? "—"}</dd>
              <dt>DNS 记录</dt><dd className="mono">{d.network?.dns_record_id ?? "—"}</dd>
              <dt>状态机</dt><dd className="mono">{d.state} → {d.desired_state}</dd>
              {d.released_at && <><dt>释放于</dt><dd className="num">{fmtFull(d.released_at)}</dd></>}
            </dl>
          </details>
        </div>

        <div className="stack">
          {/* —— 操作 —— */}
          <section className="card" aria-label="操作">
            <div className="card-head"><h2>操作</h2></div>
            <div className="card-body">
              {released ? (
                <p className="mut small">已释放的实例只留档案：公网入口已注销、容器已停止（数据还在），没有恢复入口。</p>
              ) : (
                <div className="actions">
                  <button type="button" className="btn btn-lg" disabled={!d.actions.restart || busy} onClick={doRestart}
                    title="重启会轮换登录密钥，新密钥自动抓回">
                    <Icon name="power" />重启
                  </button>
                  <button type="button" className="btn btn-danger btn-lg" disabled={!d.actions.release || slipOpen}
                    onClick={() => setSlipOpen(true)}>
                    <Icon name="release" />释放…
                  </button>
                  {!d.actions.restart && !released && <span className="mut small" style={{ alignSelf: "center" }}>当前状态不可重启</span>}
                </div>
              )}

              {slipOpen && !released && (
                <div className="slip" role="group" aria-label="释放放行条">
                  <div className="slip-title">
                    <h3>释 放 放 行 条</h3>
                    <span className="no mono">NO.{String(d.id).padStart(4, "0")} · {d.slug}</span>
                  </div>
                  <ul className="slip-rows">
                    <li><i>注销解析</i><span className="mono">A {d.network?.dns_name ?? d.slug} → RecordId {d.network?.dns_record_id ?? "—"}</span></li>
                    <li><i>注销隧道</i><span className="mono">frp 公网 {d.network?.frp_remote_port ?? "—"} · 隧道 id {d.network?.frp_proxy_id ?? "—"}</span></li>
                    <li><i>注销站点</i><span className="mono">nginx {d.network?.public_url ?? d.network?.dns_name ?? "—"}</span></li>
                    <li><i>停止容器</i><span>只 <span className="mono">stop</span>，不删容器、不碰数据目录和镜像</span></li>
                  </ul>
                  <p className="slip-due">
                    签字之后<b>不可恢复</b>：他从今往后打不开这个地址；台账里这条永远灰着，只当档案。
                  </p>
                  <div className="slip-ops">
                    <HoldButton onHold={doRelease}>按住签字释放</HoldButton>
                    <button type="button" className="btn" onClick={() => setSlipOpen(false)}>再想想</button>
                  </div>
                </div>
              )}
            </div>
          </section>

          {/* —— 网络 —— */}
          <section className="card" aria-label="网络">
            <div className="card-head"><h2>网络</h2></div>
            <div className="card-body">
              <dl className="kv">
                <dt>域名</dt><dd className="mono">{d.network?.dns_name ?? "—"}</dd>
                <dt>公网入口</dt><dd className="mono">{d.network?.public_url ?? "—"}</dd>
                <dt>公网端口</dt><dd className="num">{d.network?.frp_remote_port ?? "—"}</dd>
                <dt>容器 IP</dt><dd className="mono">{d.network?.container_ip ?? "—"}</dd>
                <dt>DSH 版本</dt><dd className="mono">{d.dsh_version ?? "—"}</dd>
                {/* 镜像只读一行，跟版本放一起；本期无任何切换入口（多版本后置）。 */}
                <dt>镜像</dt>
                <dd className="mono" title="镜像是事先构建好传到那台机器上的，控制台不构建；换版本是以后的能力，现在固定用站点配置的默认版本。">
                  {d.image_tag ?? d.image_id ?? "—"}<span className="mut small"> 站点默认 · 不可切换</span>
                </dd>
              </dl>
            </div>
          </section>

          {/* —— 异常与事件 —— */}
          {(d.last_error || (events.data?.length ?? 0) > 0) && (
            <section className="card" aria-label="异常与事件">
              <div className="card-head"><h2>{d.last_error ? "异常与最近事件" : "最近事件"}</h2></div>
              <div className="card-body">
                {d.last_error && (
                  <div className="alert alert-err" style={{ marginTop: 0 }}>
                    <Icon name="cross" /><span>{d.last_error}</span>
                  </div>
                )}
                <ul className="timeline" style={{ marginTop: d.last_error ? 14 : 0 }}>
                  {(events.data ?? []).map((ev) => (
                    <li key={ev.id} className={/fail|错误|不健康|unhealthy/i.test(ev.message + ev.kind) ? "hot" : ""}>
                      <div className="t" title={fmtFull(ev.created_at)}>{fmtRel(ev.created_at)}</div>
                      <div className="m">{ev.message}</div>
                    </li>
                  ))}
                </ul>
              </div>
            </section>
          )}
        </div>
      </div>

      {cfgOpen && (
        <Modal title={`配置逐项解释 · ${d.config_version ?? ""}`} onClose={() => setCfgOpen(false)} wide>
          <ul className="cfg-list">
            {cfgItems.map((it) => (
              <li key={it.key}>
                <div className="cfg-item-head">
                  <b>{it.label}</b><span className="mono">{it.key} = {it.value}</span>
                </div>
                <p className="cfg-item-help">{it.help}</p>
              </li>
            ))}
          </ul>
        </Modal>
      )}
    </main>
  );
}

function RenameBox({ value, busy, onSave, onCancel }: {
  value: string; busy: boolean; onSave: (v: string) => void; onCancel: () => void;
}) {
  const [v, setV] = useState(value);
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => { ref.current?.focus(); ref.current?.select(); }, []);
  return (
    <span className="rename">
      <input ref={ref} value={v} maxLength={24} aria-label="备注名" disabled={busy}
        onChange={(e) => setV(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") onSave(v);
          if (e.key === "Escape") onCancel();
        }} />
      <button type="button" className="btn btn-primary" disabled={busy} onClick={() => onSave(v)}>存</button>
      <button type="button" className="btn" onClick={onCancel}>取消</button>
    </span>
  );
}
