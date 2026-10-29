import { useEffect, useState } from "react";
import { ApiError, api, type MemoryMember } from "../api/client";
import { useAsync } from "../lib/hooks";
import { HoldButton, HelpTip, Icon, ToneChip, Toast, useToast } from "../components/ui";
import { PANEL_MEMORY_SRC, isPanelSessionCleared, writePanelSession } from "../lib/panelSession";

const msgOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

/**
 * 记忆页（§9 第 6 屏）：上半成员治理（P4 最小版），下半「记忆深度管理」嵌 Panel。
 * Panel 走同源反代 /panel/（api/memory_proxy.py）：引导会话按 MEMORY-PANEL-EMBED.md ③-B
 * 在建 iframe 之前写入 localStorage；业务 key 从头到尾不进浏览器，身份由代理服务端注入。
 */
export default function MemoryPage() {
  // —— 成员治理 ——
  // 批量路由由后端补（lead）：没上线时 404 → 占位，这一页其余部分不因此空掉。
  const members = useAsync(async () => {
    try { return await api.memoryMembers(); }
    catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.code === "not_found")) return null;
      throw e;
    }
  }, []);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [opError, setOpError] = useState<string | null>(null);
  const { toast, say } = useToast();

  const act = async (m: MemoryMember, action: "disable" | "reclaim") => {
    setBusyId(`${m.user_id}:${action}`); setOpError(null);
    try {
      await api.memoryMemberAction(m.user_id, action);
      say(action === "reclaim"
        ? `${m.slug} 的记忆账户已回收：业务 key 吊销，记忆数据留在 Hub`
        : `${m.slug} 的记忆账户已停用：随时可再启用`);
      void members.reload();
    } catch (e) {
      setOpError(msgOf(e));
    } finally { setBusyId(null); }
  };

  // —— 记忆深度管理（Panel iframe）——
  const [deep, setDeep] = useState(false);
  const [credentialLost, setCredentialLost] = useState(false);
  const [frameKey, setFrameKey] = useState(0);

  const openDeep = () => {
    writePanelSession();          // 先写引导会话，后挂 iframe——顺序就是免登的前提
    setDeep(true);
  };
  const reenter = () => {
    writePanelSession();
    setCredentialLost(false);
    setFrameKey((k) => k + 1);    // 重挂一次，Panel 重新走 auth/session
  };

  useEffect(() => {
    if (!deep) return;
    // Panel 收 401 会清 localStorage 并广播 storage 事件。这里只降级这块的提示
    // ——那是记忆凭据的事，不是控制台登录的事，绝不把运维登出。
    const on = (ev: StorageEvent) => { if (isPanelSessionCleared(ev)) setCredentialLost(true); };
    window.addEventListener("storage", on);
    return () => window.removeEventListener("storage", on);
  }, [deep]);

  const rows = members.data;

  return (
    <main className="shell">
      <div className="list-head">
        <h1>记忆
          <HelpTip label="这一页动的是什么"
            text="上半数的是全家实例在记忆中枢的业务账户（谁在写记忆、key 状态）；下半把记忆中枢的深度管理界面嵌进来。记忆数据本身永远存在记忆中枢的卷里——这一页任何动作都不删数据。" />
        </h1>
        <span className="spacer" />
        <button type="button" className="btn" onClick={() => void members.reload()}
          disabled={busyId !== null}>
          <Icon name="refresh" size={13} />刷新
        </button>
      </div>

      {opError && <div className="alert alert-err" role="alert"><Icon name="cross" /><span>{opError}</span></div>}
      {toast && <Toast text={toast} />}

      {/* —— 上半：成员治理 —— */}
      <section className="card" aria-label="成员治理">
        <div className="card-head"><h2>成员治理</h2>
          <span className="spacer" />
          <HelpTip label="停用与回收的分别"
            text="停用＝这台的凭据暂时收起来，随时可再启用；回收＝吊销业务 key，不可逆——要再用回到实例详情页点「启用记忆」，系统会重新发一枚。两种动作都只动凭据，记忆数据一律留在 Hub 卷里。" />
        </div>
        <div className="card-body">
          {members.loading && rows === null ? (
            <div className="skeleton sk-line" style={{ height: 120 }} />
          ) : rows === null ? (
            <p className="mut small">成员治理的后端还没接上：这一格等它上线自动就有，不影响下面的记忆深度管理。</p>
          ) : members.error && rows === null ? (
            <p className="mut small" role="status">成员列表拉不起来：{members.error}</p>
          ) : rows.length === 0 ? (
            <p className="mut small">还没有业务用户：实例启用记忆后就会出现在这里。</p>
          ) : (
            <div className="tbl-wrap">
              <table className="tbl">
                <thead>
                  <tr><th>实例</th><th>记忆账户</th><th>业务 key</th><th>状态</th><th>动作</th></tr>
                </thead>
                <tbody>
                  {rows.map((m) => (
                    <tr key={m.user_id}>
                      <td className="mono">{m.slug}</td>
                      <td className="mono">{m.user_id}</td>
                      <td className="mono">
                        {m.key_masked || <span className="mut">—</span>}
                        {m.key_status === "invalid" && <span className="chip tone-warn chip-sm" style={{ marginLeft: 8 }}>key 已失效</span>}
                      </td>
                      <td>
                        {m.state === "disabled" ? (
                          <ToneChip tone="off"><Icon name="hollow" size={10} />已停用</ToneChip>
                        ) : m.state === "unknown" ? (
                          <ToneChip tone="wait"><Icon name="hollow" size={10} />状态未知</ToneChip>
                        ) : (
                          <ToneChip tone="ok"><Icon name="round" size={10} />在用</ToneChip>
                        )}
                        {m.state === "unknown" && m.hub_note && (
                          <span className="mut small" style={{ marginLeft: 8 }}>{m.hub_note}</span>
                        )}
                      </td>
                      <td>
                        <span className="actions">
                          {m.state === "active" && (
                            <button type="button" className="btn btn-sm"
                              disabled={busyId !== null}
                              onClick={() => void act(m, "disable")}>停用</button>
                          )}
                          <HoldButton ms={1500} disabled={busyId !== null}
                            ariaLabel={`按住 1.5 秒回收 ${m.slug} 的记忆账户`}
                            onHold={() => void act(m, "reclaim")}>
                            回收…
                          </HoldButton>
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>

      {/* —— 下半：记忆深度管理（Panel 同源 iframe）—— */}
      <section className="card" aria-label="记忆深度管理">
        <div className="card-head"><h2>记忆深度管理</h2>
          <span className="spacer" />
          <HelpTip label="这个界面是从哪来的"
            text="这是记忆中枢自己的深度管理界面（浏览与编辑各层记忆、知识库向导），由控制台服务端反代进来——业务 key 留在控制台库里，浏览器从不接触它。轻操作（启用、停用、回收）用上面那张表就够，不用进这里。" />
        </div>
        <div className="card-body">
          {!deep ? (
            <button type="button" className="btn btn-primary btn-lg" onClick={openDeep}>
              <Icon name="eye" />打开记忆深度管理
            </button>
          ) : (
            <>
              {credentialLost && (
                <div className="alert alert-warn" role="alert">
                  <Icon name="triangle" />
                  <span>记忆凭据失效：代不进记忆中枢。你的控制台登录没有受影响。<br />
                    去实例详情页重新启用记忆，或在这里点「重新进入」重试。</span>
                  <button type="button" className="btn btn-ghost" onClick={reenter}>重新进入</button>
                </div>
              )}
              <iframe key={frameKey} className="panel-frame" src={PANEL_MEMORY_SRC}
                title="记忆深度管理"
                sandbox="allow-scripts allow-same-origin allow-forms allow-popups" />
            </>
          )}
        </div>
      </section>
    </main>
  );
}
