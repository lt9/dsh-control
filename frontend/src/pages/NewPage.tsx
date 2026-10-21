import { useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
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

  // 防"连点两次凭空开出一台公网实例"（2026-10-21 现网事故）不靠让人打字（§9 新建零打字）：
  // confirm 由这张卡回显（就是上面显示的那个名字），服务端逐字比对它刚算出的 slug——
  // 真双击的第二下带的是已经过期的名字，服务端直接 422，什么都不会建。
  // 前端这里再把提交那一下锁死：ref 同步置位，同一次点击事件循环里发不出第二个请求。
  const started = useRef(false);
  const start = async () => {
    if (started.current) return;
    if (!nextSlug) { setError("还没拿到要创建的域名，刷新页面重试"); return; }
    started.current = true;
    setBusy(true); setError(null);
    try {
      const r = await api.create({ ...(hostId == null ? {} : { host_id: hostId }), confirm: nextSlug });
      nav(`/jobs/${r.job_id}?instance=${r.instance_id}&kind=deploy`);
    } catch (e) {
      // 没建成才解锁让人再试；名字可能是被别人抢先占了，所以重新取一次号
      started.current = false;
      setBusy(false);
      void plan.reload();
      // 确认词不对 = 卡片上的名字已经不是服务端此刻给的那个了。后端那行解释是写给
      // "把名字填进确认框"的旧交互的，这一期没有输入框，这里按码说这一页自己的话。
      setError(e instanceof ApiError && e.code === "confirm_mismatch"
        ? "这个名字刚被别人抢先占了（或者是这一页放太久了）。已经重新取好号，照着卡片上新的那个名字再点一次。"
        : e instanceof Error ? e.message : String(e));
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
              <button type="button" className="btn btn-primary btn-lg" disabled={busy || !nextSlug}
                onClick={() => void start()}>
                {busy ? "已提交，正在装船…" : nextSlug ? "开始部署" : "正在取号…"}
              </button>
              <span className="mut small">十步开通，随时可回台账看进度</span>
            </div>
            <p className="mut small" role="note">
              确认：会在云上新建 DNS、隧道、反代站点并在机器上起容器。这一页不用打任何字：
              卡片上的 <span className="mono">{nextSlug ?? "正在取号…"}</span> 提交时原样回传给服务端核对，
              名字要是刚被别人抢先占了就直接拒绝，绝不会建成别的。
            </p>
          </div>
        </div>
      </div>
    </main>
  );
}
