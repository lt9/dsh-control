import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { api, type CatalogCapability } from "../api/client";
import { capKindZh } from "../lib/format";
import { useAsync } from "../lib/hooks";
import { HelpTip, Icon, ToneChip } from "../components/ui";

/** catalog state → 状态点。只有 published 能铺；draft/其他都算未发布。 */
function capStateChip(c: CatalogCapability) {
  if (c.state === "published") {
    return c.current
      ? <ToneChip tone="ok"><Icon name="round" size={10} />现行</ToneChip>
      : <ToneChip tone="off"><Icon name="hollow" size={10} />旧版</ToneChip>;
  }
  return <ToneChip tone="warn"><Icon name="triangle" size={10} />未发布</ToneChip>;
}

/**
 * 能力目录页（§vendor：能力包的四类 kind 一览）。
 * 发布走 CLI（dshctl capability publish），这里只读不发布——§9 减法，
 * 页面上放一句 HelpTip 说清「发布去哪」就够了，不摆一个点了只会报错的按钮。
 */
export default function CapabilityPage() {
  const nav = useNavigate();
  const [onlyCurrent, setOnlyCurrent] = useState(true);
  const catalog = useAsync(() => api.capabilities(), []);

  // 「被几台期望」后端没给，这里就不列（不自己拼 N+1，lead 明确要求）。
  const all = catalog.data ?? [];
  const rows = onlyCurrent ? all.filter((c) => c.current) : all;

  return (
    <main className="shell">
      <div className="list-head">
        <h1>能力目录
          <HelpTip label="这一页动的是什么"
            text="这里看全站登记的能力包（技能/补丁/插件/服务）。发布新能力走命令行（dshctl capability publish），网页只管看与给某台实例铺——所以这一页没有「发布」按钮，也不是漏了。" />
        </h1>
        <span className="spacer" />
        <button type="button" className={`btn${onlyCurrent ? " on" : ""}`}
          aria-pressed={onlyCurrent}
          onClick={() => setOnlyCurrent((v) => !v)}>
          只看现行版本：{onlyCurrent ? "开" : "关"}
        </button>
        <button type="button" className="btn" onClick={() => void catalog.reload()}
          disabled={catalog.loading}>
          <Icon name="refresh" size={13} />刷新
        </button>
      </div>

      <section className="card" aria-label="目录">
        <div className="card-body">
          {catalog.loading && catalog.data === null ? (
            <div className="skeleton sk-line" style={{ height: 120 }} />
          ) : catalog.error && catalog.data === null ? (
            <p className="mut small" role="status">能力目录拉不起来：{catalog.error}</p>
          ) : rows.length === 0 ? (
            <p className="mut small">
              {all.length === 0
                ? "还没有任何能力包：用 dshctl capability publish 发布第一个。"
                : "没有符合筛选的版本。"}
            </p>
          ) : (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr>
                    <th>标题</th><th>类型</th><th>现行版本</th><th>兼容 DSH</th><th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr key={`${c.capability_id}@${c.version}`}>
                      <td>
                        {c.title}
                        {c.default_on && (
                          <span className="chip chip-sm" style={{ marginLeft: 8 }}>默认随开通</span>
                        )}
                        {c.note && <div className="mut small">{c.note}</div>}
                      </td>
                      <td>{capKindZh(c.kind)}</td>
                      <td className="mono">{c.version}</td>
                      <td className="mono">{c.compatible_dsh || <span className="mut">不限</span>}</td>
                      <td>{capStateChip(c)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      <p className="mut small" style={{ marginTop: 12 }}>
        要给某台实例铺能力，去那台的详情页「能力」小块点动作（一次一张工单）。
        <button type="button" className="btn btn-ghost" onClick={() => nav("/")}>回台账挑一台</button>
      </p>
    </main>
  );
}
