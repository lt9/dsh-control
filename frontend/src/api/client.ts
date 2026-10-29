// dsh-control API 客户端：契约唯一依据 docs/ARCHITECTURE.md §4。
// 字段名逐字照契约。登录靠 httpOnly cookie（dshc_session），前端不存任何 token；
// 写操作统一带 X-Requested-With: 1（CSRF 抑制，§4）。

export type InstanceState =
  | "pending" | "deploying" | "running" | "unhealthy"
  | "restarting" | "released" | "purged" | "error";
export type JobKind = "deploy" | "release" | "restart" | "purge" | "publish" | "memory" | "capability";
// §4：deploy 工单的来路，服务端入队时按当时状态写死（新建 / running 续建 / released 恢复）。
// 工单页的标题与完成文案看这个，不看 URL——手敲链接、分享链接也得说对话。
export type JobPurpose = "create" | "upgrade" | "restore";
export type JobState = "pending" | "running" | "succeeded" | "failed";
export type StepState = "pending" | "running" | "succeeded" | "failed" | "skipped";

export interface Session { ok: boolean; expires_at?: string | null }

export interface Host {
  id: number; name: string; enabled?: boolean; note?: string | null;
  data_root?: string | null; [k: string]: unknown;
}

export interface Overview {
  running: number; unhealthy: number; released: number;
  hosts: number; by_host: unknown[];
  /** 站点统一域名后缀（确认卡显示完整域名用；字段名以后端为准，前端防御式读取）。 */
  dns_zone?: string | null;
}

export interface InstanceRow {
  id: number; slug: string; owner_label: string | null;
  state: InstanceState; dsh_version: string | null; config_version: string | null;
  host_id: number | null; host_name: string | null;
  dns_name: string | null; updated_at: string | null;
  released_at: string | null;
  /** 彻底释放时间；仅 purged 有值（§3/§4）。 */
  purged_at?: string | null;
  /** 保留期满日 = released_at + 保留期；仅 released 有值（§4）。
   *  列表响应形状契约没逐字给出，这里按详情同名字段防御式读，没有就不显示。 */
  purge_after?: string | null;
}

/** §4 actions：布尔开关由服务端算，前端只看它，不自己推状态。
 *  restart/release 是契约里就有的两个键；其余三个是这一期新加的，
 *  后端可能还在路上，所以类型上留可空——缺键 = 按钮不出现（宁可少给入口）。 */
export interface InstanceActions {
  restart: boolean; release: boolean;
  /** running/unhealthy 可「升级/重建」（走 deploy，不传 image）。 */
  upgrade?: boolean;
  /** released 且未 purge → 可恢复（走 deploy）。 */
  restore?: boolean;
  /** 仅 released 可彻底释放。 */
  purge?: boolean;
  /** 旧键：开通失败后的「重新开通」。契约 §4 的示例里没这一条，保留兼容读法。 */
  deploy?: boolean;
}

export interface InstanceDetail {
  id: number; slug: string; owner_label: string | null;
  host: { id: number; name: string } | null;
  state: InstanceState; desired_state: string;
  dsh_version: string | null; image_tag: string | null; image_id: string | null;
  config_version: string | null;
  config: { items: { key: string; value: string; label: string; help: string }[] } | null;
  /** DSH 自己那套（模型/压缩/插件/技能/预设）。老快照没这一组 → null */
  dsh_config?: DshConfig | null;
  network: {
    dns_name: string | null; public_url: string | null;
    frp_remote_port: number | null; frp_proxy_id: number | string | null;
    dns_record_id: string | null; container_ip: string | null;
  } | null;
  runtime: {
    docker_state: string | null; health: string | null; restart_count: number | null;
    cpu_pct: number | null; mem_usage_bytes: number | null; mem_limit_bytes: number | null;
    captured_at: string | null;
  } | null;
  access: {
    has_token: boolean; token_masked: string | null; url: string | null;
    url_masked?: string | null;
    captured_at: string | null;
  } | null;
  last_error: string | null; released_at: string | null;
  /** 彻底释放时间（终态那一刻）。 */
  purged_at?: string | null;
  /** 保留期满日；仅 released 给，其余状态 null（§4）。前端据此显示「还剩 N 天」。 */
  purge_after?: string | null;
  actions: InstanceActions;
}

