// dsh-control 假接口：按 ARCHITECTURE §4 契约返回固定数据；数据全为文档占位地址。
// node mock/server.mjs → 13300（key: dshc-demo）；MOCK=empty → 空台账；MOCK_NO_SSE=1 → 演练轮询回落
import http from "node:http";
const iso = (s = 0) => new Date(Date.now() + s * 1000).toISOString();
let SEQ = 10, tok = {}, jobs = {};
const hosts = [{ id: 1, name: "nas-1", enabled: true, data_root: "/mnt/hdd/dsh-family", note: "机械盘，剩 1.7 T" }, { id: 2, name: "host-2", enabled: true, data_root: "/home/u/dsh-data", note: "主控台自己这台" }];
let insts = process.env.MOCK === "empty" ? [] : [
  { id: 1, slug: "dsh-probe", owner_label: null, host_id: 2, state: "released", dsh_version: "1.14.2", config_version: "v1", dns_name: "dsh-probe.example.com", released_at: iso(-86400 * 6), updated_at: iso(-86400 * 6), err: null },{ id: 2, slug: "dsh-01", owner_label: "老婆", host_id: 1, state: "running", dsh_version: "1.14.2", config_version: "v1", dns_name: "dsh-01.example.com", released_at: null, updated_at: iso(-3600), err: null },{ id: 3, slug: "dsh-02", owner_label: null, host_id: 1, state: "unhealthy", dsh_version: "1.13.8", config_version: "v1", dns_name: "dsh-02.example.com", released_at: null, updated_at: iso(-900), err: "健康检查连续 3 次失败：站点返回 502；距上次自动重启不足 10 分钟，先不重启" }];
const CFG = [{ key: "documentsDirectory", value: "/data", label: "工作区根目录", help: "DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配" }, { key: "trustedHosts", value: "", label: "准入域名", help: "Origin 和 Host 不完全相等就拒绝——「能打开但一直重连」都是它的锅" },
{ key: "TZ", value: "Asia/Shanghai", label: "时区", help: "定时任务和备份都按北京时间走" }, { key: "backupWindow", value: "03:20-04:00", label: "备份窗口", help: "窗口内不自动重启，免得把正在跑的备份腰斩" }];
const STEPS = { deploy: [[10, "域名规划"], [20, "主机预检"], [30, "添加解析"], [40, "开隧道"], [50, "建反代站点"], [60, "部署容器"], [70, "抓登录密钥"], [80, "公网探活"], [90, "验收复核"]],
restart: [[10, "重启容器"], [20, "等待健康"], [30, "抓登录密钥"], [40, "公网探活"]], release: [[10, "核对确认词"], [20, "注销 nginx 站点"], [30, "注销 frp 隧道"], [40, "注销解析记录"], [50, "停止容器（只停不删）"], [60, "标记已释放"]] };
const mkJob = (k, i, ms) => (jobs[++SEQ] = { id: SEQ, kind: k, inst: i, t0: Date.now(), stepMs: ms, steps: STEPS[k], done: false }, SEQ);
function jobView(id) { const j = jobs[id]; if (!j) return null; const st = j.steps.map((_, i) => { const e = Date.now() - j.t0; return e >= (i + 1) * j.stepMs ? "succeeded" : e >= i * j.stepMs ? "running" : "pending"; });
  if (!j.done && st.every((s) => s === "succeeded")) { j.done = true; const I = insts.find((x) => x.id === j.inst); if (I) { I.updated_at = iso(); if (j.kind === "deploy") I.state = "running"; if (j.kind === "restart") tok[I.id] = Math.floor(Math.random() * 1e6); if (j.kind === "release") { I.state = "released"; I.released_at = iso(); } } }
  return { kind: j.kind, state: j.done ? "succeeded" : "running", instance_id: j.inst, steps: j.steps.map(([s, t], i) => ({ seq: s, key: "k" + s, title: t, state: st[i], detail: st[i] === "succeeded" ? `通过 · 用时 ${(0.6 + i * 0.7).toFixed(1)} s` : st[i] === "running" ? "执行中…" : null })) }; }
