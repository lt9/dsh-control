// dsh-control 假接口：按 ARCHITECTURE §4 契约返回固定数据；数据全为文档占位地址。
// node mock/server.mjs → 13300（key: dshc-demo）；MOCK=empty → 空台账；MOCK_NO_SSE=1 → 演练轮询回落
import http from "node:http";
const iso = (s = 0) => new Date(Date.now() + s * 1000).toISOString();
// 保留期（§6.4 DSHC_PURGE_AFTER_DAYS，默认 15 天）：released 满期后 watcher 自动 purge。
const PURGE_DAYS = Number(process.env.MOCK_PURGE_DAYS ?? 15);
// purge_after = released_at + 保留期（§4：只在 state=released 时给，UTC ISO）
const purgeAfterOf = (i) => (i.state === "released" && i.released_at
  ? new Date(new Date(i.released_at).getTime() + PURGE_DAYS * 86400 * 1000).toISOString() : null);
let SEQ = 10, tok = {}, jobs = {};
// 站点设置（§6.1.5）：base_url 存空串=吃站点的公网域；GET 出去的是解析后的生效值（§4）
const SITE_BASE = "https://console.example.com";
let settings = { image_ship: { mode: "ssh", base_url: "", mirror_order: ["gitee", "github", "console"], auto_mirror: true } };
const baseOf = () => settings.image_ship.base_url || SITE_BASE;
// 外部渠道（§6.1.6）：token 只写不回显——这里连值都不留，只留"配过"这个事实。
// 假数据故意一条 ok 一条 failed，好让界面上三种状态一次看全（未推送看 gitee 之外的新 tag）。
let channels = {
  github: { repo: "lt9/dsh-control", token_set: !process.env.MOCK_NO_TOKENS, latest_state: "ok",
    url: "https://github.example.invalid/lt9/dsh-control/releases/download/image-dsh-family_1_14_2/dsh-family_1.14.2.tar.gz",
    error: null, updated_at: iso(-86400 * 9) },
  gitee: { repo: "lt9/dsh-control", token_set: !process.env.MOCK_NO_TOKENS, latest_state: "failed", url: null,
    error: "第 9 卷传到一半被掐：gitee 回 403（附件总量要超 1G，旧版 release 没删干净）。",
    updated_at: iso(-3600 * 5) } };
// MOCK_NO_TOKENS=1 → 演练"没配令牌"那一档：没令牌就没人推过，状态一律清零
const stateOf = (ch) => (channels[ch].token_set ? channels[ch].latest_state : null);
// 状态键两个名字都发：后端 _settings_payload 现在给的是 state，任务书里叫 latest_state，
// 前端两个都认——这里两个都给，两边落地哪个都不会把界面读空。
const chanView = (ch) => ({ repo: channels[ch].repo, token_set: channels[ch].token_set,
  state: stateOf(ch), latest_state: stateOf(ch),
  url: stateOf(ch) === "ok" ? channels[ch].url : null,
  error: stateOf(ch) === "failed" ? channels[ch].error : null, updated_at: channels[ch].updated_at });
const settingsView = () => ({ image_ship: { ...settings.image_ship, base_url: baseOf() },
  mirrors: { github: chanView("github"), gitee: chanView("gitee") } });
// 制品库（§6.1.5）：一行一个 tag，行存在即文件已完整落盘。MOCK_NO_IMAGES=1 → 演练空态
const STORE_SIZE = 480e9;
const artTok = () => "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_".split("").sort(() => Math.random() - .5).slice(0, 26).join("");
const shaOf = (t) => { let h = 0x811c9dc5; for (let i = 0; i < t.length; i++) h = Math.imul(h ^ t.charCodeAt(i), 16777619) >>> 0; let out = ""; for (let i = 0; i < 16; i++) { h ^= h << 13; h >>>= 0; h ^= h >>> 17; h ^= h << 5; h >>>= 0; h = Math.imul(h, 16777619) >>> 0; out += h.toString(16).padStart(8, "0"); } return out.slice(0, 64); };
const artFile = (tag) => tag.replace(/[^\w.-]+/g, "_") + ".tar.gz";
let artSeq = 2;
let arts = process.env.MOCK_NO_IMAGES ? [] : [
  { id: 1, tag: "dsh-family:1.14.2", size_bytes: 4.32e9, sha256: shaOf("dsh-family:1.14.2"), token: "Kq7Zx2mVb4nT8sL1pR6yW3cDXQ", origin: "nas-1", created_at: iso(-86400 * 9) },
  { id: 2, tag: "dsh-family:1.13.8", size_bytes: 4.11e9, sha256: shaOf("dsh-family:1.13.8"), token: "Zp4Qw8eRt2yu6io0pas9dfgHjK", origin: "nas-1", created_at: iso(-86400 * 38) }];