/** 详情页「DSH 配置」卡：后端把 `dsh web --dump-config` 读成分组事实。 */
export interface DshConfigGroup {
  title: string;
  items: { key: string; value: string; note: string }[];
}
export interface DshConfig {
  captured_at: string; dsh_version: string; model: string; base_url: string;
  agent_team: boolean; office_skills: boolean; skills: string[]; bundles: string[];
  groups: DshConfigGroup[]; source: string;
}

export interface TokenResp { token: string | null; url: string | null; captured_at: string | null }

export interface Step { seq: number; key: string; title: string; state: StepState; detail: string | null }
export interface Job {
  kind: JobKind; state: JobState; steps: Step[];
  /** §4：deploy 工单的来路，服务端入队时按当时状态定死；其余 kind 为 null。 */
  purpose?: JobPurpose | null;
  instance_id?: number | null; error?: string | null;
}
export interface LogItem { id: number; seq: number; level: string; line: string; ts: string }
export interface JobLogs { items: LogItem[]; next: number }
export interface EventItem { id: number; kind: string; message: string; created_at: string; data?: unknown }

/** §6.1.5 镜像分发方式：ssh=面板管道直传（局域网快路）；download=目标机自己从面板公网域拉制品。 */
export type ShipMode = "ssh" | "download";
/** §6.1.6 分发渠道：console=§6.1.5 的 /dl/{token}/（永远兜底）；github/gitee=公共 Releases 直链。 */
export type ShipChannel = "console" | "github" | "gitee";
/** 能配令牌、能往外推的那两个渠道（console 是内置的，没有令牌概念）。 */
export type MirrorName = "github" | "gitee";
/** 最新制品在某渠道的推送状态；image_channels 里没那一行=从没推过 → null。 */
export type ChannelState = "ok" | "failed" | "pending" | null;

/** §6.1.6 渠道行。注意：**令牌永不出网**，这里只有 token_set 这个布尔。 */
export interface MirrorChannel {
  /** 形如 lt9/dsh-control；没配 = 空串。 */
  repo: string;
  token_set: boolean;
  latest_state: ChannelState;
  /** 目标机可直连的入口：github=asset 直链，gitee=manifest 直链。公共 CDN，无秘密。 */
  url: string | null;
  error: string | null;
  updated_at: string | null;
}
/** GET /settings：base_url 给的是**解析后的生效值**（留空时=站点公网域）。 */
export interface Settings {
  image_ship: {
    mode: ShipMode;
    base_url: string;
    /** 探测优先序（§6.1.6），默认 gitee→github→console。 */
    mirror_order: ShipChannel[];
    /** 发布时是否顺道推外部渠道，默认 true。 */
    auto_mirror: boolean;
  };
  mirrors: Record<MirrorName, MirrorChannel>;
}
/** PUT 是局部更新（只认已知键），所以全都可选。 */
export interface SettingsPatch {
  image_ship?: { mode?: ShipMode; base_url?: string; mirror_order?: ShipChannel[]; auto_mirror?: boolean };
  /** 仓库名在 PUT 白名单里（§4）；令牌不在这儿——走 PUT /api/mirror-tokens/{channel}。 */
  mirrors?: { github?: { repo: string }; gitee?: { repo: string } };
}
/** §4 制品行：token 就是门票（公开免鉴权下载），url 是解析后的完整链接。 */
export interface ImageArtifact {
  id: number; tag: string; size_bytes: number | null; sha256: string | null;
  token: string | null; url: string | null;
  origin_host: string | null; created_at: string | null;
}
export interface ImageList { items: ImageArtifact[]; store_free_bytes: number | null }

/** GET /instances/{slug}/memory（api/memory.py 落地形状）：三层事实分开给——
 *  enabled/missing 是站点配没配；wired/legacy_p0 是容器里的接线事实；hub 是栈探活。
 *  wired=null 是宿主机够不着（容器侧事实未知），不是「没接」。 */
export interface MemoryStatus {
  ok: boolean; slug: string;
  enabled: boolean; missing: string;
  wired: boolean | null; legacy_p0: boolean | null;
  user: { user_id: string; key_masked: string; key_status: "valid" | "invalid" | "absent" | null } | null;
  hub: { core_ok: boolean; proxy_tcp: boolean | null; error?: string } | null;
  last_job: { id: number; state: string; params: Record<string, unknown> } | null;
}

