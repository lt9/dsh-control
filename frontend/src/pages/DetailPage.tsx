import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, type DshConfig, type TokenResp } from "../api/client";
import { fmtBytes, fmtDate, fmtFull, fmtDaysLeft, fmtRel, maskMiddle, maskUrlToken } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { ConsequenceCard, CopyButton, Icon, Modal, StateChip } from "../components/ui";

/** 详情页四种「有后果的动作」，同一时刻只摊开一张后果卡。 */
type Op = "release" | "purge" | "upgrade" | "restore";

export default function DetailPage() {
  const { id } = useParams();
  const nav = useNavigate();
  const [cfgOpen, setCfgOpen] = useState(false);
  const [op, setOp] = useState<Op | null>(null);
  const [showToken, setShowToken] = useState(false);
  const [renameFor, setRenameFor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [opError, setOpError] = useState<string | null>(null);

  const det = useAsync(() => api.instance(id!), [id]);
  const events = useAsync(() => api.events(id!), [id]);
  const [tok, setTok] = useState<TokenResp | null>(null);
  const [tokErr, setTokErr] = useState<string | null>(null);
  const [tokBusy, setTokBusy] = useState(false);
  // DSH 配置：进页面只读快照（读一次要 ssh 进容器 0.6s+），要新值请点「重新读取」
  const [dsh, setDsh] = useState<DshConfig | null>(null);
  const [dshErr, setDshErr] = useState<string | null>(null);
  const [dshBusy, setDshBusy] = useState(false);

  // §9：进页面那一次走 /token 现取——重启后密钥会变，这里是当前最新的一份。
  const fetchToken = useCallback(async (): Promise<Partial<TokenResp> | null> => {
    if (!id) return null;
    setTokBusy(true); setTokErr(null);
    try {
      const t = await api.token(id);
      setTok({ token: t.token ?? null, url: t.url ?? null, captured_at: t.captured_at ?? null });
      return t;
    } catch (e) {
      setTokErr(e instanceof Error ? e.message : "取不到");
      return null;
    } finally { setTokBusy(false); }
  }, [id]);
  // §9：完整密钥只在「进页面那一次」和「用户自己点刷新 / 再取一次 / 点复制」时现取。
  // 这一页没有定时器、没有 SSE，任何自动路径都不会去敲 /token（§3：watcher 也不抓）。
  // 进页面那一次要等详情先回来——知道容器已经不在（released 停着 / purged 已删）
  // 就不去敲这一枪：本来取不到，白挨一次 409，还会在页面上留一条没意义的「现取失败」。
  const state = det.data?.state ?? null;
  /** 容器不在的状态：released 停着（数据留着）、purged 已删（连数据目录都没了）。
   *  这两态没有可抓的登录密钥，也不该再采运行态。 */
  const containerGone = state === "released" || state === "purged";
  const revealedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!state || revealedFor.current === state) return;
    revealedFor.current = state;
    if (containerGone) return;
    void fetchToken();
  }, [state, containerGone, fetchToken]);

  const refreshDsh = useCallback(async () => {
    if (!id) return;
    setDshBusy(true); setDshErr(null);
    try {
      setDsh(await api.dshConfig(id));
      void det.reload();          // 快照已落库，台账那份也要跟着新
    } catch (e) {
      setDshErr(e instanceof Error ? e.message : "读不到 DSH 配置（容器没起来或 ssh 不通）");
    } finally { setDshBusy(false); }
  }, [id, det.reload]);

  // 手动「刷新」= 用户主动要一份最新的，这一次才再 reveal 一次（§9：不自动轮询）。
  // 容器已经不在了的状态没什么可现取的，别去敲那一下。
  const refreshAll = () => {
    void det.reload(); void events.reload();
    if (!containerGone) void fetchToken();
  };
  const d = det.data;
  useEffect(() => { setDsh(d?.dsh_config ?? null); setDshErr(null); }, [d?.dsh_config]);
  const released = d?.state === "released";
  const purged = d?.state === "purged";

  const doRestart = async () => {
    if (!d) return;
    setBusy(true); setOpError(null);
    try { const r = await api.restart(d.id); nav(`/jobs/${r.job_id}?instance=${d.id}&kind=restart`); }
    catch (e) { setOpError(e instanceof Error ? e.message : String(e)); setBusy(false); }
  };
  /** deploy 是续建统一入口（§4）：开通失败续建 / 升级重建 / released 恢复，三种来路
   *  都往这里走，差别只在文案与工单页标题。都不传 image——目标镜像就是站点当前 tag，
   *  本期界面不做版本选择（§6.1）。 */
  const doDeploy = async (opKind?: "upgrade" | "restore") => {
    if (!d) return;
    setBusy(true); setOpError(null);
    try {
      const r = await api.deploy(d.id);
      nav(`/jobs/${r.job_id}?instance=${d.id}&kind=deploy${opKind ? `&op=${opKind}` : ""}`);
    }
    catch (e) { setOpError(e instanceof Error ? e.message : String(e)); setBusy(false); setOp(null); }
  };
  const doRelease = async () => {
    if (!d) return;
    setBusy(true); setOpError(null);
    try { const r = await api.release(d.id, d.slug); nav(`/jobs/${r.job_id}?instance=${d.id}&kind=release`); }
    catch (e) { setOpError(e instanceof Error ? e.message : String(e)); setBusy(false); setOp(null); }
  };
  /** 彻底释放：confirm=slug 由前端填（§4），用户不打字，防误靠按住 1.5 秒。 */
  const doPurge = async () => {
    if (!d) return;
    setBusy(true); setOpError(null);
    try { const r = await api.purge(d.id, d.slug); nav(`/jobs/${r.job_id}?instance=${d.id}&kind=purge`); }
    catch (e) { setOpError(e instanceof Error ? e.message : String(e)); setBusy(false); setOp(null); }
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
  // 默认打码，点一下才展开（§9）。复制不再拿屏幕上的串——它给 CopyButton 传
  // provide，点复制时现取真值，取不到就什么都不做。
  const shownUrl = fullUrl ? (showToken ? fullUrl : maskUrlToken(fullUrl))
    : (d.access?.url_masked ?? null);
  const shownToken = fullToken ? (showToken ? fullToken : maskMiddle(fullToken)) : (d.access?.token_masked ?? null);
  const label = d.owner_label;
  const cfgItems = d.config?.items ?? [];
  // actions 由服务端算（§4）；万一后端还没给这一组，退化成「什么都不给点」，
  // 而不是让详情页整页白掉——台账页看不到这台，比页面崩了有用。
  const acts = d.actions ?? { restart: false, release: false };
  const canUpgrade = acts.upgrade === true && (d.state === "running" || d.state === "unhealthy");
  const canRestore = acts.restore === true && released;
  const canPurge = acts.purge === true && released;
  // 开通失败续建：契约 §4 的示例 actions 里没有 deploy 这一键，老前端一直靠它。
  // 后端没给就按状态自己判——error/pending 且没有别的可用动作时，这就是那台「重新开通」。
  const canRetry = acts.deploy === true
    || (acts.deploy === undefined && (d.state === "error" || d.state === "pending"));
  const port = d.network?.frp_remote_port ?? null;
  const dueDate = fmtDate(d.purge_after);
  const dueLeft = fmtDaysLeft(d.purge_after);

  return (
    <main className="shell">
      <button type="button" className="btn btn-ghost" style={{ marginBottom: 4 }} onClick={() => nav("/")}>
        <Icon name="back" size={13} />回台账
      </button>
      <div className="detail-head">
        <span className="id">{d.slug}</span>
        {renameFor === null
          ? <span className="label">
              {label ?? <span className="mut small">{purged ? "未备注" : "未备注 · 点铅笔给他起个名"}</span>}
              {!purged && (
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
              {purged ? (
                <p className="mut small">这台已彻底释放：容器和数据目录都删了，没有可登录的地址，密钥也随数据一起没了。</p>
              ) : released ? (
                <p className="mut small">实例已释放，公网入口已注销，现在没有可用的登录地址。恢复回来还是这个域名。</p>
              ) : (<>
                {tokErr && !fullUrl && <div className="alert alert-warn"><Icon name="triangle" /><span>现取失败：{tokErr}。下面是库里最后一次取到的（打码）。</span></div>}
                <div className="secret-row">
                  <span className="cap">地址</span>
                  <span className="val mono">{shownUrl ?? <span className="mut">
                    {d.access?.has_token ? "完整地址含登录密钥，点右边「再取一次」现取" : "还没抓到过"}
                  </span>}</span>
                  <span className="ops">
                    {(fullUrl || d.access?.has_token) && (
                      <button type="button" className="btn btn-icon"
                        aria-label={showToken ? "隐藏 token" : "展开完整地址"}
                        onClick={() => {
                          if (!fullUrl && !showToken) { void fetchToken().then((r) => { if (r?.url) setShowToken(true); }); return; }
                          setShowToken((v) => !v);
                        }}>
                        <Icon name={showToken ? "eye-off" : "eye"} size={15} />
                      </button>
                    )}
                    <CopyButton text={fullUrl} provide={() => fetchToken().then((r) => r?.url ?? null)}
                      label="复制地址" title="发给他，打开就用" />
                  </span>
                </div>
                <div className="secret-row">
                  <span className="cap">密钥</span>
                  <span className="val mono">{shownToken ?? <span className="mut">—</span>}</span>
                  <span className="ops">
                    <CopyButton text={fullToken} provide={() => fetchToken().then((r) => r?.token ?? null)}
                      label="复制密钥" />
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
              {containerGone || !d.runtime ? (
                <p className="mut small">
                  {purged ? "已彻底释放：容器已删除，不再采集。"
                    : released ? "已释放：容器只停不删，不再采集。"
                    : "还没采到运行态。"}
                </p>
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

          {/* —— DSH 配置：她问的是"用的哪个模型、能不能生图、团队开没开" —— */}
          <section className="card" aria-label="DSH 配置">
            <div className="card-head">
              <h2>DSH 配置</h2>
              <span className="mut small">{dsh ? `读取于 ${dsh.captured_at}` : "未读取"}</span>
              <span className="spacer" />
              {!containerGone && (
                <button type="button" className="btn btn-sm" onClick={() => void refreshDsh()}
                  disabled={dshBusy} aria-busy={dshBusy}
                  title="进容器读一次实际生效的配置（约 1 秒）">
                  <Icon name="refresh" size={14} />{dshBusy ? "读取中…" : "重新读取"}
                </button>
              )}
            </div>
            <div className="card-body">
              {dshErr && <p className="mut small" role="alert" style={{ color: "var(--err)" }}>{dshErr}</p>}
              {!dsh && !dshErr && (
                <p className="mut small">
                  {containerGone
                    ? "这台没有配置快照可读：容器已经不在了。"
                    : "这台实例还没读过 DSH 内部的配置（旧快照）。点「重新读取」进容器读一次实际生效的值。"}
                </p>
              )}
              {dsh && dsh.groups.map((g) => (
                <div className="dsh-group" key={g.title}>
                  <h3>{g.title}</h3>
                  <dl className="kv dsh-kv">
                    {g.items.map((it) => (
                      <div key={it.key} style={{ display: "contents" }}>
                        <dt title={it.note || undefined}>{it.key}</dt>
                        <dd className="mono">
                          {it.value || "—"}
                          {it.note && <span className="mut small" style={{ display: "block" }}>{it.note}</span>}
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              ))}
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
                    <dt>{it.label}</dt>
                    <dd className="mono">{String(it.value ?? "") === ""
                      ? <span className="mut">（这条快照里没记值——旧快照没回填生效值）</span>
                      : it.value}</dd>
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
              {d.purge_after && <>
                <dt>保留期至</dt>
                <dd className="num">{dueDate}{dueLeft ? <span className="mut small">（{dueLeft}）</span> : null}</dd>
              </>}
              {d.purged_at && <><dt>彻底释放于</dt><dd className="num">{fmtFull(d.purged_at)}</dd></>}
            </dl>
          </details>
        </div>

        <div className="stack">
          {/* —— 操作 —— */}
          <section className="card" aria-label="操作">
            <div className="card-head"><h2>操作</h2></div>
            <div className="card-body">
              {purged ? (
                /* purged = 只读档案：一个动作按钮都不给（§9）。历史照常看，就在下面这张卡。 */
                <p className="mut small">
                  {"已彻底释放：容器、数据目录和外部对象都删干净了，"}<b>不可恢复</b>{"。这一条只当档案留着——它做过什么，看下面的事件与台账明细。"}
                </p>
              ) : (<>
                <div className="actions">
                  {canRetry && (
                    <button type="button" className="btn btn-primary btn-lg" disabled={busy || op !== null}
                      onClick={() => void doDeploy()}
                      title="从失败的那一步接着建：已建好的域名、隧道、反代会跳过，不会重复创建">
                      <Icon name="plus" />重新开通
                    </button>
                  )}
                  {canUpgrade && (
                    <button type="button" className="btn btn-lg" disabled={busy || op !== null}
                      onClick={() => setOp("upgrade")}
                      title="用站点当前的镜像标签原地重建这台容器：数据不动，登录链接不变，会话会断一下">
                      <Icon name="upgrade" />升级 / 重建
                    </button>
                  )}
                  {!containerGone && (
                    <button type="button" className="btn btn-lg" disabled={!acts.restart || busy || op !== null}
                      onClick={doRestart}
                      title="重启会轮换登录密钥，新密钥自动抓回">
                      <Icon name="power" />重启
                    </button>
                  )}
                  {canRestore && (
                    <button type="button" className="btn btn-primary btn-lg" disabled={busy || op !== null}
                      onClick={() => setOp("restore")}
                      title="重建解析、隧道与反代，把停着的容器起回来：还是这个域名，数据原样">
                      <Icon name="restore" />恢复实例
                    </button>
                  )}
                  {acts.release && !released && (
                    <button type="button" className="btn btn-danger btn-lg" disabled={busy || op !== null}
                      onClick={() => setOp("release")}>
                      <Icon name="release" />释放…
                    </button>
                  )}
                  {canPurge && (
                    <button type="button" className="btn btn-danger btn-lg" disabled={busy || op !== null}
                      onClick={() => setOp("purge")}
                      title="删容器、删数据目录，不可恢复">
                      <Icon name="purge" />彻底释放…
                    </button>
                  )}
                  {!acts.restart && !acts.release && !canUpgrade && !canRetry && !released && !purged && (
                    <span className="mut small" style={{ alignSelf: "center" }}>当前状态没有可执行的操作</span>
                  )}
                  {released && !canRestore && !canPurge && (
                    <p className="mut small">已释放：容器停着、数据留着，但接口说这台既不能恢复也不能彻底释放。</p>
                  )}
                </div>

                {/* 一屏后果卡：点开哪个动作就摊开哪张，零打字（§9）。
                    释放与彻底释放要长按 1.5 秒，升级/重建与恢复一次点击。 */}
                {op === "release" && (
                  <ConsequenceCard
                    title="释放放行条" no={`NO.${String(d.id).padStart(4, "0")} · ${d.slug}`}
                    rows={[
                      { cap: "注销解析", val: <span className="mono">A {d.network?.dns_name ?? d.slug} → RecordId {d.network?.dns_record_id ?? "—"}</span> },
                      { cap: "注销隧道", val: <span className="mono">frp 公网 {d.network?.frp_remote_port ?? "—"} · 隧道 id {d.network?.frp_proxy_id ?? "—"}</span> },
                      { cap: "注销站点", val: <span className="mono">nginx {d.network?.public_url ?? d.network?.dns_name ?? "—"}</span> },
                      { cap: "停止容器", val: <span>只 <span className="mono">stop</span>，不删容器、不碰数据目录和镜像</span> },
                    ]}
                    due={<>签字之后他立刻打不开这个地址。容器只停不删、数据原样留着，保留期内在这一页可以一键恢复；<b>到期还没恢复的，系统会自动彻底释放</b>。</>}
                    hold confirmLabel="按住签字释放" holdAriaLabel="按住 1.5 秒确认释放"
                    onConfirm={doRelease} onCancel={() => setOp(null)} busy={busy}
                  />
                )}

                {op === "purge" && (
                  <ConsequenceCard
                    title="彻底释放" no={`NO.${String(d.id).padStart(4, "0")} · ${d.slug}`}
                    head={d.purge_after
                      ? (dueLeft === "已到期"
                        ? <>保留期已于 <b className="num">{dueDate}</b> 期满，系统随时可能自动执行同样的操作。</>
                        : <>保留期至 <b className="num">{dueDate}</b>（{dueLeft}），到期后系统会自动执行同样的操作。</>)
                      : <>保留期满之后，系统会自动执行同样的操作。</>}
                    rows={[
                      { cap: "删除容器", val: <span>连正在跑的一起 <span className="mono">docker rm</span>；镜像不动</span> },
                      { cap: "删除数据目录", val: <span>这台的全部用户数据，<b>含备份归档与全部历史</b>，一起删掉</span> },
                      { cap: "补删外部对象", val: <span>解析 / 隧道 / 反代站点，凡是还挂着记录的逐个补删干净</span> },
                      { cap: "保留的东西", val: <span>只有台账这一条：它的工单与事件照常可看</span> },
                    ]}
                    due={<><b>不可恢复。</b>要再用只能新建一台，数据不会回来。</>}
                    hold confirmLabel="按住签字彻底释放" holdAriaLabel="按住 1.5 秒确认彻底释放"
                    onConfirm={doPurge} onCancel={() => setOp(null)} busy={busy}
                  />
                )}

                {op === "upgrade" && (
                  <ConsequenceCard
                    danger={false}
                    title="升级 / 重建" no={`NO.${String(d.id).padStart(4, "0")} · ${d.slug}`}
                    rows={[
                      { cap: "做什么", val: <span>用站点当前的镜像标签把这台容器<b>原地重建</b>一次（不是 <span className="mono">restart</span>，换镜像必须重建才生效）</span> },
                      { cap: "目标镜像", val: <span className="mono" title="镜像是事先构建好传到那台机器上的，控制台不构建；这一期不做版本选择，目标就是站点配置的那个标签">
                        {d.image_tag ?? "站点当前标签"}<span className="mut small"> · 站点当前版本，不用选</span></span> },
                      { cap: "会断什么", val: <span>正在跑的会话断一下，重开就好</span> },
                      { cap: "不动什么", val: <span>数据目录一个字不动；域名、公网端口、反代都不重建</span> },
                      { cap: "登录链接", val: <span>不变，密钥也不轮换——现在这串地址重建完照样能用</span> },
                    ]}
                    due={<>同实例原地重建，不会新建第二台，也不迁数据。</>}
                    confirmLabel={busy ? "已提交，正在执行…" : "确认重建"}
                    onConfirm={() => void doDeploy("upgrade")} onCancel={() => setOp(null)} busy={busy}
                  />
                )}

                {op === "restore" && (
                  <ConsequenceCard
                    danger={false}
                    title="恢复实例" no={`NO.${String(d.id).padStart(4, "0")} · ${d.slug}`}
                    rows={[
                      { cap: "重新解析", val: <span className="mono">A {d.network?.dns_name ?? d.slug}（还是这个域名）</span> },
                      { cap: "重开隧道", val: <span>frp 公网端口尽量找回原来那个{port != null ? <span className="mono"> {port}</span> : null}；被占了就换一个，域名不受影响</span> },
                      { cap: "重建站点", val: <span className="mono">nginx {d.network?.public_url ?? d.network?.dns_name ?? "—"}（upstream 名照旧）</span> },
                      { cap: "起回容器", val: <span>同一个数据目录、同一个容器名，<b>数据原样</b></span> },
                    ]}
                    due={<>还是这一台、还是这个域名，不新建第二台。恢复之后保留期不再倒计时，到期自动彻底释放那条也不再触发。</>}
                    confirmLabel={busy ? "已提交，正在执行…" : "开始恢复"}
                    onConfirm={() => void doDeploy("restore")} onCancel={() => setOp(null)} busy={busy}
                  />
                )}
              </>)}
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
              <div className="card-head">
                <h2>{d.last_error ? "异常与最近事件" : "最近事件"}</h2>
                {(events.data?.length ?? 0) > 0 && (
                  <span className="mut small">{(events.data ?? []).length} 条</span>
                )}
              </div>
              <div className="card-body">
                {d.last_error && (
                  <div className="alert alert-err" style={{ marginTop: 0 }}>
                    <Icon name="cross" /><span>{d.last_error}</span>
                  </div>
                )}
                {/* 事件能攒到上百条，以前整条摊在页面上，详情页一打开就得滚半天。
                    收进一个定高滚动区：异常提示留在外面（那条不能被藏起来），
                    列表自己滚。tabIndex/role 是滚动区必须能用键盘滚的规矩。 */}
                <div className="feed-scroll scroll-light" tabIndex={0} role="region"
                     aria-label={`最近事件，共 ${(events.data ?? []).length} 条，可滚动`}>
                  <ul className="timeline" style={{ marginTop: d.last_error ? 14 : 0 }}>
                    {(events.data ?? []).map((ev) => (
                      <li key={ev.id} className={/fail|错误|不健康|unhealthy/i.test(ev.message + ev.kind) ? "hot" : ""}>
                        <div className="t" title={fmtFull(ev.created_at)}>{fmtRel(ev.created_at)}</div>
                        <div className="m">{ev.message}</div>
                      </li>
                    ))}
                  </ul>
                </div>
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