const artView = (a) => ({ id: a.id, tag: a.tag, size_bytes: a.size_bytes, sha256: a.sha256, token: a.token,
  url: baseOf() + "/dl/" + a.token + "/" + artFile(a.tag), origin_host: a.origin, created_at: a.created_at });
const storeFree = () => STORE_SIZE - arts.reduce((n, a) => n + (a.size_bytes || 0), 0);
const hosts = [{ id: 1, name: "nas-1", enabled: true, data_root: "/mnt/hdd/dsh-family", note: "机械盘，剩 1.7 T" }, { id: 2, name: "host-2", enabled: true, data_root: "/home/u/dsh-data", note: "主控台自己这台" }];
let insts = process.env.MOCK === "empty" ? [] : [
  { id: 1, slug: "dsh-probe", owner_label: null, host_id: 2, state: "released", dsh_version: "1.14.2", config_version: "v2", dns_name: "dsh-probe.example.com", released_at: iso(-86400 * 6), updated_at: iso(-86400 * 6), err: null },{ id: 2, slug: "dsh-01", owner_label: "家人", host_id: 1, state: "running", dsh_version: "1.14.2", config_version: "v2", dns_name: "dsh-01.example.com", released_at: null, updated_at: iso(-3600), err: null },{ id: 3, slug: "dsh-02", owner_label: null, host_id: 1, state: "unhealthy", dsh_version: "1.13.8", config_version: "v2", dns_name: "dsh-02.example.com", released_at: null, updated_at: iso(-900), err: "健康检查连续 3 次失败：站点返回 502；距上次自动重启不足 10 分钟，先不重启" },
  // purged 终态示例（§3/§6.4）：释放 41 天 → 满 15 天保留期 → 26 天前自动彻底释放。
  { id: 4, slug: "dsh-old", owner_label: "试验田", host_id: 2, state: "purged", dsh_version: "1.13.1", config_version: "v2", dns_name: "dsh-old.example.com", released_at: iso(-86400 * 41), purged_at: iso(-86400 * 26), updated_at: iso(-86400 * 26), tag: "dsh-family:1.13", err: null }];
