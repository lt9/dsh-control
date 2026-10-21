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
  const ov = useAsync(() => api.overview(), []);
  const [hostId, setHostId] = useState<number | null>(null); // null = 服务端按剩余空间挑
  const [busy, setBusy] = useState(false);
  // 输入框收 slug（dsh-04）也收卡片上显示的那个完整域名（dsh-04.example.test）：
  // 上一版只认 slug，页面却写着"把域名打进输入框"，不懂技术的人打到第一步就卡住。
  const matchesTarget = (v: string) => {
    const s = v.trim().toLowerCase();
    if (!s || !nextSlug) return false;
    return s === nextSlug.toLowerCase() || s === (dnsName ?? "").toLowerCase();
  };
  const [armed, setArmed] = useState(false);
  const [typed, setTyped] = useState("");
  const [error, setError] = useState<string | null>(null);

  // 域名后缀首选站点配置（overview.dns_zone 等字段，防御式读），退而求其次从既有实例反推。
  const zone = useMemo(() => {
    const o = ov.data as unknown as Record<string, unknown> | null;
    const z = (o?.dns_zone ?? o?.zone ?? o?.domain_suffix ?? null) as string | null;
    if (z) return "." + String(z).replace(/^\.+/, "");
    const withDns = (rows.data ?? []).map((r) => r.dns_name).find(Boolean);
    return withDns?.includes(".") ? withDns.slice(withDns.indexOf(".")) : null;
  }, [ov.data, rows.data]);
  // 域名以前是前端自己数既有实例猜的（max+1）：与服务端各算一套，
  // 并发或改名时就会错位，确认卡上写的和真建出来的可能不是同一个。
  // 现在问服务端要（GET /instances/next-slug），创建时还要把它回显回去。
  const plan = useAsync(() => api.nextSlug(), []);
  const nextSlug = plan.data?.slug ?? null;
  const dnsName = plan.data?.dns_name ?? null;
  const hostList = (hosts.data ?? []).filter((h) => h.enabled !== false);
  const picked = hostList.find((h) => h.id === hostId) ?? null;

  const start = async () => {
    setBusy(true); setError(null);
    try {
      if (!nextSlug) throw new Error("还没拿到要创建的域名，刷新页面重试");
      const r = await api.create({ ...(hostId == null ? {} : { host_id: hostId }), confirm: nextSlug });
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
                <span className="mono" style={{ fontSize: 15 }}>
                  {dnsName ?? (nextSlug ? nextSlug + (zone ?? "") : "正在取号…")}
                </span>
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
              {/* 二次确认必须是"键盘动作"，不能是"第二次点击"。
                  上一版数点击次数，被真双击 / 自动化连发 mouseReleased+mouseClicked
                  直接穿过去，2026-10-21 现网误建了一台 dsh-03（已走释放流程回收）。
                  这一步会在云上建 DNS、开隧道、加反代站点并起容器，必须打到
                  输入框里的域名与卡片上显示的一致才允许提交。 */}
              {!armed ? (
                <>
                  <button type="button" className="btn btn-primary btn-lg" disabled={busy || !nextSlug}
                    onClick={() => { setArmed(true); setTyped(""); }}>
                    {busy ? "已提交，正在装船…" : nextSlug ? "开始部署" : "正在取号…"}
                  </button>
                  <span className="mut small">十步开通，随时可回台账看进度</span>
                </>
              ) : (
                <>
                  <input className="input" type="text" value={typed} autoFocus
                    aria-label={`输入 ${nextSlug ?? "域名"} 以确认新建`}
                    placeholder={`输入 ${nextSlug ?? "域名"}（或完整域名）确认`}
                    onChange={(e) => setTyped(e.target.value)} />
                  <button type="button" className="btn btn-primary btn-lg"
                    disabled={busy || !matchesTarget(typed)} onClick={() => void start()}>
                    {busy ? "已提交，正在装船…" : "确认新建"}
                  </button>
                  <button type="button" className="btn btn-ghost"
                    onClick={() => { setArmed(false); setTyped(""); }}>
                    返回改主意
                  </button>
                </>
              )}
            </div>
            {armed && (
              <p className="mut small" role="note">
                确认：会在云上新建 DNS、隧道、反代站点并在机器上起容器。请把上面卡片里的域名
                或完整域名原样打进输入框才允许提交——点两下不算确认。
              </p>
            )}
          </div>
        </div>
      </div>
    </main>
  );
}