/** /memory 页成员治理行（批量路由定稿形状）：hub 够不着时 key_status=null、
 *  state="unknown"，hub_note 是人话原因（属于错误文案，可直接上屏）。 */
export interface MemoryMember {
  slug: string; user_id: string; key_masked: string;
  key_status: "valid" | "invalid" | "absent" | null;
  state: "active" | "disabled" | "unknown";
  hub_note: string;
}

/** 能力目录行（api/capabilities.py）：一行一个「能力+版本」，current 打星。
 *  params 是 manifest 的 schema 字典，secret 项后端只标不显值。 */
export interface CatalogCapability {
  capability_id: string; version: string; kind: string;
  title: string; state: string; current: boolean;
  compatible_dsh: string; on_missing: string; kit_version: string;
  note: string; params: Record<string, unknown>;
  default_on: boolean; restart_required: boolean;
}

/** 实例能力台账行 + 后端算好的对账结论（drift/unknown_version 前端照字段渲染，不自己推）。 */
export interface InstanceCapability {
  capability_id: string; title: string; kind: string;
  desired_state: "present" | "absent"; desired_version: string; want_version: string;
  installed: boolean; enabled: boolean; observed_version: string; marker_present: boolean;
  health: string; health_detail: string; reason: string; last_job_id: number | null;
  observed_at: string | null; in_catalog: boolean;
  drift: boolean; unknown_version: boolean;
  /** 后端给的按钮依据（人话标签：升级|重试 / 启用|禁用 / 卸载）。 */
  actions: string[];
}

export class ApiError extends Error {
  status: number;
  /** §4.1 机器码（响应里有的话）。文案由前端自己组织，需要按码分情况的界面认这个，
   *  不去猜后端那行人话——例如新建被 confirm_mismatch 挡下时，界面该说的是"重新取号
   *  再点一次"，而不是后端那句"填进确认框"（这一期根本没有输入框）。 */
  code: string | null;
  constructor(status: number, message: string, code: string | null = null) {
    super(message); this.status = status; this.code = code;
  }
}

async function http<T>(
  path: string,
  init?: RequestInit & { json?: unknown; rw?: boolean },
): Promise<T> {
  const headers: Record<string, string> = {};
  const method = (init?.method ?? "GET").toUpperCase();
  const opts: RequestInit = { method, credentials: "same-origin", headers };
  if (init?.json !== undefined) {
    headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(init.json);
  }
  // §4：写操作要求 X-Requested-With: 1。普通 GET 不带（省一次 preflight），
  // 但"方法写着 GET、其实会改动服务器状态"的接口必须显式传 rw:true——
  // 现取登录密钥就是这种（SSH 进容器抓日志再落库）。漏带头时详情页每次进页
  // 都吃 422 missing_x_requested_with，用户只看到"现取失败"四个字。
  if (method !== "GET" || init?.rw) headers["X-Requested-With"] = "1";
  let res: Response;
  try {
    res = await fetch(`/api${path}`, opts);
  } catch {
    throw new ApiError(0, "网络不可达：控制台服务没在跑？");
  }
  if (res.status === 401 && !path.startsWith("/login") && !path.startsWith("/session")) {
    window.dispatchEvent(new Event("dshc:unauthorized"));
  }
  let body: unknown = null;
  try { body = await res.json(); } catch { /* 无 JSON 体 */ }
  if (!res.ok) {
    const b = body as { detail?: unknown; error?: string; message?: string } | null;
    const d = b?.detail;
    const one = (s: string) => (s && /[。.！]$/.test(s) ? s : s + "。");
    // 机器码先抠出来（两种形状：{error} 与 {detail:{error,message}}），给按码分情况的界面用
    const code = typeof b?.error === "string" ? b.error
      : d && typeof d === "object" && !Array.isArray(d) && typeof (d as { error?: unknown }).error === "string"
        ? (d as { error: string }).error : null;
    const detail = typeof d === "string" ? d
      : d && typeof d === "object" && !Array.isArray(d)
        ? (() => {
            const o = d as { error?: string; message?: string };
            return o.message ? one(o.message) : o.error ? one(errZh(o.error) || `出错：${o.error}`) : null;
          })()
        : typeof b?.error === "string" ? one(errZh(b.error) || b.error)
        : typeof b?.message === "string" ? one(b.message)
        : Array.isArray(d) ? "提交内容没有通过校验。" : null;
    throw new ApiError(res.status, detail ?? `请求失败（HTTP ${res.status}）。`, code);
  }
  return body as T;
}