const CFG = [{ key: "image", value: "dsh-family:local", label: "镜像", help: "镜像是事先构建好传到那台机器上的，控制台不构建；换版本是以后的能力，现在固定用站点配置的默认版本" }, { key: "documentsDirectory", value: "/data", label: "工作区根目录", help: "DSH 用它拼默认工作区，容器里没有 xdg-user-dir，必须显式配" }, { key: "trustedHosts", value: "", label: "准入域名", help: "Origin 和 Host 不完全相等就拒绝——「能打开但一直重连」都是它的锅" },
{ key: "TZ", value: "Asia/Shanghai", label: "时区", help: "定时任务和备份都按北京时间走" }, { key: "backupWindow", value: "03:20-04:00", label: "备份窗口", help: "窗口内不自动重启，免得把正在跑的备份腰斩" }];
// DSH 容器内**实际生效**的配置（详情页「DSH 配置」卡）；值全是文档占位
const grp = (t, ...i) => ({ title: t, items: i.map(([key, value, note]) => ({ key, value, note: note || "" })) });
const DSHC = (I) => ({
  captured_at: iso(-3600), dsh_version: I.dsh_version || "1.14.2",
  model: "qwen-example-27b", base_url: "http://192.0.2.20:8000/v1",
  agent_team: true, office_skills: true, skills: ["comfy-image"],
  bundles: ["dsh-base", "dsh-web-app"],
  groups: [
    grp("模型", ["默认模型", "qwen-example-27b", "会话默认用的那一个；下拉里可换"],
        ["提供方", "local", "地址 http://192.0.2.20:8000/v1"],
        ["上下文窗口", "262144", "超过就触发压缩"], ["单次回复上限", "32768"]),
    grp("上下文压缩", ["压缩触发比例", "0.8", "用到窗口的这个比例就自动压缩"],
        ["压缩预算", "8192", "每次压缩产出多少 token 的摘要"]),
    grp("生图", ["DSH 原生入口", "没有", "DSH 没有原生生图入口，生图由技能调 ComfyUI 接口完成"],
        ["技能是否就位", "已就位", "技能放在实例数据卷的 skills/ 下，DSH 会自动发现"]),
    grp("插件与智能体团队", ["智能体团队", "已开启", "团队成员、共享任务板、团队工具都在"],
        ["团队成员上限", "4", "开启后由它管并发，不再受子代理上限约束"],
        ["插件安装入口", "已关闭", "防止误装带云端调用的第三方插件"]),
    grp("技能（Skills）", ["办公技能（Word/PPT/Excel）", "已开启", "官方 office 技能 + LibreOffice Kit"],
        ["已发现技能", "comfy-image", "来自实例数据卷 skills/ 目录，改完不用重启"]),
    grp("预设与并发", ["会话预设", "standard", "standard / ptc / minimal / cordis 四选一"],
        ["工作流并发", "2", ""]),
    grp("准入与权限", ["公网地址", "https://" + I.dns_name, "一键登录地址就是拼在它后面"],
        ["文件权限档", "danger-full-access", "她让改文件时不再逐次弹窗"],
        ["读取时间", iso(-3600).slice(0, 19).replace("T", " ") + " UTC", "点「重新读取」按最新值刷新"]),
  ],
});
const STEPS = { deploy: [[10, "域名规划"], [20, "主机预检"], [30, "添加解析"], [40, "开隧道"], [50, "建反代站点"], [60, "部署容器"], [66, "铺生图技能与智能体团队"], [70, "抓登录密钥"], [80, "公网探活"], [90, "验收复核"]],
restart: [[5, "铺生图技能与智能体团队"], [10, "重启容器"], [20, "等待健康"], [30, "抓登录密钥"], [40, "公网探活"]], release: [[10, "核对确认词"], [20, "注销 nginx 站点"], [30, "注销 frp 隧道"], [40, "注销解析记录"], [50, "停止容器（只停不删）"], [60, "标记已释放"]],
publish: [[10, "源机 docker save｜gzip 流进制品库"], [20, "算 sha256 并登记（同标签原子替换）"]],
 purge: [[10, "核对确认词"], [20, "补删外部对象（解析/隧道/站点）"], [30, "删除容器"], [40, "删除数据目录（含归档与历史）"], [50, "标记已彻底释放"]] };
