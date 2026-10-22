import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError, api, type ImageArtifact, type ShipMode } from "../api/client";
import { SHIP_MODE_META, fmtBytes, fmtFull, fmtRel, maskMiddle } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { ConsequenceCard, CopyButton, HelpTip, Icon, Toast, useToast } from "../components/ui";

const msgOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** 下载链接里 token 那一截打码显示（§9：表格里的 token 默认打码）。
 *  复制出去的仍然是完整链接——打码只发生在屏幕上。 */
function maskedUrl(a: ImageArtifact): string {
  if (!a.url) return "—";
  return a.token ? a.url.replace(a.token, maskMiddle(a.token, 4, 3)) : a.url;
}
const fileName = (a: ImageArtifact) => (a.url ? a.url.split("/").pop() || a.url : `${a.tag}.tar.gz`);

/**
 * 第五个界面（§9 第 5 屏）：镜像分发方式 + 下载基址 + 制品表 + 从主机发布。
 * 这一页动的是站点级设置与制品库，不碰任何一台实例；两处改动都立刻生效（§6.1.5）。
 */
export default function SettingsPage() {
  const nav = useNavigate();
  const cfg = useAsync(() => api.settings(), []);
  const store = useAsync(() => api.images(), []);
  const hosts = useAsync(() => api.hosts(), []);
  const { toast, say } = useToast();

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [op, setOp] = useState<ImageArtifact | null>(null);   // 摊开下架后果卡的那一行
  const [openSha, setOpenSha] = useState<number | null>(null); // 展开完整校验码的那一行
  const [baseDraft, setBaseDraft] = useState<string | null>(null); // null = 没动过，跟着接口给的值
  const [tag, setTag] = useState("");
  const [fromHost, setFromHost] = useState<string>("");
  const tagRef = useRef<HTMLInputElement>(null);

  const mode: ShipMode = cfg.data?.image_ship.mode ?? "ssh";
  const effBase = cfg.data?.image_ship.base_url ?? "";
  const base = baseDraft ?? effBase;
  const items = store.data?.items ?? [];
  const free = store.data?.store_free_bytes ?? null;
  const hostList = (hosts.data ?? []).filter((h) => h.enabled !== false);
  const baseHelp = "这是目标机去拉制品时用哪个公网地址，上面制品表里那些链接的域名就是它。"
    + "留空 = 跟着站点的公网域走（站点以后换域名，链接自己跟着变）；填了就以填的为准。"
    + (mode === "ssh" ? "现在是「SSH 直传」，这一条暂时用不上——切到「控制台下载」才轮到它。" : "");

  /** PUT 是局部更新，回来的就是新的生效值，直接拿回执渲染，不再多问一次。 */
  const applyPatch = async (patch: Parameters<typeof api.putSettings>[0], done: string) => {
    setBusy(true); setErr(null);
    try {
      const r = await api.putSettings(patch);
      if (r && r.image_ship) cfg.setData(r); else void cfg.reload();
      say(done);
    } catch (e) {
      setErr(msgOf(e));
    } finally { setBusy(false); }
  };

  const switchMode = (m: ShipMode) => {
    if (m === mode || busy) return;
    void applyPatch({ image_ship: { mode: m } }, `已切到「${SHIP_MODE_META[m].zh}」，下一轮开通就按它走`);
  };
  const saveBase = (v: string) => {
    if (busy) return;
    const t = v.trim();
    if (t === effBase) { setBaseDraft(null); say("下载基址没变，不用改"); return; }
    void applyPatch({ image_ship: { base_url: t } },
      t ? `下载基址已改成 ${t}` : "已清空：下载基址交回站点公网域");
    setBaseDraft(null);
    // 制品的 url 是按 base_url 现拼的（§6.1.5：deploy 每轮现读），基址一改，下面那张表
    // 里的链接就得跟着重拉——留着旧域名，他会照着旧链接去下，那地址已经不通了。
    void store.reload();
  };

  const doPurgeArtifact = async () => {
    if (!op) return;
    setBusy(true); setErr(null);
    try {
      await api.deleteImage(op.id);
      setOp(null);
      void store.reload();
      say(`${op.tag} 已下架：文件删了，那条链接当场失效`);
    } catch (e) {
      // 404 = 这一行已经不在了（另一个人先下架了，或同标签刚被重发顶掉）：
      // 后果已经达成，没什么可重试的，把表刷成真的就好，别留一张写着旧大小的卡。
      if (e instanceof ApiError && e.status === 404) { setOp(null); void store.reload(); }
      setErr(msgOf(e));
    } finally { setBusy(false); }
  };

  const publish = async () => {
    const t = tag.trim();
    setErr(null);
    if (!t) { setErr("先写上要发布哪个镜像标签，例如 dsh-family:1.14.2"); tagRef.current?.focus(); return; }
    if (fromHost === "") { setErr("先选一台源主机——镜像得先从某台机器上取出来"); return; }
    setBusy(true);
    try {
      const r = await api.publishImage({ tag: t, from_host_id: Number(fromHost) });
      // 发布是一张常规工单（kind=publish），进度照常规工单页看，不在这页摆半屏日志
      nav(`/jobs/${r.job_id}?kind=publish`);
    } catch (e) {
      setErr(msgOf(e));
      setBusy(false);
    }
  };

  return (
    <main className="shell">
      <div className="list-head">
        <h1>设置<HelpTip label="这一页动的是什么"
          text="这一页动的都是站点级的东西：改完立刻生效，下一轮开通就按新的走，不用重启控制台。" /></h1>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => { void cfg.reload(); void store.reload(); }} disabled={busy}>
          <Icon name="refresh" size={13} />刷新
        </button>
      </div>

      {(err || cfg.error || store.error || hosts.error) && (
        <div className="alert alert-warn" role="alert" style={{ marginTop: 0, marginBottom: 14 }}>
          <Icon name="cross" /><span>{err ?? `这页的数据没拉全：${cfg.error ?? store.error ?? hosts.error}`}</span>
        </div>
      )}

      <div className="stack">
      {/* —— 1 & 2：分发方式 + 下载基址（同一份 site.image_ship，所以在同一张卡里）—— */}
      <section className="card" aria-label="镜像分发">
        <div className="card-head">
          <h2>镜像分发<HelpTip label="镜像分发"
            text="开通实例时目标机上没有那个镜像，控制台就按这里选的路径把镜像搬过去。两边都不用你动手搬。" /></h2>
        </div>
        <div className="card-body">
          <div className="mode-pick" role="radiogroup" aria-label="分发方式">
            {(Object.keys(SHIP_MODE_META) as ShipMode[]).map((m) => {
              const on = m === mode;
              return (
                <div key={m} className={`mode-card${on ? " on" : ""}`}>
                  <button type="button" role="radio" aria-checked={on} disabled={busy}
                    className="mc-pick" onClick={() => switchMode(m)}>
                    <span className="mc-dot" aria-hidden />
                    <b>{SHIP_MODE_META[m].zh}</b>
                    <span className="mc-use">{SHIP_MODE_META[m].use}</span>
                  </button>
                  <HelpTip label={`${SHIP_MODE_META[m].zh}是怎么搬的`} text={SHIP_MODE_META[m].note} />
                  {on && <span className="mc-on-tag">当前</span>}
                </div>
              );
            })}
          </div>

          <div className="base-row">
            <label className="base-field" htmlFor="base-url">
              <span className="field-cap">下载基址<HelpTip label="下载基址" text={baseHelp} /></span>
              <input id="base-url" className="input mono" type="text" value={base}
                placeholder="留空 = 用站点公网域" disabled={busy}
                onChange={(e) => setBaseDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") saveBase(base); }} />
            </label>
            <button type="button" className="btn btn-primary" disabled={busy || baseDraft === null}
              onClick={() => saveBase(base)}>保存</button>
            <button type="button" className="btn" disabled={busy || base === ""}
              onClick={() => saveBase("")} title="清空这个框：下载链接改用站点自己的公网域">
              交回站点公网域
            </button>
          </div>
        </div>
      </section>

      {/* —— 3：制品表 —— */}
      <section className="card" aria-label="制品库">
        <div className="card-head">
          <h2>制品库</h2>
          <span className="spacer" />
          {free != null && (
            <span className="mut small" title="制品库挂在哪个目录由控制台的环境变量定">
              剩余空间 <b className="num">{fmtBytes(free)}</b>
            </span>
          )}
          <span className="mut small">{items.length} 个制品</span>
        </div>
        <div className="card-body">
          {items.length === 0 && store.loading && (
            <>
              <div className="skeleton sk-line" style={{ width: "26%" }} />
              <div className="skeleton sk-line" style={{ height: 58 }} />
              <div className="skeleton sk-line" style={{ height: 58, width: "82%" }} />
            </>
          )}
          {items.length === 0 && !store.loading && (
            <div className="hostcap">
              <span>还没有发布过制品。下面选好源主机、写上镜像标签，点一次就发好了。</span>
              <button type="button" className="btn btn-sm" onClick={() => {
                tagRef.current?.scrollIntoView({ block: "center" });
                tagRef.current?.focus();
              }}>去发布</button>
            </div>
          )}
          {mode === "download" && items.length === 0 && !store.loading && (
            <p className="small" style={{ color: "var(--warn)", marginTop: 10 }}>
              现在是「控制台下载」模式，可库里一个制品都没有：开通缺镜像的实例会在这一步失败。
              要么先把镜像发上来，要么切回「SSH 直传」。
            </p>
          )}

          {items.length > 0 && (
            <div className="tbl-wrap">
              <table className="tbl img-tbl">
                <thead>
                  <tr>
                    <th className="col-tag">镜像标签</th><th className="col-size">大小</th><th className="col-when">发布时间</th>
                    <th>校验码 sha256</th>
                    <th>公开链接（免登录）<HelpTip label="公开链接"
                      text="链接里的 token 就是门票：泄链接=泄镜像。要撤销就下架，链接当场失效。" /></th><th><span className="sr-only">操作</span></th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((a) => (
                    <tr key={a.id}>
                      <td className="col-tag">
                        <span className="row-title mono">{a.tag}</span>
                        {a.origin_host && <div className="row-sub">从 {a.origin_host} 取的</div>}
                      </td>
                      <td className="num col-size">{fmtBytes(a.size_bytes)}</td>
                      <td className="col-when" title={fmtFull(a.created_at)}>{fmtRel(a.created_at)}</td>
                      <td>
                        {openSha === a.id ? (
                          <button type="button" className="sha-full mono" title="点一下收起"
                            onClick={() => setOpenSha(null)}>{a.sha256 ?? "没算出来"}</button>
                        ) : (
                          <button type="button" className="sha-btn mono" title="点一下看完整校验码"
                            onClick={() => setOpenSha(a.id)}>{(a.sha256 ?? "—").slice(0, 8)}</button>
                        )}
                        <CopyButton text={a.sha256} label="" title="复制完整校验码" />
                      </td>
                      <td className="cell-url">
                        <span className="mono url-mask" title={a.url ?? undefined}>{maskedUrl(a)}</span>
                        <CopyButton text={a.url} label="复制链接" title="整条链接免登录，谁拿到谁能下" />
                      </td>
                      <td className="cell-ops col-ops">
                        <button type="button" className="btn btn-danger" disabled={busy || op !== null}
                          onClick={() => setOp(a)}>下架…</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* 下架是不可逆的：一屏把后果说完，确认用释放那套长按（§9） */}
          {op && (
            <ConsequenceCard
              title="下架制品" no={`${op.tag} · ${fmtBytes(op.size_bytes)}`}
              head={<>下架是<b>删文件</b>：制品库里那个 <span className="mono">{fileName(op)}</span> 直接没了。</>}
              rows={[
                { cap: "删掉的文件", val: <span className="mono">{fileName(op)}（{fmtBytes(op.size_bytes)}）</span> },
                { cap: "立刻失效", val: <span>这条公开链接当场 404，已经把它抄走的人在用的那一份也一起废掉</span> },
                { cap: "正在拉的", val: <span>目标机正在 curl 的这一把会断，那一轮开通失败</span> },
                { cap: "不受影响", val: <span>已经 load 进目标机的镜像、以及所有在跑的实例，都不动</span> },
              ]}
              due={<><b>要再用得重新发布一次</b>，而且是新的 token、新的一条链接。</>}
              hold confirmLabel="按住签字下架" holdAriaLabel="按住 1.5 秒确认下架"
              onConfirm={() => void doPurgeArtifact()} onCancel={() => setOp(null)} busy={busy}
            />
          )}
        </div>
      </section>

      {/* —— 4：从主机发布 —— */}
      <section className="card" aria-label="从主机发布">
        <div className="card-head">
          <h2>从主机发布<HelpTip label="从主机发布"
            text="在源机上把镜像 docker save | gzip 流进制品库，算好 sha256 再登记，GB 级要等几分钟——点完就跳工单页看进度，这一页不用等。同一个标签重发是原子替换：新的落库成功才删旧的，旧链接当场作废。" /></h2>
        </div>
        <div className="card-body">
          <div className="pub-form">
            <label className="pub-field" htmlFor="pub-host">
              <span className="field-cap">源主机</span>
              <select id="pub-host" className="input" value={fromHost} disabled={busy}
                onChange={(e) => setFromHost(e.target.value)}>
                <option value="">选一台已经装着这个镜像的机器</option>
                {hostList.map((h) => <option key={h.id} value={h.id}>{h.name}</option>)}
              </select>
            </label>
            <label className="pub-field grow" htmlFor="pub-tag">
              <span className="field-cap">镜像标签</span>
              <input id="pub-tag" ref={tagRef} className="input mono" type="text" value={tag}
                placeholder="dsh-family:x.y.z" list="known-tags" disabled={busy}
                onChange={(e) => setTag(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") void publish(); }} />
            </label>
            <button type="button" className="btn btn-primary btn-lg" disabled={busy}
              onClick={() => void publish()}>
              <Icon name="publish" />{busy ? "已提交，正在排队…" : "开始发布"}
            </button>
          </div>
          <datalist id="known-tags">{items.map((a) => <option key={a.id} value={a.tag} />)}</datalist>
        </div>
      </section>

      </div>

      {toast && <Toast text={toast} />}
    </main>
  );
}
