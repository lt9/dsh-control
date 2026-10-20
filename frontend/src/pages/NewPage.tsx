import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api/client";
import { useAsync } from "../lib/hooks";
import { Icon } from "../components/ui";

/**
 * 新建 = 零输入（§9）：一张已经自动填好的确认卡 + 一个「开始部署」。
 * 域名、备注、端口全由服务端生成；换主机是卡片上的一次点击。
 */
export default function NewPage() {
  const nav = useNavigate();
  const hosts = useAsync(() => api.hosts(), []);
  const rows = useAsync(() => api.instances(), []);
  const [hostId, setHostId] = useState<number | null>(null); // null = 服务端按剩余空间挑
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const zone = useMemo(() => {
    const withDns = (rows.data ?? []).map((r) => r.dns_name).find(Boolean);
    return withDns?.includes(".") ? withDns.slice(withDns.indexOf(".")) : null;
  }, [rows.data]);
  const nextSlug = useMemo(() => {
    let max = 0;
    for (const r of rows.data ?? []) {
      const m = /^dsh-(\d+)$/.exec(r.slug);
      if (m) max = Math.max(max, Number(m[1]));
    }
    return `dsh-${String(max + 1).padStart(2, "0")}`;
  }, [rows.data]);
  const hostList = (hosts.data ?? []).filter((h) => h.enabled !== false);
  const picked = hostList.find((h) => h.id === hostId) ?? null;

  const start = async () => {
    setBusy(true); setError(null);
    try {
      const r = await api.create(hostId == null ? {} : { host_id: hostId });
      nav(`/jobs/${r.job_id}?instance=${r.instance_id}&kind=deploy`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <main className="shell">
      <div className="confirm-wrap">
        <button type="button" className="btn btn-ghost" onClick={() => nav("/")}>
          <Icon name="back" size={13} />回台账
        </button>
        <div className="confirm-card card" style={{ marginTop: 8 }}>
          <div className="card-head"><h2>新建实例 · 确认卡</h2></div>
          <div className="card-body">
            <p className="mut small" style={{ marginBottom: 14 }}>
              下面每一项都是程序生成好的，看一眼没问题就点开始部署；名字以后不能改。
            </p>
            <dl className="kv">
              <dt>域名</dt>
              <dd>
                <span className="mono" style={{ fontSize: 15 }}>{nextSlug}{zone ?? ""}</span>
                <span className="auto-tag">自动生成</span>
                {!zone && <div className="mut small">后缀由服务器统一配置，部署时补全</div>}
              </dd>
              <dt>所在主机</dt>
              <dd>
                <span className="host-pick" role="radiogroup" aria-label="选择主机">
                  <button type="button" role="radio" aria-checked={hostId === null}
                    className={`btn${hostId === null ? " on" : ""}`} onClick={() => setHostId(null)}>
                    自动挑（剩余空间最大的）
                  </button>
                  {hostList.map((h) => (
                    <button key={h.id} type="button" role="radio" aria-checked={hostId === h.id}
                      className={`btn${hostId === h.id ? " on" : ""}`} onClick={() => setHostId(h.id)}>
                      {h.name}
                    </button>
                  ))}
                </span>
                {hosts.error && <div className="mut small">主机清单没拉到：{hosts.error}</div>}
                {(picked?.note ?? picked?.data_root) && (
                  <div className="mut small mono">{picked.data_root}{picked.note ? ` · ${picked.note}` : ""}</div>
                )}
              </dd>
              <dt>数据盘</dt>
              <dd className="mono">{picked?.data_root ?? "所选主机的默认数据目录"}<span className="auto-tag">自动</span></dd>
              <dt>资源</dt>
              <dd>全家统一规格（配置版本 v1）<span className="auto-tag">自动生成</span></dd>
              <dt>公网端口</dt>
              <dd>从配置起点向上找第一个空位<span className="auto-tag">自动分配</span></dd>
              <dt>备注名</dt>
              <dd className="mut small">先不起名，建好后详情页点铅笔随时改</dd>
            </dl>
            {error && (
              <div className="alert alert-err" role="alert">
                <Icon name="cross" /><span>没开起来：{error}</span>
              </div>
            )}
            <div className="confirm-foot">
              <button type="button" className="btn btn-primary btn-lg" disabled={busy} onClick={start}>
                {busy ? "已提交，正在装船…" : "开始部署"}
              </button>
              <span className="mut small">九步开通，随时可回台账看进度</span>
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