// 下一台叫什么：dsh- + 现有最大序号 +1，两位零填充（口径照后端 domain/slug.py）。
const nextSlug = () => "dsh-" + String(Math.max(0, ...insts.map((i) => Number(/^dsh-(\d+)$/.exec(i.slug)?.[1] ?? 0))) + 1).padStart(2, "0");
const mkJob = (k, i, ms, purpose) => (jobs[++SEQ] = { id: SEQ, kind: k, inst: i, t0: Date.now(), stepMs: ms, steps: STEPS[k], done: false, purpose }, SEQ);
// 发布工单：不带实例（制品是站点级的），进度照样走 SSE/轮询
const mkPublish = (tag, origin) => (jobs[++SEQ] = { id: SEQ, kind: "publish", inst: null, t0: Date.now(), stepMs: 2600, steps: STEPS.publish, done: false, publish: { tag, origin, size: Math.round(3.8e9 + ((tag.length * 73) % 9) * 1e8), sha: shaOf(tag + (SEQ + 1)) } }, SEQ);
function jobView(id) { const j = jobs[id]; if (!j) return null; const st = j.steps.map((_, i) => { const e = Date.now() - j.t0; return e >= (i + 1) * j.stepMs ? "succeeded" : e >= i * j.stepMs ? "running" : "pending"; });
  if (!j.done && st.every((s) => s === "succeeded")) { j.done = true; const I = insts.find((x) => x.id === j.inst); if (I) { I.updated_at = iso(); if (j.kind === "deploy") { I.state = "running"; I.released_at = null; } if (j.kind === "restart") tok[I.id] = Math.floor(Math.random() * 1e6); if (j.kind === "release") { I.state = "released"; I.released_at = iso(); } if (j.kind === "purge") { I.state = "purged"; I.purged_at = iso(); I.freed = true; } }
    // 发布工单不带实例（制品是站点级的），所以这段必须在 if (I) 外面：
    // 20 record 过了才插行；同标签是原子替换——新行落库成功后才把旧行丢掉。
    if (j.kind === "publish" && j.publish) { const { tag, origin, size, sha } = j.publish; arts = [{ id: ++artSeq, tag, size_bytes: size, sha256: sha, token: artTok(), origin, created_at: iso() }, ...arts.filter((x) => x.tag !== tag)];
      // auto_mirror 开着且配了令牌才推（§6.1.6：mirror_* 步未配就跳过）
      if (settings.image_ship.auto_mirror && channels.github.token_set && channels.github.repo) {
        channels.github.latest_state = "ok"; channels.github.error = null; channels.github.updated_at = iso();
        channels.github.url = "https://github.example.invalid/" + channels.github.repo + "/releases/download/image-" + artFile(tag).replace(/\.tar\.gz$/, "").replace(/[^\w.-]+/g, "_") + "/" + artFile(tag); } } }
  return { kind: j.kind, state: j.done ? "succeeded" : "running", instance_id: j.inst, purpose: j.kind === "deploy" ? j.purpose ?? null : null, steps: j.steps.map(([s, t], i) => ({ seq: s, key: "k" + s, title: t, state: st[i], detail: st[i] === "succeeded" ? `通过 · 用时 ${(0.6 + i * 0.7).toFixed(1)} s` : st[i] === "running" ? "执行中…" : null })) }; }
function logsFor(id, after) { const j = jobs[id]; if (!j) return { items: [], next: after }; const out = [];
  if (j.kind === "publish" && j.publish) { const { tag, origin, size, sha } = j.publish, f = artFile(tag), gb = (n) => (n / 1e9).toFixed(2) + " GB";
    const say = [[`$ ssh ${origin} docker save ${tag} | gzip -n > 制品库/${f}.tmp`, `  已落 ${gb(size * 0.44)} / ${gb(size)} · 面板内存管道，两台都不落盘`, `  收流完成 ${gb(size)}，退出码 0`],
      [`$ sha256sum ${f}.tmp`, `  ${sha}  ${f}.tmp`, `改名去 .tmp 并登记一行（同标签的旧行等新行落库后才作废）`]];
    const its = [];
    say.forEach((ls, si) => ls.forEach((line, k) => { const lid = si * 3 + k + 1, at = j.t0 + si * j.stepMs + k * 700;
      if (lid <= after || at > Date.now()) return; its.push({ id: lid, seq: j.steps[si][0], level: "info", line, ts: new Date(at).toISOString() }); }));
    return { items: its, next: Math.max(after, its.length ? its[its.length - 1].id : after) }; }
  j.steps.forEach((s, si) => { for (let k = 0; k < 3; k++) { const lid = si * 3 + k + 1, at = j.t0 + si * j.stepMs + k * 700; if (lid <= after || at > Date.now()) continue;
    out.push({ id: lid, seq: s[0], level: "info", line: k === 0 ? `$ ssh ${hosts[si % 2].name} … # ${s[1]}` : k === 1 ? `← ${s[1]}：done (exit 0)` : "校验回读通过", ts: new Date(at).toISOString() }); } });
  return { items: out, next: Math.max(after, out.length ? out[out.length - 1].id : after) }; }
const tokOf = (i) => (tok[i] ??= 100000 + i * 777);
// 容器已经不在的两个态：released 停着（数据留着，可恢复）、purged 已删（终态，只读档案）
const gone = (i) => i.state === "released" || i.state === "purged";
// actions 由服务端算：口径照后端 serialize._actions（restart 只给在跑的几态、
// release 含 deploying、deploy 只给从未开通成功的 pending/error、restore/purge=released、
// upgrade=running/unhealthy）。purged 全 false（只读）。
const RESTARTABLE = new Set(["running", "unhealthy", "restarting", "error"]);
const RELEASABLE = new Set(["deploying", "running", "unhealthy", "restarting", "error"]);
const actionsOf = (I) => ({ restart: RESTARTABLE.has(I.state), release: RELEASABLE.has(I.state), deploy: I.state === "pending" || I.state === "error", restore: I.state === "released", purge: I.state === "released", upgrade: I.state === "running" || I.state === "unhealthy" });

