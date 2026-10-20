import { useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, type InstanceDetail, type Step } from "../api/client";
import { JOB_META, JOB_STATE_META, STEP_STATE_META, fmtClock } from "../lib/format";
import { useJobFeed } from "../lib/hooks";
import { CopyButton, Icon, ToneChip } from "../components/ui";

export default function JobPage() {
  const { id } = useParams();
  const nav = useNavigate();
  const [q] = useSearchParams();
  const instanceId = q.get("instance") ? Number(q.get("instance")) : null;
  const kind = (q.get("kind") as keyof typeof JOB_META) || "deploy";
  const feed = useJobFeed(id ? Number(id) : null);
  const job = feed.job;
  const done = job?.state === "succeeded" || job?.state === "failed";
  const meta = JOB_STATE_META[job?.state ?? "pending"];

  return (
    <main className="shell">
      <button type="button" className="btn btn-ghost" style={{ marginBottom: 4 }} onClick={() => nav("/")}>
        <Icon name="back" size={13} />回台账
      </button>
      <div className="job-head">
        <h1>{JOB_META[kind]?.zh ?? "任务"} · 工单 #{id}</h1>
        <ToneChip tone={meta.tone}>{meta.zh}</ToneChip>
        {instanceId && (
          <button type="button" className="btn btn-ghost" onClick={() => nav(`/instances/${instanceId}`)}>
            看这台实例
          </button>
        )}
      </div>

      {feed.error && (
        <div className="alert alert-err" role="alert"><Icon name="cross" /><span>{feed.error}</span></div>
      )}

      {job?.state === "succeeded" && kind === "deploy" && instanceId && (
        <DonePanel instanceId={instanceId} />
      )}
      {job?.state === "succeeded" && kind === "restart" && instanceId && (
        <DonePanel instanceId={instanceId} restart />
      )}
      {job?.state === "succeeded" && kind === "release" && (
        <div className="card done-panel">
          <h2>已释放</h2>
          <p className="mut">解析、隧道、站点已注销；容器已停止，数据原样保留。台账里这条档案永远可见，但不可恢复。</p>
        </div>
      )}
      {job?.state === "failed" && (
        <div className="card" style={{ marginBottom: 16 }}>
          <div className="card-head"><h2 className="tone-err" style={{ color: "var(--err)" }}>任务失败</h2></div>
          <div className="card-body">
            <p className="small">{job.error ?? "失败原因见下方步骤与日志的标红处。"}</p>
            {instanceId && (
              <p style={{ marginTop: 10 }}>
                <button type="button" className="btn" onClick={() => nav(`/instances/${instanceId}`)}>去实例详情看异常记录</button>
              </p>
            )}
          </div>
        </div>
      )}

      <div className="job-cols">
        <section className="card" aria-label="步骤">
          <div className="card-head"><h2>点对点进度</h2></div>
          <div className="card-body" style={{ padding: "4px 16px 12px" }}>
            <ol className="steps">
              {(job?.steps ?? []).map((s) => <StepRow key={s.seq} s={s} />)}
              {!job && <li className="s-pending"><span className="seq">…</span><span className="st mut">等待工单数据…</span></li>}
            </ol>
          </div>
        </section>

        <LogPanel logs={feed.logs} transport={feed.transport} running={!done} />
      </div>
    </main>
  );
}

function StepRow({ s }: { s: Step }) {
  const m = STEP_STATE_META[s.state];
  const stepNo = Math.round(s.seq / 10);
  return (
    <li className={`s-${s.state}`}>
      <span className="seq num" aria-hidden>{stepNo}</span>
      <span className="st">
        <b>{s.title}</b>
        <span className={`st-state tone-${m.tone}`}>{m.zh}</span>
        {s.detail && <div className="detail mono">{s.detail}</div>}
      </span>
    </li>
  );
}

