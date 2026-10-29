// 前端纯函数单测：client.ts 错误词条 + panelSession 引导会话。
// 无测试框架依赖，直接 `node --experimental-strip-types tests/frontend.test.ts`。
// 断言的是「机器码 → 人话」映射与 §4.1 的 cannot_ 前缀退化——这两条是评审必查项，
// 也是错误文案唯一按码分情况的地方，得能在 CI 之前先跑绿。

import assert from "node:assert/strict";
import { errZh } from "../src/api/client.ts";
import {
  PANEL_SESSION_KEY, buildPanelSession, isPanelSessionCleared, writePanelSession,
} from "../src/lib/panelSession.ts";

let pass = 0;
function ok(name: string, fn: () => void) {
  try { fn(); pass += 1; console.log(`  ok  ${name}`); }
  catch (e) { console.error(`FAIL  ${name}`); throw e; }
}

// —— 错误词条（§4.1）——
ok("invalid_action 有中文词条", () => assert.equal(errZh("invalid_action"), "这个动作现在做不了"));
ok("memory_unavailable 有中文词条", () => assert.match(errZh("memory_unavailable"), /记忆中枢还没配好/));
ok("memory_admin_key_missing 有词条", () => assert.match(errZh("memory_admin_key_missing"), /记忆管理密钥/));
ok("rate_limited 沿用既有词条", () => assert.match(errZh("rate_limited"), /试得太快/));
ok("instance_not_found 有词条", () => assert.match(errZh("instance_not_found"), /实例不在/));
ok("capability_not_published 有词条", () => assert.match(errZh("capability_not_published"), /目录里/));

// —— cannot_ 前缀退化（§4.1 的写法：带状态后缀退到前缀）——
ok("cannot_memory_from_released 退到 cannot_memory", () =>
  assert.match(errZh("cannot_memory_from_released"), /只有运行中的实例/));
ok("cannot_purge_from_running 退到既有词条", () =>
  assert.match(errZh("cannot_purge_from_running"), /只有已释放/));
ok("未知 cannot_ 前缀原样带出（好过 [object Object]）", () =>
  assert.equal(errZh("cannot_fly_from_mars"), "cannot_fly_from_mars"));
ok("非 cannot_ 的未知码原样带出", () => assert.equal(errZh("totally_new"), "totally_new"));

// —— panelSession 引导会话（MEMORY-PANEL-EMBED.md ③-B）——
ok("引导会话形状：空 userKey 让 Panel 不自发业务 key", () => {
  const s = buildPanelSession();
  assert.equal(s.authMethod, "idp");
  assert.equal(s.userKey, "");            // 空串 = 身份由代理服务端注入
  assert.equal(s.instanceId, "default");
  assert.equal(typeof s.user.user_id, "string");
});

ok("writePanelSession 写对 key 与 JSON 形状", () => {
  const store = new Map<string, string>();
  const fake = { setItem: (k: string, v: string) => { store.set(k, v); } };
  writePanelSession(fake);
  assert.ok(store.has(PANEL_SESSION_KEY));
  const written = JSON.parse(store.get(PANEL_SESSION_KEY)!);
  assert.deepEqual(written, buildPanelSession());
});

ok("writePanelSession 写不进也不抛（隐私模式兜底）", () => {
  const boom = { setItem() { throw new Error("quota"); } };
  assert.doesNotThrow(() => writePanelSession(boom));
});

// —— storage 事件回落判定（只认这条 key 被清空，绝不误登出）——
ok("同 key 被清空 → 判定凭据失效", () =>
  assert.equal(isPanelSessionCleared({ key: PANEL_SESSION_KEY, newValue: null }), true));
ok("同 key 但被改写 → 不算失效", () =>
  assert.equal(isPanelSessionCleared({ key: PANEL_SESSION_KEY, newValue: "{}" }), false));
ok("别的 key 清空 → 与记忆无关，不降级", () =>
  assert.equal(isPanelSessionCleared({ key: "dshc.something", newValue: null }), false));
ok("key 为 null（clear 全站）→ 不误判成记忆失效", () =>
  assert.equal(isPanelSessionCleared({ key: null, newValue: null }), false));

console.log(`\n${pass} 项全过。`);