const detail = (I) => ({ id: I.id, slug: I.slug, owner_label: I.owner_label, host: { id: I.host_id, name: hosts.find((h) => h.id === I.host_id).name },
state: I.state, desired_state: gone(I) ? "released" : "running", dsh_version: I.dsh_version, image_tag: I.tag || "dsh-family:local", image_id: "a1b2c3d4", config_version: I.config_version, dsh_config: DSHC(I), config: { items: CFG.map((c) => ({ ...c, value: c.key === "trustedHosts" ? I.dns_name : c.value })) },
// purged 之后外部 id 字段清干净（§6.4 mark_purged），档案里只留域名这个名字
network: { dns_name: I.dns_name, public_url: gone(I) ? null : "https://" + I.dns_name, frp_remote_port: gone(I) ? null : 20000 + I.id, frp_proxy_id: gone(I) ? null : 700 + I.id, dns_record_id: gone(I) ? null : "9000000" + I.id, container_ip: gone(I) ? null : "172.30.0." + I.id },
runtime: gone(I) ? null : { docker_state: "running", health: I.state === "unhealthy" ? "unhealthy" : "healthy", restart_count: I.state === "unhealthy" ? 2 : 0, cpu_pct: 0.4, mem_usage_bytes: 2.8e8, mem_limit_bytes: 4.29e9, captured_at: iso(-60) },
// 密钥与含密钥的地址只在 reveal 里出现（§4）；这里只给打码与时间，purged 连这份都没了
access: { has_token: !gone(I), token_masked: gone(I) ? null : "to" + tokOf(I.id) + "··9" + I.id, url: gone(I) ? null : `https://${I.dns_name}/?token=········`, captured_at: gone(I) ? null : iso(-7200) },
last_error: I.err, released_at: I.released_at, purged_at: I.purged_at || null, purge_after: purgeAfterOf(I),
actions: actionsOf(I) });
// 事件时间线：purged 的历史照常给（§6.4：工单与事件保留，只是全部只读）
const evts = (I) => [{ id: 3, kind: "token_captured", message: "抓到最新登录密钥并入库", created_at: I.updated_at }, { id: 2, kind: "auto_restart", message: I.state === "unhealthy" ? "健康检查失败，等待自动重启冷却窗口" : "自动重启一次，恢复健康", created_at: iso(-86400) },
{ id: 1, kind: I.state === "purged" ? "instance_purged" : I.state === "released" ? "released" : "deployed", message: I.state === "purged" ? "已彻底释放：容器与数据目录（含备份归档）已删除，外部对象补删干净" : I.state === "released" ? "已释放：解析/隧道/站点注销，容器停止（数据保留，保留期内可恢复）" : "开通完成，验收通过", created_at: I.state === "purged" ? (I.purged_at || iso(-86400 * 26)) : iso(-86400 * 8) }];
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
  if (p === "/api/overview") return send(200, { dns_zone: "example.com", running: insts.filter((i) => i.state === "running").length, unhealthy: insts.filter((i) => ["unhealthy", "restarting", "error"].includes(i.state)).length, released: insts.filter((i) => i.state === "released").length, hosts: hosts.length, by_host: hosts.map((h) => ({ host: h.name, note: h.note, total: insts.filter((i) => i.host_id === h.id).length, free_gb: h.id === 1 ? 1740 : 42 })) });
  // —— 设置页（§4/§6.1.5）——
  if (p === "/api/settings" && req.method === "GET") return send(200, settingsView());
  if (p === "/api/settings" && req.method === "PUT") { const s2 = b.image_ship || {};
    if (s2.mode !== undefined && s2.mode !== "ssh" && s2.mode !== "download") return send(422, { detail: { ok: false, error: "invalid_mode", message: "分发方式只有 ssh 与 download 两种" } });
    if (s2.mirror_order !== undefined) {
      const want = ["gitee", "github", "console"];
      // 后端口径（routes.py）：去重的非空子集就行，不要求三个都在
      const bad = !Array.isArray(s2.mirror_order) || !s2.mirror_order.length
        || new Set(s2.mirror_order).size !== s2.mirror_order.length
        || s2.mirror_order.some((c) => !want.includes(c));
      if (bad) return send(422, { detail: { ok: false, error: "invalid_mirror_order", message: "探测优先序得是 gitee、github、console 里挑几个、且不重复的非空排列" } });
      settings.image_ship.mirror_order = s2.mirror_order; }
    if (s2.auto_mirror !== undefined) {
      if (typeof s2.auto_mirror !== "boolean") return send(422, { detail: { ok: false, error: "invalid_auto_mirror", message: "auto_mirror 只有真与假" } });
      settings.image_ship.auto_mirror = s2.auto_mirror; }
    // 仓库名：顶层 mirrors.{ch}.repo 与 image_ship.mirrors.{ch}.repo 两种摆法都收（§4 那行读得出两种）
    const repos = { ...(b.mirrors || {}), ...(s2.mirrors || {}) };
    for (const ch of Object.keys(repos)) {
      if (ch !== "github" && ch !== "gitee") return send(422, { detail: { ok: false, error: "invalid_channel", message: "外部渠道只有 github 与 gitee 两个" } });
      const r = String(repos[ch]?.repo ?? "").trim();
      // 空串=不推这个渠道；非空必须是 owner/repo
      if (r && !/^[\w.-]+\/[\w.-]+$/.test(r)) return send(422, { detail: { ok: false, error: "invalid_repo", message: `${ch} 的仓库名要写成 owner/repo，例如 lt9/dsh-control` } });
      channels[ch].repo = r; }
    if (s2.mode !== undefined) settings.image_ship.mode = s2.mode;
    if (s2.base_url !== undefined) settings.image_ship.base_url = String(s2.base_url).trim();
    return send(200, settingsView()); }   // PUT 也回全量生效值，前端拿回执直接渲染
  // 令牌即焚（§6.1.6）：只写不回显，响应里除了 token_set 什么都没有
  const mt = m(/^\/api\/mirror-tokens\/(\w+)$/);
  if (mt && req.method === "PUT") { const ch = mt[1];
    if (ch !== "github" && ch !== "gitee") return send(422, { detail: { ok: false, error: "invalid_channel", message: "外部渠道只有 github 与 gitee 两个" } });
    const t = String(b.token ?? "");
    if (!t.trim()) return send(422, { detail: { ok: false, error: "invalid_token", message: "令牌是空的——要配就粘一个访问令牌进来" } });
    channels[ch].token_set = true;   // 值不留， mock 也不练存秘密
    return send(200, { ok: true, token_set: true }); }
  if (p === "/api/images" && req.method === "GET") return send(200, { items: arts.map(artView), store_free_bytes: storeFree() });
  if (p === "/api/images" && req.method === "POST") { const tag = String(b.tag ?? "").trim(), H = hosts.find((h) => h.id === Number(b.from_host_id));
    if (!tag) return send(422, { detail: { ok: false, error: "tag_required", message: "要先写上镜像标签，比如 dsh-family:1.14.2" } });
    if (!H) return send(404, { detail: { ok: false, error: "host_not_found", message: "源主机不存在，刷新页面重选一台" } });
    return send(200, { job_id: mkPublish(tag, H.name) }); }
  const ai = m(/^\/api\/images\/(\d+)$/);
  if (ai && req.method === "DELETE") { const at = arts.findIndex((a) => a.id === Number(ai[1])); if (at < 0) return send(404, { detail: { ok: false, error: "not_found", message: "这条制品已经不在了" } });
    arts.splice(at, 1); return send(200, { ok: true }); }   // 删行删文件：链接即刻失效
