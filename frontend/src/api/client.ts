// dsh-control API 客户端：契约唯一依据 docs/ARCHITECTURE.md §4。
// 字段名逐字照契约。登录靠 httpOnly cookie（dshc_session），前端不存任何 token；
// 写操作统一带 X-Requested-With: 1（CSRF 抑制，§4）。

export type InstanceState =
  | "pending" | "deploying" | "running" | "unhealthy"
  | "restarting" | "released" | "error";
export type JobKind = "deploy" | "release" | "restart";
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
}

export interface InstanceDetail {
  id: number; slug: string; owner_label: string | null;
  host: { id: number; name: string } | null;
  state: InstanceState; desired_state: string;
  dsh_version: string | null; image_tag: string | null; image_id: string | null;
  config_version: string | null;
  config: { items: { key: string; value: string; label: string; help: string }[] } | null;
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
  actions: { restart: boolean; release: boolean; deploy: boolean };
}

export interface TokenResp { token: string | null; url: string | null; captured_at: string | null }

export interface Step { seq: number; key: string; title: string; state: StepState; detail: string | null }
export interface Job {
  kind: JobKind; state: JobState; steps: Step[];
  instance_id?: number | null; error?: string | null;
}
export interface LogItem { id: number; seq: number; level: string; line: string; ts: string }
export interface JobLogs { items: LogItem[]; next: number }
export interface EventItem { id: number; kind: string; message: string; created_at: string; data?: unknown }

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) { super(message); this.status = status; }
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
    const detail = typeof d === "string" ? d
      : d && typeof d === "object" && !Array.isArray(d)
        ? (() => {
            const o = d as { error?: string; message?: string };
            return o.message ? one(o.message) : o.error ? one(ERR_ZH[o.error] ?? `出错：${o.error}`) : null;
          })()
        : typeof b?.error === "string" ? one(ERR_ZH[b.error] ?? b.error)
        : typeof b?.message === "string" ? one(b.message)
        : Array.isArray(d) ? "提交内容没有通过校验。" : null;
    throw new ApiError(res.status, detail ?? `请求失败（HTTP ${res.status}）。`);
  }
  return body as T;
}

/** 后端返回机器码时翻成人话；未知码原样带出（好过 [object Object]）。 */
const ERR_ZH: Record<string, string> = {
  bad_key: "运维密钥不对",
};

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
    updated_at: (raw.updated_at as string) ?? null,
  };
}

function toList(raw: unknown): Record<string, unknown>[] {
  if (Array.isArray(raw)) return raw as Record<string, unknown>[];
  const o = raw as { items?: unknown[]; instances?: unknown[] } | null;
  return (o?.items ?? o?.instances ?? []) as Record<string, unknown>[];
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
  create: (body: { host_id?: number }) =>
    http<{ instance_id: number; dns_name: string; job_id: number }>("/instances",
      { method: "POST", json: body }),
  instance: (id: number | string) => http<InstanceDetail>(`/instances/${id}`),
  /** §4 现取：SSH 抓容器日志里的最新一键地址，落库后返回 {token,url,captured_at}。
   *  这是"GET 但会改动服务器状态"的接口，必须 rw:true 带上 X-Requested-With。 */
  token: (id: number | string) => http<Partial<TokenResp>>(`/instances/${id}/token`, { rw: true }),
  rename: (id: number | string, owner_label: string) =>
    http<unknown>(`/instances/${id}`, { method: "PATCH", json: { owner_label } }),
  restart: (id: number | string) =>
    http<{ job_id: number }>(`/instances/${id}/restart`, { method: "POST", json: {} }),
  /** 开通失败后续建：从失败那一步接着做，已建好的外部对象会被跳过而不是重建。 */
  deploy: (id: number | string) =>
    http<{ job_id: number }>(`/instances/${id}/deploy`, { method: "POST", json: {} }),
  /** confirm 必须逐字等于 slug，由前端填（§4/§9），用户不打字；防误靠按住式确认 UI。 */
  release: (id: number | string, confirm: string) =>
    http<{ job_id: number }>(`/instances/${id}/release`, { method: "POST", json: { confirm } }),

  job: (id: number | string) => http<Job>(`/jobs/${id}`),
  jobLogs: (id: number | string, after: number) =>
    http<JobLogs>(`/jobs/${id}/logs?after=${after}`),
  jobStreamUrl: (id: number | string) => `/api/jobs/${id}/stream`,
  events: async (id: number | string): Promise<EventItem[]> =>
    toList(await http<unknown>(`/instances/${id}/events`)) as unknown as EventItem[],
};
