import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, type InstanceRow, type InstanceState } from "../api/client";
import { useAsync } from "../lib/hooks";
import { fmtRel, fmtFull } from "../lib/format";
import { Icon, StateChip } from "../components/ui";

type Filter = "all" | "alive" | "trouble" | "released";

const TROUBLE: InstanceState[] = ["unhealthy", "restarting", "error"];

export default function ListPage() {
  const nav = useNavigate();
  const [sp, setSp] = useSearchParams();
  const q = sp.get("q") ?? "";
  const [qInput, setQInput] = useState(q);
  const searchRef = useRef<HTMLInputElement>(null);

  // 搜索 = 用户主动发起的检索，走服务端 ?q=（§4）；输入即搜，250ms 防抖。
  useEffect(() => { setQInput(q); }, [q]); // 前进/后退回来时同步
  useEffect(() => {
    if (qInput === q) return;
    const t = setTimeout(() => {
      const next = new URLSearchParams(sp);
      if (qInput) next.set("q", qInput); else next.delete("q");
      setSp(next, { replace: true });
    }, 250);
    return () => clearTimeout(t);
  }, [qInput]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {  // 「/」聚焦搜索（输入框里打字时不抢）
    const onKey = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (e.key === "/" && tag !== "INPUT" && tag !== "TEXTAREA") {
        e.preventDefault(); searchRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const rows = useAsync(() => api.instances(q || undefined), [q]);
  const ov = useAsync(() => api.overview(), []);
  const [filter, setFilter] = useState<Filter>("all");
  const [showReleased, setShowReleased] = useState(false);

  const [main, released] = useMemo(() => {
    const all = rows.data ?? [];
    return [all.filter((r) => r.state !== "released"), all.filter((r) => r.state === "released")];
  }, [rows.data]);

  const counts = useMemo(() => {
    const all = rows.data ?? [];
    return {
      alive: all.filter((r) => r.state === "running" || r.state === "deploying" || r.state === "pending").length,
      trouble: all.filter((r) => TROUBLE.includes(r.state)).length,
      released: released.length,
      total: all.length,
    };
  }, [rows.data, released]);

  const shown = useMemo(() => {
    if (filter === "alive") return main.filter((r) => r.state === "running" || r.state === "deploying" || r.state === "pending");
    if (filter === "trouble") return main.filter((r) => TROUBLE.includes(r.state));
    return main;
  }, [main, filter]);

  const firstRun = !q && !rows.loading && !rows.error && counts.total === 0;

  const capHosts = useMemo(() => (ov.data?.by_host ?? []) as Record<string, unknown>[], [ov.data]);
  const clearQ = () => { setQInput(""); const next = new URLSearchParams(sp); next.delete("q"); setSp(next, { replace: true }); };

  if (firstRun) {
    return (
      <main className="shell">
        <FirstRun onNew={() => nav("/instances/new")} />
      </main>
    );
  }

  return (
    <main className="shell">
      <h1 className="sr-only">实例台账</h1>
      <div className="list-head">
        <div className="search-box">
          <Icon name="search" size={15} />
          <input ref={searchRef} value={qInput} aria-label="搜索实例"
            onChange={(e) => setQInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") clearQ(); }}
            placeholder="搜备注名 / 域名 / dsh-01…（按 / 直达）"
            autoComplete="off" spellCheck={false} />
          {(qInput || q) && (
            <button type="button" className="search-clear" aria-label="清空搜索" onClick={clearQ}>
              <Icon name="close" size={13} />
            </button>
          )}
        </div>
        {q && <span className="match-note" role="status">
          {rows.loading ? "搜索中…" : `匹配 ${counts.total} 条`}
        </span>}
        <span className="spacer" />
        <button type="button" className="btn btn-primary btn-lg" onClick={() => nav("/instances/new")}>
          <Icon name="plus" />新建实例
        </button>
      </div>

      <div className="stats" role="group" aria-label="按状态筛选">
        <FilterStat on={filter === "all"} label="全部" n={counts.total} onClick={() => setFilter("all")} />
        <FilterStat on={filter === "alive"} label="运行中" n={counts.alive} tone="t-ok" onClick={() => setFilter("alive")} />
        <FilterStat on={filter === "trouble"} label="要关注" n={counts.trouble} tone="t-warn" onClick={() => setFilter("trouble")} />
        <span className="stat" style={{ cursor: "default" }}>
          <b className="num">{ov.data?.hosts ?? "—"}</b>台主机
        </span>
      </div>

      {rows.error && (
        <div className="alert alert-err" role="alert">
          <Icon name="cross" /><span>台账没拉起来：{rows.error}
            <button type="button" className="btn btn-ghost" onClick={() => void rows.reload()}>重试</button>
          </span>
        </div>
      )}

      {rows.loading && !rows.data && (
        <div className="card" aria-busy="true">
          <div className="skeleton sk-line" style={{ width: "30%" }} />
          <div className="skeleton sk-line" />
          <div className="skeleton sk-line" style={{ width: "85%" }} />
          <div className="skeleton sk-line" style={{ width: "60%" }} />
        </div>
      )}

      {!rows.loading && (
        <section aria-label="在册实例">
          <div className="card">
            <div className="tbl-wrap scroll-light">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>实例</th><th>状态</th><th>DSH 版本</th><th>配置</th>
                    <th>所在主机</th><th>域名</th><th>更新时间</th>
                  </tr>
                </thead>
                <tbody>
                  {shown.length === 0 && (
                    <tr><td colSpan={7} style={{ padding: 22 }} className="mut">
                      {q
                        ? <>没有匹配「<b>{q}</b>」的实例——备注名、域名、dsh-01 这样的标签都搜得到。
                            <button type="button" className="btn btn-ghost" onClick={clearQ}>清空搜索</button></>
                        : <>这个筛选下没有实例。<button type="button" className="btn btn-ghost" onClick={() => setFilter("all")}>看全部</button></>}
                    </td></tr>
                  )}
                  {shown.map((r) => <Row key={r.id} r={r} onOpen={() => nav(`/instances/${r.id}`)} />)}
                </tbody>
              </table>
            </div>
          </div>

          {capHosts.length > 0 && !q && (
            <div className="hostcap" aria-label="主机容量">
              <span className="cap-label">主机容量</span>
              {capHosts.map((h, i) => {
                const free = num(h.free_gb ?? h.disk_free_gb);
                return (
                  <span key={i} className="hc">
                    <b>{String(h.host ?? `主机${i + 1}`)}</b>
                    <span className="num">{num(h.total) != null ? `${num(h.total)} 台` : ""}</span>
                    {free != null && <span className="num">数据盘剩 {fmtGB(free)}</span>}
                    {h.note ? <span className="mut">{String(h.note)}</span> : null}
                  </span>
                );
              })}
              <span className="mut" style={{ marginLeft: "auto" }}>新建时按剩余空间自动挑</span>
            </div>
          )}

          {released.length > 0 && (
            <div className="released-zone">
              <button type="button" className="zone-toggle" aria-expanded={showReleased}
                onClick={() => setShowReleased((v) => !v)}>
                <Icon name="chevron" />
                已释放 · {released.length}
                <span className="zone-note">档案保留，不可恢复</span>
              </button>
              {showReleased && (
                <div className="card" style={{ marginTop: 8 }}>
                  <div className="tbl-wrap scroll-light">
                    <table className="tbl">
                      <thead>
                        <tr>
                          <th>实例</th><th>状态</th><th>DSH 版本</th><th>配置</th>
                          <th>所在主机</th><th>当年域名</th><th>释放时间</th>
                        </tr>
                      </thead>
                      <tbody>
                        {released.map((r) => <Row key={r.id} r={r} released onOpen={() => nav(`/instances/${r.id}`)} />)}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}
            </div>
          )}
        </section>
      )}
    </main>
  );
}

const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
function fmtGB(gb: number) {
  return gb >= 1024 ? `${(gb / 1024).toFixed(gb >= 10240 ? 0 : 1)} T` : `${Math.round(gb)} G`;
}

function FilterStat({ on, label, n, tone, onClick }: {
  on: boolean; label: string; n: number; tone?: string; onClick: () => void;
}) {
  return (
    <button type="button" className={`stat${on ? " on" : ""}${tone ? ` ${tone}` : ""}`}
      onClick={onClick} aria-pressed={on}>
      <b className="num">{n}</b>{label}
    </button>
  );
}

function Row({ r, released = false, onOpen }: { r: InstanceRow; released?: boolean; onOpen: () => void }) {
  const title = r.owner_label || r.slug;
  const sub = r.owner_label ? r.slug : null;
  return (
    <tr className="clickable" tabIndex={0} role="link" aria-label={`打开 ${title} 详情`}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onOpen(); } }}>
      <td>
        <div className="row-title">{title}</div>
        {sub && <div className="row-sub mono">{sub}</div>}
      </td>
      <td><StateChip state={r.state} size="sm" /></td>
      <td className="mono">{r.dsh_version ?? "—"}</td>
      <td className="mono">{r.config_version ?? "—"}</td>
      <td>{r.host_name ?? "—"}</td>
      <td className="mono">{r.dns_name ?? "—"}</td>
      <td className="num" title={fmtFull(r.updated_at)}>{released ? "—" : fmtRel(r.updated_at)}</td>
    </tr>
  );
}

/** 空状态 = 第一次进来该怎么走（新系统第一屏，按首跑设计）。 */
function FirstRun({ onNew }: { onNew: () => void }) {
  return (
    <section className="first-run">
      <h1>台账还是空的</h1>
      <p className="lead">
        这里是全家 DSH 实例的总台：开一条实例，就是替他配好「一条域名 + 一条隧道 +
        一个反代站点 + 一个容器」，以后看状态、重启、回收都从这一屏走。
      </p>
      <ol className="steps-guide">
        <li><b>点「新建实例」</b><span>确认卡会自动填好——名字、域名、主机都不用你挑。</span></li>
        <li><b>看它装船</b><span>九步开通逐条点亮，日志实时滚动，出错当场标出来。</span></li>
        <li><b>把一键地址发给他</b><span>部署完成后大字给出，手机打开就能用，不用再登录。</span></li>
      </ol>
      <button type="button" className="btn btn-primary btn-lg" onClick={onNew}>
        <Icon name="plus" />新建实例
      </button>
    </section>
  );
}