function logsFor(id, after) { const j = jobs[id]; if (!j) return { items: [], next: after }; const out = [];
  j.steps.forEach((s, si) => { for (let k = 0; k < 3; k++) { const lid = si * 3 + k + 1, at = j.t0 + si * j.stepMs + k * 700; if (lid <= after || at > Date.now()) continue;
    out.push({ id: lid, seq: s[0], level: "info", line: k === 0 ? `$ ssh ${hosts[si % 2].name} … # ${s[1]}` : k === 1 ? `← ${s[1]}：done (exit 0)` : "校验回读通过", ts: new Date(at).toISOString() }); } });
  return { items: out, next: Math.max(after, out.length ? out[out.length - 1].id : after) }; }
const tokOf = (i) => (tok[i] ??= 100000 + i * 777), detail = (I) => ({ id: I.id, slug: I.slug, owner_label: I.owner_label, host: { id: I.host_id, name: hosts.find((h) => h.id === I.host_id).name },
state: I.state, desired_state: I.state === "released" ? "released" : "running", dsh_version: I.dsh_version, image_tag: "dsh-family:local", image_id: "a1b2c3d4", config_version: I.config_version, config: { items: CFG.map((c) => ({ ...c, value: c.key === "trustedHosts" ? I.dns_name : c.value })) }, network: { dns_name: I.dns_name, public_url: "https://" + I.dns_name, frp_remote_port: 4892 + I.id, frp_proxy_id: 700 + I.id, dns_record_id: "9000000" + I.id, container_ip: I.state === "released" ? null : "172.30.0." + I.id }, runtime: I.state === "released" ? null : { docker_state: "running", health: I.state === "unhealthy" ? "unhealthy" : "healthy", restart_count: I.state === "unhealthy" ? 2 : 0, cpu_pct: 0.4, mem_usage_bytes: 2.8e8, mem_limit_bytes: 4.29e9, captured_at: iso(-60) }, access: { has_token: I.state !== "released", token_masked: "to" + tokOf(I.id) + "··9" + I.id, url: I.state === "released" ? null : `https://${I.dns_name}/?token=········`, captured_at: iso(-7200) }, last_error: I.err, released_at: I.released_at, actions: { restart: I.state !== "released", release: I.state !== "released" } });
const evts = (I) => [{ id: 3, kind: "token_captured", message: "抓到最新登录密钥并入库", created_at: I.updated_at }, { id: 2, kind: "auto_restart", message: I.state === "unhealthy" ? "健康检查失败，等待自动重启冷却窗口" : "自动重启一次，恢复健康", created_at: iso(-86400) },
{ id: 1, kind: I.state === "released" ? "released" : "deployed", message: I.state === "released" ? "已释放：解析/隧道/站点注销，容器停止（数据保留）" : "开通完成，验收通过", created_at: iso(-86400 * 8) }];
http.createServer(async (req, res) => {
  const u = new URL(req.url, "http://x"), p = u.pathname; let raw = ""; for await (const c of req) raw += c;
  const b = raw ? JSON.parse(raw) : {}, send = (c, o, h = {}) => { res.writeHead(c, { "Content-Type": "application/json", ...h }); res.end(JSON.stringify(o)); };
  const m = (re) => p.match(re), iid = () => Number(m(/^\/api\/instances\/(\d+)/)?.[1]), auth = req.headers.cookie?.includes("dshc_session");
  if (p === "/api/healthz") return send(200, { ok: true });
  if (p === "/api/login") { if (!b.key) return send(400, { detail: "密钥没粘贴进来" }); if (String(b.key).trim() !== "dshc-demo") return send(401, { detail: "运维密钥不对" });
    return send(200, { ok: true, expires_at: iso(7 * 86400) }, { "Set-Cookie": "dshc_session=1; HttpOnly; SameSite=Lax; Path=/; Max-Age=604800" }); }
  if (!auth) return send(401, { detail: "没登录或登录已过期" });
  if (p === "/api/session") return send(200, { ok: true, expires_at: iso(7 * 86400) });
  if (p === "/api/logout") return send(200, { ok: true });
  if (p === "/api/hosts") return send(200, hosts);
  if (p === "/api/overview") return send(200, { running: insts.filter((i) => i.state === "running").length, unhealthy: insts.filter((i) => ["unhealthy", "restarting", "error"].includes(i.state)).length, released: insts.filter((i) => i.state === "released").length, hosts: hosts.length, by_host: hosts.map((h) => ({ host: h.name, note: h.note, total: insts.filter((i) => i.host_id === h.id).length, free_gb: h.id === 1 ? 1740 : 42 })) });
if (p === "/api/instances" && req.method === "GET") { const q = (u.searchParams.get("q") ?? "").toLowerCase(); return send(200, insts.filter((i) => !q || [i.slug, i.owner_label, i.dns_name].some((v) => v?.toLowerCase().includes(q))).map((i) => ({ ...i, host: hosts.find((h) => h.id === i.host_id), err: undefined }))); }
if (p === "/api/instances" && req.method === "POST") { const nid = Math.max(0, ...insts.map((i) => i.id)) + 1, slug = "dsh-" + String(Math.max(...insts.map((i) => Number(/^dsh-(\d+)$/.exec(i.slug)?.[1] ?? 0)), 0) + 1).padStart(2, "0");
insts.push({ id: nid, slug, owner_label: null, host_id: b.host_id ?? 2, state: "deploying", dsh_version: null, config_version: "v1", dns_name: slug + ".example.com", released_at: null, updated_at: iso(), err: null });
return send(200, { instance_id: nid, dns_name: slug + ".example.com", job_id: mkJob("deploy", nid, 2600) }); }
  const I = m(/^\/api\/instances\/(\d+)/) ? insts.find((i) => i.id === iid()) : null;
  if (m(/^\/api\/instances\/\d+$/) && req.method === "PATCH") { if (!I) return send(404, { detail: "实例不存在" }); I.owner_label = b.owner_label ?? ""; return send(200, detail(I)); }
  if (m(/^\/api\/instances\/\d+\/token$/)) { if (!I || I.state === "released") return send(409, { detail: "该实例没有可用的容器" }); const t = tok[I.id] = "tok" + Math.floor(Math.random() * 1e6) + "x9"; return send(200, { token: t, url: `https://${I.dns_name}/?token=${t}`, captured_at: iso() }); }
  if (m(/^\/api\/instances\/\d+\/events$/)) return I ? send(200, evts(I)) : send(404, { detail: "实例不存在" });
  if (m(/^\/api\/instances\/\d+\/restart$/)) { if (!I || I.state === "released") return send(409, { detail: "当前状态不可重启" }); I.state = "restarting"; return send(200, { job_id: mkJob("restart", I.id, 1600) }); }
  if (m(/^\/api\/instances\/\d+\/release$/)) { if (!I) return send(404, { detail: "实例不存在" }); if (b.confirm !== I.slug) return send(422, { detail: "确认词与实例名不一致，拒绝释放" }); return send(200, { job_id: mkJob("release", I.id, 1300) }); }
  if (m(/^\/api\/instances\/\d+$/)) return I ? send(200, detail(I)) : send(404, { detail: "实例不存在" });
  const jid = Number(m(/^\/api\/jobs\/(\d+)/)?.[1]);
  if (m(/^\/api\/jobs\/\d+\/logs$/)) return send(200, logsFor(jid, Number(u.searchParams.get("after") ?? 0)));
  if (m(/^\/api\/jobs\/\d+\/stream$/)) { if (process.env.MOCK_NO_SSE) return send(404, { detail: "SSE 已按请求关闭（演练轮询回落）" });
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    let cur = 0, t = 0; const push = () => { const v = jobView(jid); if (!v) return clearInterval(t); res.write(`event: step\ndata: ${JSON.stringify(v)}\n\n`);
      const l = logsFor(jid, cur); cur = l.next; l.items.forEach((i) => res.write(`event: log\ndata: ${JSON.stringify(i)}\n\n`));
      if (v.state !== "running") { res.write(`event: done\ndata: ${JSON.stringify({ state: v.state })}\n\n`); clearInterval(t); res.end(); } };
    push(); t = setInterval(push, 1200); req.on("close", () => clearInterval(t)); return; }
  if (m(/^\/api\/jobs\/\d+$/)) { const v = jobView(jid); return v ? send(200, v) : send(404, { detail: "工单不存在" }); }
  send(404, { detail: "接口不存在：" + p });
}).listen(13300, "127.0.0.1", () => console.log("dsh-control mock → http://127.0.0.1:13300 (key: dshc-demo)"));