/** 后端返回机器码时翻成人话；未知码原样带出（好过 [object Object]）。 */
const ERR_ZH: Record<string, string> = {
  bad_key: "运维密钥不对",
  unauthenticated: "登录过期了，重新登录",
  rate_limited: "试得太快，等一分钟再试",
  not_found: "找不到了，回台账看看",
  confirm_mismatch: "页面数据过期了，刷新后再试",
  already_released: "已经释放过了",
  cannot_purge: "只有已释放的实例能彻底释放",
  cannot_restart: "当前状态不能重启",
  cannot_deploy: "当前状态不能重新开通",
  // —— 记忆启停（api/memory.py）：invalid_action 422；cannot_memory_from_<state>
  //    走下面的 cannot_ 前缀退化，一个词条吃所有状态后缀 ——
  invalid_action: "这个动作现在做不了",
  cannot_memory: "只有运行中的实例能启用或停用记忆；已释放的实例记忆留在 Hub，恢复后再启用",
  // 记忆成员治理（lead 侧批量路由）与工单撞车的兜底词条：
  job_in_progress: "该实例已有任务在跑，等它结束再操作",
  memory_admin_key_missing: "记忆管理密钥还没登记进凭据，控制台代不进 Panel",
  memory_unavailable: "记忆中枢还没配好——站点的记忆配置配全后这里才可用",
  // —— 能力（api/capabilities.py）——
  instance_not_found: "这台实例不在了，回台账看看",
  capability_not_published: "目录里没有这个能力的可用版本",
};

/** `cannot_purge_from_running` 这类带状态后缀的码，查表前先退到前缀（§4.1 的写法）。 */
export function errZh(code: string): string {
  if (ERR_ZH[code]) return ERR_ZH[code];
  if (!code.startsWith("cannot_")) return code;
  const prefix = code.split("_from_")[0];
  return ERR_ZH[prefix] ?? code;
}

// —— 列表/概览的响应形状契约未逐字给出：做防御性归一（见回报「契约疑问」）。 ——

function pickHost(v: unknown): { id: number | null; name: string | null } {
  if (v && typeof v === "object") {
    const o = v as { id?: number; name?: string };
    return { id: o.id ?? null, name: o.name ?? null };
  }
  return { id: typeof v === "number" ? v : null, name: null };
}

function toRow(raw: Record<string, unknown>): InstanceRow {
  const h = pickHost(raw.host ?? (raw.host_id != null ? raw.host_id : null) ?? null);
  return {
    id: Number(raw.id), slug: String(raw.slug ?? ""),
    owner_label: (raw.owner_label as string) ?? null,
    state: (raw.state as InstanceState) ?? "pending",
    dsh_version: (raw.dsh_version as string) ?? null,
    config_version: (raw.config_version as string) ?? null,
    host_id: h.id ?? (raw.host_id as number) ?? null,
    host_name: h.name ?? (raw.host_name as string) ?? null,
    dns_name: (raw.dns_name as string) ?? null,
    released_at: (raw.released_at as string) ?? null,
    purged_at: (raw.purged_at as string) ?? null,
    purge_after: (raw.purge_after as string) ?? null,
    updated_at: (raw.updated_at as string) ?? null,
  };
}

function toList(raw: unknown): Record<string, unknown>[] {
  if (Array.isArray(raw)) return raw as Record<string, unknown>[];
  const o = raw as { items?: unknown[]; instances?: unknown[] } | null;
  return (o?.items ?? o?.instances ?? []) as Record<string, unknown>[];
}

const DEFAULT_MIRROR_ORDER: ShipChannel[] = ["gitee", "github", "console"];

/** §4 那行把这一段的字段写成 `{repo, token_set, state?}`，任务书里叫 latest_state——
 *  两个名字都认，别因为后端先落地哪一个而把界面做成空的。 */
function toChannel(v: unknown): MirrorChannel {
  const o = (v ?? {}) as Record<string, unknown>;
  const st = (o.latest_state ?? o.state ?? null) as unknown;
  return {
    repo: typeof o.repo === "string" ? o.repo : "",
    token_set: o.token_set === true,
    latest_state: st === "ok" || st === "failed" || st === "pending" ? st : null,
    url: typeof o.url === "string" ? o.url : null,
    error: typeof o.error === "string" ? o.error : null,
    updated_at: typeof o.updated_at === "string" ? o.updated_at : null,
  };
}