function LogPanel({ logs, transport, running }: {
  logs: { id: number; level: string; line: string; ts: string }[];
  transport: "sse" | "poll"; running: boolean;
}) {
  const box = useRef<HTMLPreElement>(null);
  const [follow, setFollow] = useState(true);
  const [pinnedCount, setPinnedCount] = useState(0);
  const lastLen = useRef(0);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    if (follow) {
      el.scrollTop = el.scrollHeight;
      setPinnedCount(0);
    } else if (logs.length !== lastLen.current) {
      setPinnedCount(logs.length - lastLen.current);
    }
    lastLen.current = logs.length;
  }, [logs, follow]);

  const onScroll = () => {
    const el = box.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 36;
    if (!nearBottom && follow) setFollow(false);
    else if (nearBottom && !follow) setFollow(true); // 手动滚回底部 = 重新跟滚
  };

  return (
    <section className="card logcard" aria-label="部署日志">
      <div className="card-head">
        <h2>日志</h2><span className="spacer" />
        <span className="mut small">
          {transport === "sse" ? "SSE 实时流" : "增量轮询"}{running ? " · 进行中" : " · 已结束"}
        </span>
      </div>
      <pre ref={box} className="logpanel scroll-dark" onScroll={onScroll} aria-live="polite">
        {logs.length === 0 && <span className="lt">还没吐日志…</span>}
        {logs.map((l) => (
          <div key={l.id}>
            <span className="lt">{fmtClock(l.ts)}</span>
            <span className={`ll ll-${l.level}`}>{(l.level || "info").toUpperCase().slice(0, 4)}</span>
            {l.line}
          </div>
        ))}
      </pre>
      <div className="log-foot">
        <button type="button" className={`btn${follow ? " on" : ""}`}
          aria-pressed={follow}
          onClick={() => { setFollow((v) => !v); }}>
          {follow ? "自动滚到底：开" : "自动滚到底：已暂停"}
        </button>
        {!follow && pinnedCount > 0 && (
          <button type="button" className="btn" onClick={() => {
            setFollow(true);
            if (box.current) box.current.scrollTop = box.current.scrollHeight;
          }}>↓ {pinnedCount} 行新日志，跳到底</button>
        )}
        <span style={{ marginLeft: "auto" }}>{logs.length} 行</span>
      </div>
    </section>
  );
}

/** 部署完成：大字给可复制的一键登录地址。 */
function DonePanel({ instanceId, restart = false }: { instanceId: number; restart?: boolean }) {
  const [det, setDet] = useState<InstanceDetail | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const got = useRef(false);

  useEffect(() => {
    if (got.current) return;
    got.current = true;
    void (async () => {
      try {
        const d = await api.instance(instanceId);
        setDet(d);
        // 已回收的实例没有可用的公网入口，别再"现取一键地址"（容器已停，取到也是死链，
        // 还会让外行以为这串地址照样能用）。
        if (d.state === "released") return;
        const t = await api.token(instanceId);
        setUrl(t.url ?? null);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
      }
    })();
  }, [instanceId]);

  const gone = det?.state === "released";

  return (
    <div className="card done-panel" style={{ marginBottom: 16 }}>
      <h2>{restart ? "已重启，密钥已换新" : "开通完成"}</h2>
      <p className="mut small">{restart ? "旧密钥已失效，下面这串是新的。" : "把这串地址发给他：手机打开就能用，不用再登录。"}</p>
      {det && <p className="small">{det.owner_label || det.slug} · <span className="mono">{det.network?.dns_name}</span></p>}
      {gone
        ? <p className="mut small" style={{ marginTop: 12 }}>这台已回收：公网入口注销、容器已停，
            地址不再有效（数据还在，档案留着）。</p>
        : url
        ? <>
            <code className="big-url">{url}</code>
            <CopyButton text={url} label="复制一键地址" big />
          </>
        : <p className="mut small" style={{ marginTop: 12 }}>{err ? `一键地址现取失败：${err}` : "正在现取一键地址…"}</p>}
    </div>
  );
}
