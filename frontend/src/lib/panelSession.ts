// MemoryPanel 同源嵌入的引导会话（MEMORY-PANEL-EMBED.md ③-B 的落地）。
// 全部逻辑抽成纯函数：写哪个 key、写什么形状、什么算「被 Panel 清掉」，
// 都可以在没有 DOM 的环境里断言。

/** Panel 前端认的会话键（`web/src/lib/panelSession.ts` 里的 `STORAGE_KEY`）。 */
export const PANEL_SESSION_KEY = "tdai-panel.session";

/** iframe 的深链：hash 路由直达记忆页（③-B）。 */
export const PANEL_MEMORY_SRC = "/panel/#/memory";

export interface PanelSession {
  authMethod: "idp";
  instanceId: string;
  /** 空串 = Panel 前端自己不发 X-Tdai-User-Key，身份由 /api/v1 反代在服务端注入
   *  （credentials kind=memory_admin_key）。业务 key 从头到尾不进浏览器。 */
  userKey: string;
  user: { user_id: string; display_name: string };
}

/** 占位身份：只在问不到 admin key 真实主体时用（Panel 拿 user_id 去 core 查
 * 「我的 team」，占位名查出来就是团队（0）——2026-10-29 现网实证）。 */
export const FALLBACK_PANEL_IDENTITY: PanelSession["user"] = {
  user_id: "dshctl-console",
  display_name: "控制台运维",
};

export function buildPanelSession(user?: PanelSession["user"]): PanelSession {
  return {
    authMethod: "idp",
    instanceId: "default",
    userKey: "",
    user: user && user.user_id ? user : FALLBACK_PANEL_IDENTITY,
  };
}

/** 建 iframe **之前**调用（顺序就是契约：先写会话，后挂框，Panel 一睁眼就是登录态）。 */
export function writePanelSession(
  user?: PanelSession["user"],
  store: Pick<Storage, "setItem"> = localStorage,
): void {
  try {
    store.setItem(PANEL_SESSION_KEY, JSON.stringify(buildPanelSession(user)));
  } catch {
    // 隐私模式写不进：iframe 会自己撞登录门，页面不因此崩，也不该把运维登出。
  }
}

/** Panel 收 401 会清自己的 localStorage 并广播 storage 事件（stores/auth.ts:74-89）。
 *  控制台收到这个只回落到「记忆凭据失效」提示——这不是控制台登录过期，
 *  绝不能把运维登出。事件过滤也抽出来：key 对、且是被清空（newValue=null）才算。 */
export function isPanelSessionCleared(
  ev: { key: string | null; newValue: string | null },
): boolean {
  return ev.key === PANEL_SESSION_KEY && ev.newValue === null;
}