/** 这一段是这一期新加的，后端可能还在路上：缺的键一律落回"没配"，
 *  让界面显示成空态而不是崩成一片 undefined。优先序缺谁补谁——
 *  探测少一条路是真会坏事的。 */
function toSettings(raw: unknown): Settings {
  const o = (raw ?? {}) as Record<string, unknown>;
  const s = (o.image_ship ?? {}) as Record<string, unknown>;
  const m = (o.mirrors ?? {}) as Record<string, unknown>;
  const given = Array.isArray(s.mirror_order)
    ? s.mirror_order.filter((c): c is ShipChannel => c === "console" || c === "github" || c === "gitee")
    : [];
  return {
    image_ship: {
      mode: s.mode === "download" ? "download" : "ssh",
      base_url: typeof s.base_url === "string" ? s.base_url : "",
      mirror_order: [...new Set([...given, ...DEFAULT_MIRROR_ORDER])],
      auto_mirror: s.auto_mirror !== false,
    },
    mirrors: { github: toChannel(m.github), gitee: toChannel(m.gitee) },
  };
}

/** 能力目录/台账的响应形状后端已定稿，这里照旧做防御式归一：缺键落回最保守值
 *  （没装、没启用、没漂移），让界面显示成空态而不是崩成一片 undefined。 */
function toCatalogItem(c: Record<string, unknown>): CatalogCapability {
  return {
    capability_id: String(c.capability_id ?? ""),
    version: String(c.version ?? ""),
    kind: String(c.kind ?? ""),
    title: String(c.title ?? c.capability_id ?? ""),
    state: String(c.state ?? ""),
    current: c.current === true,
    compatible_dsh: String(c.compatible_dsh ?? ""),
    on_missing: String(c.on_missing ?? ""),
    kit_version: String(c.kit_version ?? ""),
    note: String(c.note ?? ""),
    params: (c.params && typeof c.params === "object" ? c.params : {}) as Record<string, unknown>,
    default_on: c.default_on === true,
    restart_required: c.restart_required !== false,
  };
}

function toLedgerItem(c: Record<string, unknown>): InstanceCapability {
  return {
    capability_id: String(c.capability_id ?? ""),
    title: String(c.title ?? c.capability_id ?? ""),
    kind: String(c.kind ?? ""),
    desired_state: c.desired_state === "absent" ? "absent" : "present",
    desired_version: String(c.desired_version ?? ""),
    want_version: String(c.want_version ?? ""),
    installed: c.installed === true,
    enabled: c.enabled === true,
    observed_version: String(c.observed_version ?? ""),
    marker_present: c.marker_present === true,
    health: String(c.health ?? ""),
    health_detail: String(c.health_detail ?? ""),
    reason: String(c.reason ?? ""),
    last_job_id: typeof c.last_job_id === "number" ? c.last_job_id : null,
    observed_at: (c.observed_at as string) ?? null,
    in_catalog: c.in_catalog === true,
    drift: c.drift === true,
    unknown_version: c.unknown_version === true,
    actions: Array.isArray(c.actions)
      ? c.actions.filter((a): a is string => typeof a === "string") : [],
  };
}