if (p === "/api/instances/next-slug") { const s2 = nextSlug(); return send(200, { slug: s2, dns_name: s2 + ".example.com" }); }
if (p === "/api/instances" && req.method === "GET") { const q = (u.searchParams.get("q") ?? "").toLowerCase(); return send(200, insts.filter((i) => !q || [i.slug, i.owner_label, i.dns_name].some((v) => v?.toLowerCase().includes(q))).map((i) => ({ ...i, host: hosts.find((h) => h.id === i.host_id), purged_at: i.purged_at ?? null, purge_after: purgeAfterOf(i), err: undefined }))); }
if (p === "/api/instances" && req.method === "POST") { const nid = Math.max(0, ...insts.map((i) => i.id)) + 1, slug = nextSlug();
  // 防连点建重（§4）：确认词必须逐字等于服务端此刻刚算出的那个 slug。前端把确认卡上显示的
  // 名字回显上来（用户零打字）；真双击的第二下带的是过期名字，在这儿被挡掉，什么都不建。
  if ((b.confirm ?? "").trim() !== slug) return send(422, { detail: { ok: false, error: "confirm_mismatch", message: `没有创建任何东西：要建的实例是 ${slug}` } });
insts.push({ id: nid, slug, owner_label: null, host_id: b.host_id ?? 2, state: "deploying", dsh_version: null, config_version: "v2", dns_name: slug + ".example.com", released_at: null, updated_at: iso(), err: null });
return send(200, { instance_id: nid, dns_name: slug + ".example.com", job_id: mkJob("deploy", nid, 2600, "create") }); }
  const I = m(/^\/api\/instances\/(\d+)/) ? insts.find((i) => i.id === iid()) : null;
  if (m(/^\/api\/instances\/\d+$/) && req.method === "PATCH") { if (!I) return send(404, { detail: "实例不存在" }); I.owner_label = b.owner_label ?? ""; return send(200, detail(I)); }
  if (m(/^\/api\/instances\/\d+\/token$/)) { if (!I || gone(I)) return send(409, { detail: "该实例没有可用的容器" }); const t = tok[I.id] = "tok" + Math.floor(Math.random() * 1e6) + "x9"; return send(200, { token: t, url: `https://${I.dns_name}/?token=${t}`, captured_at: iso() }); }
  if (m(/^\/api\/instances\/\d+\/events$/)) return I ? send(200, evts(I)) : send(404, { detail: "实例不存在" });
  if (m(/^\/api\/instances\/\d+\/restart$/)) { if (!I) return send(404, { detail: "实例不存在" }); if (gone(I)) return send(409, { ok: false, error: "cannot_restart_from_" + I.state }); I.state = "restarting"; return send(200, { job_id: mkJob("restart", I.id, 1600) }); }
  // 续建统一入口（§4）：pending/error 续建、running/unhealthy 升级重建、released 恢复，
  // 都跑同一张 deploy 工单；body 可选 {image}，留空 = 站点当前 tag（§6.1）。purged 拒绝。
  if (m(/^\/api\/instances\/\d+\/deploy$/)) { if (!I) return send(404, { detail: "实例不存在" });
    if (I.state === "purged") return send(409, { ok: false, error: "cannot_deploy_from_purged" });
    if (b.image) I.tag = String(b.image);
    const purpose = I.state === "released" ? "restore" : I.state === "running" || I.state === "unhealthy" ? "upgrade" : "create";
    if (I.state === "released") I.state = "deploying";   // 恢复：先回 deploying，工单跑完才 running
    return send(200, { job_id: mkJob("deploy", I.id, 1500, purpose) }); }
  // 彻底释放（§6.4）：仅 released；confirm 逐字等于 slug（前端填），否则 422 confirm_mismatch。
  if (m(/^\/api\/instances\/\d+\/purge$/)) { if (!I) return send(404, { detail: "实例不存在" });
    if (I.state !== "released") return send(409, { ok: false, error: "cannot_purge_from_" + I.state });
    if (b.confirm !== I.slug) return send(422, { ok: false, error: "confirm_mismatch" });
    return send(200, { job_id: mkJob("purge", I.id, 1300) }); }
  if (m(/^\/api\/instances\/\d+\/release$/)) { if (!I) return send(404, { detail: "实例不存在" }); if (gone(I)) return send(409, { ok: false, error: "already_released" }); if (b.confirm !== I.slug) return send(422, { ok: false, error: "confirm_mismatch" }); return send(200, { job_id: mkJob("release", I.id, 1300) }); }
  if (m(/^\/api\/instances\/\d+\/dsh-config$/)) return I ? send(200, DSHC(I)) : send(404, { detail: "实例不存在" });
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
}).listen(Number(process.env.PORT ?? 13300), "127.0.0.1", function () { console.log(`dsh-control mock → http://127.0.0.1:${this.address().port} (key: dshc-demo)`); });