export const api = {
  login: (key: string) =>
    http<Session>("/login", { method: "POST", json: { key } }),
  logout: () => http<unknown>("/logout", { method: "POST", json: {} }),
  session: () => http<Session>("/session"),

  overview: () => http<Overview>("/overview"),
  hosts: async (): Promise<Host[]> => toList(await http<unknown>("/hosts")) as unknown as Host[],
  instances: async (q?: string, state?: string): Promise<InstanceRow[]> => {
    const qs = new URLSearchParams();
    if (q) qs.set("q", q);
    if (state) qs.set("state", state);
    const s = qs.toString();
    const raw = await http<unknown>(`/instances${s ? `?${s}` : ""}`);
    return toList(raw).map(toRow);
  },
  /** 确认卡先取号：下一台叫什么、挂在哪个域名上（服务端说了算）。 */
  nextSlug: () => http<{ slug: string; dns_name: string }>("/instances/next-slug"),
  create: (body: { host_id?: number; confirm: string }) =>
    http<{ instance_id: number; dns_name: string; job_id: number }>("/instances",
      { method: "POST", json: body }),
  instance: (id: number | string) => http<InstanceDetail>(`/instances/${id}`),
  /** §4 现取：SSH 抓容器日志里的最新一键地址，落库后返回 {token,url,captured_at}。
   *  这是"GET 但会改动服务器状态"的接口，必须 rw:true 带上 X-Requested-With。 */
  token: (id: number | string) => http<Partial<TokenResp>>(`/instances/${id}/token`, { rw: true }),
  /** 现取 DSH 生效配置并刷新快照。同 /token：GET 但会改动服务器状态，rw:true。 */
  dshConfig: (id: number | string) => http<DshConfig>(`/instances/${id}/dsh-config`, { rw: true }),
  rename: (id: number | string, owner_label: string) =>
    http<unknown>(`/instances/${id}`, { method: "PATCH", json: { owner_label } }),
  restart: (id: number | string) =>
    http<{ job_id: number }>(`/instances/${id}/restart`, { method: "POST", json: {} }),
  /** §4 续建统一入口，三种用途共用这一个动作：
   *  · pending/error → 开通失败续建（从失败那步接着做，已建好的外部对象跳过）
   *  · running/unhealthy → 升级/重建（原地重建容器）
   *  · released → 恢复（重建解析/隧道/反代，数据原样）
   *  body 可选 `{image}`（§6.1：留空 = 站点当前目标 tag）。界面上永远留空——
   *  本期不做版本选择（§6.1），所以这里不传 image，也不发一个空串。 */
  deploy: (id: number | string, opts?: { image?: string }) =>
    http<{ job_id: number }>(`/instances/${id}/deploy`,
      { method: "POST", json: opts?.image ? { image: opts.image } : {} }),
  /** confirm 必须逐字等于 slug，由前端填（§4/§9），用户不打字；防误靠按住式确认 UI。 */
  release: (id: number | string, confirm: string) =>
    http<{ job_id: number }>(`/instances/${id}/release`, { method: "POST", json: { confirm } }),
  /** 彻底释放（§6.4）：删容器 + 删数据目录，不可恢复。仅 released 可发起；
   *  confirm 同样由前端填 slug，防误靠 §9 的长按确认。
   *  memory_disposal（P1）：确认卡上二选一——"keep" 保留记忆（默认）或
   *  "revoke" 停用业务用户；两种选择下记忆数据都留在 Hub 卷，这里从不删。 */
  purge: (id: number | string, confirm: string, memoryDisposal?: "keep" | "revoke") =>
    http<{ job_id: number }>(`/instances/${id}/purge`,
      { method: "POST", json: memoryDisposal ? { confirm, memory_disposal: memoryDisposal } : { confirm } }),

  // —— 记忆（api/memory.py：路径参数是 slug，不是数字 id）——
  memoryStatus: (slug: string) => http<MemoryStatus>(`/instances/${slug}/memory`),
  /** 只入队 kind=memory 工单，重跑=再点一次；{job_id} 后照常去工单页看进度。 */
  memoryAction: (slug: string, action: "enable" | "disable") =>
    http<{ job_id: number }>(`/instances/${slug}/memory`, { method: "POST", json: { action } }),
  /** 成员治理列表（批量路由在 lead 侧；没上线时按 not_found 降级成占位）。 */
  memoryMembers: async (): Promise<MemoryMember[]> => {
    const raw = await http<unknown>("/memory/members");
    const o = (Array.isArray(raw) ? { members: raw } : raw) as { members?: unknown[]; items?: unknown[] } | null;
    return ((o?.members ?? o?.items ?? []) as Record<string, unknown>[]).map((m) => ({
      slug: String(m.slug ?? ""),
      user_id: String(m.user_id ?? ""),
      key_masked: typeof m.key_masked === "string" ? m.key_masked : "",
      key_status: m.key_status === "valid" || m.key_status === "invalid" || m.key_status === "absent"
        ? m.key_status : null,
      state: m.state === "disabled" ? "disabled" : m.state === "unknown" ? "unknown" : "active",
      hub_note: typeof m.hub_note === "string" ? m.hub_note : "",
    }));
  },
  memoryMemberAction: (userId: string, action: "disable" | "reclaim") =>
    http<{ ok: boolean }>(`/memory/members/${encodeURIComponent(userId)}/action`,
      { method: "POST", json: { action } }),
  /** Panel 引导会话身份（挂 iframe 前问一次；user_id 空串=服务端问不到，前端用占位名）。 */
  panelIdentity: () =>
    http<{ ok: boolean; user_id: string; display_name: string }>("/memory/panel-identity"),

  // —— 能力目录（api/capabilities.py）——
  capabilities: async (state?: string): Promise<CatalogCapability[]> => {
    const qs = state ? `?state=${encodeURIComponent(state)}` : "";
    const raw = await http<unknown>(`/capabilities${qs}`);
    const o = (Array.isArray(raw) ? { items: raw } : raw) as { items?: unknown[] } | null;
    return ((o?.items ?? []) as Record<string, unknown>[]).map(toCatalogItem);
  },
  instanceCapabilities: async (id: number | string): Promise<InstanceCapability[]> => {
    const raw = await http<unknown>(`/instances/${id}/capabilities`);
    const o = (Array.isArray(raw) ? { items: raw } : raw) as { items?: unknown[] } | null;
    return ((o?.items ?? []) as Record<string, unknown>[]).map(toLedgerItem);
  },
  /** 一键动作都是工单（§vendor 决策三）：升级到 vX＝apply+version；重试＝apply 不带 version。 */
  capabilityAction: (id: number | string, body: {
    action: "install" | "apply" | "enable" | "disable" | "uninstall";
    capability: string; version?: string; params?: Record<string, unknown>;
  }) => http<{ job_id: number }>(`/instances/${id}/capability`, { method: "POST", json: body }),

  job: (id: number | string) => http<Job>(`/jobs/${id}`),
  jobLogs: (id: number | string, after: number) =>
    http<JobLogs>(`/jobs/${id}/logs?after=${after}`),
  jobStreamUrl: (id: number | string) => `/api/jobs/${id}/stream`,
  events: async (id: number | string): Promise<EventItem[]> =>
    toList(await http<unknown>(`/instances/${id}/events`)) as unknown as EventItem[],

  // —— 设置页（§6.1.5 镜像分发）——
  /** GET /settings：base_url 给的是**解析后的生效值**（留空时=站点公网域）。 */
  settings: async (): Promise<Settings> => toSettings(await http<unknown>("/settings")),
  /** PUT 是局部更新（§4：只认已知键），改完不重启生效，所以切完直接拿回执渲染。 */
  putSettings: async (patch: SettingsPatch): Promise<Settings> =>
    toSettings(await http<unknown>("/settings", { method: "PUT", json: patch })),
  /** 令牌即焚（§6.1.6）：PUT 上去只回 token_set，值不回前端，也没有任何 GET 拿得到它。 */
  putMirrorToken: (channel: MirrorName, token: string) =>
    http<{ ok: boolean; token_set: boolean }>(`/mirror-tokens/${channel}`,
      { method: "PUT", json: { token } }),
  images: async (): Promise<ImageList> => {
    const raw = await http<unknown>("/images");
    const o = (Array.isArray(raw) ? { items: raw } : raw) as
      { items?: unknown[]; store_free_bytes?: number | null } | null;
    const items = ((o?.items ?? []) as Record<string, unknown>[]).map((a) => ({
      id: Number(a.id),
      tag: String(a.tag ?? ""),
      size_bytes: (a.size_bytes as number) ?? null,
      sha256: (a.sha256 as string) ?? null,
      token: (a.token as string) ?? null,
      url: (a.url as string) ?? null,
      origin_host: typeof a.origin_host === "string" ? a.origin_host
        : pickHost(a.origin_host).name,
      created_at: (a.created_at as string) ?? null,
    }));
    return { items, store_free_bytes: (o?.store_free_bytes as number) ?? null };
  },
  /** 发布 = 开一张 publish 工单，进度照常规工单页看（§6.1.5）。 */
  publishImage: (body: { tag: string; from_host_id?: number }) =>
    http<{ job_id: number }>("/images", { method: "POST", json: body }),
  /** 下架：删行删文件，公开链接即刻失效（§4）。不可逆，界面上走长按确认。 */
  deleteImage: (id: number | string) =>
    http<unknown>(`/images/${id}`, { method: "DELETE", json: {} }),
};
