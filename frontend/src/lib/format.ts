// 时间与状态语义。所有时间一律按 Asia/Shanghai 显示（§9）。

import type {
  ChannelState, InstanceCapability, InstanceState, JobKind, JobPurpose, JobState, MemoryStatus,
  ShipChannel, ShipMode, StepState,
} from "../api/client";

const TZ = "Asia/Shanghai";

const shortFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ, month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", hour12: false,
});
const fullFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const clockFmt = new Intl.DateTimeFormat("zh-CN", {
  timeZone: TZ, hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

export function fmtShort(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : shortFmt.format(d);
}
export function fmtFull(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : fullFmt.format(d);
}
export function fmtClock(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : clockFmt.format(d);
}

export function fmtRel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso).getTime();
  if (Number.isNaN(d)) return iso;
  const diff = Date.now() - d;
  if (diff < 45_000) return "刚刚";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)} 小时前`;
  if (diff < 30 * 86_400_000) return `${Math.round(diff / 86_400_000)} 天前`;
  return fmtShort(iso);
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null) return "—";
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(0)} MB`;
  return `${Math.round(n / 1e3)} KB`;
}

const dateFmt = new Intl.DateTimeFormat("en-CA", {
  timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
});

/** 只到日的日期，形状就是契约里写的 YYYY-MM-DD（§4 的 purge_after 文案要用它）。 */
export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : dateFmt.format(d);
}

/** 保留期还剩几天（向上取整：还剩 3 小时也说「还剩 1 天」，不写成 0 天吓人）。
 *  到期之后系统自己会动手，所以这里说「已到期」而不是「剩 0 天」。 */
export function fmtDaysLeft(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return null;
  const diff = t - Date.now();
  if (diff <= 0) return "已到期";
  return `还剩 ${Math.ceil(diff / 86_400_000)} 天`;
}

/** 状态语义全站唯一：running 绿 / unhealthy·restarting 琥珀 / error 红 / released 灰（§9）。
 *  tone 决定颜色，glyph 决定形状——颜色之外再叠一层形状冗余，不单靠色。
 *  purged 与 released 同为灰，靠 glyph（空圈加斜杠）+「删除态」文案分开：
 *  一个是停着等恢复，一个是连数据目录都没了，扫读时不能混。 */
export type Tone = "ok" | "warn" | "err" | "off" | "live" | "wait";
export const STATE_META: Record<InstanceState, { zh: string; tone: Tone; glyph: string }> = {
  running:    { zh: "运行中", tone: "ok",   glyph: "round" },
  unhealthy:  { zh: "不健康", tone: "warn", glyph: "triangle" },
  restarting: { zh: "重启中", tone: "warn", glyph: "triangle" },
  error:      { zh: "故障",   tone: "err",  glyph: "cross" },
  released:   { zh: "已释放", tone: "off",  glyph: "hollow" },
  purged:     { zh: "已彻底释放", tone: "off", glyph: "void" },
  deploying:  { zh: "部署中", tone: "live", glyph: "half" },
  pending:    { zh: "待部署", tone: "wait", glyph: "hollow" },
};

/** 详情页「记忆」小块的状态点：一台只给一个事实，解释全部在 tips 里（IV-8）。
 *  优先级：栈故障 > legacy > 已接 > 未接。wired=null 是宿主机够不着
 *  （容器侧事实未知），不能冒充「未接」——给「未确认」，点一下刷新就是重测。 */
export function memoryDot(m: MemoryStatus | null): { zh: string; tone: Tone; glyph: string } {
  if (!m) return { zh: "未确认", tone: "wait", glyph: "hollow" };
  if (!m.enabled) return { zh: "未接", tone: "off", glyph: "hollow" };
  if (m.hub && m.hub.core_ok === false) return { zh: "栈故障", tone: "err", glyph: "cross" };
  if (m.wired === true) {
    return m.legacy_p0 === true
      ? { zh: "legacy 手工", tone: "warn", glyph: "triangle" }
      : { zh: "已接", tone: "ok", glyph: "round" };
  }
  if (m.wired === false) return { zh: "未接", tone: "off", glyph: "hollow" };
  return { zh: "未确认", tone: "wait", glyph: "hollow" };
}

export const JOB_META: Record<JobKind, { zh: string }> = {
  deploy: { zh: "开通部署" },
  restart: { zh: "重启" },
  release: { zh: "释放" },
  purge: { zh: "彻底释放" },
  publish: { zh: "发布制品" },
  memory: { zh: "记忆启停" },
  capability: { zh: "能力操作" },
};

/** 能力 kind 的中文名（目录与详情小块共用）。 */
export const CAP_KIND_META: Record<string, { zh: string }> = {
  skill: { zh: "技能" },
  patch: { zh: "补丁" },
  plugin: { zh: "插件" },
  service: { zh: "服务" },
};

export const capKindZh = (kind: string) => CAP_KIND_META[kind]?.zh ?? (kind || "未知");

/** 实例能力块的状态点：对账结论后端已算好（drift/unknown_version/health），
 *  前端照字段渲染不自己推。优先级：drift > health down > degraded > unknown_version > 正常。 */
export function capabilityDot(c: InstanceCapability): { zh: string; tone: Tone; glyph: string } {
  if (c.desired_state === "absent") {
    return c.drift
      ? { zh: "说好卸载，还装着", tone: "warn", glyph: "triangle" }
      : { zh: "已卸载", tone: "off", glyph: "hollow" };
  }
  if (c.drift) return { zh: "没对上", tone: "err", glyph: "cross" };
  if (c.health === "down") return { zh: "健康故障", tone: "err", glyph: "cross" };
  if (c.health === "degraded") return { zh: "健康降级", tone: "warn", glyph: "triangle" };
  if (c.unknown_version) return { zh: "版本未知", tone: "warn", glyph: "triangle" };
  if (!c.installed) return { zh: "没装上", tone: "err", glyph: "cross" };
  if (!c.enabled) return { zh: "已禁用", tone: "off", glyph: "hollow" };
  return { zh: "正常", tone: "ok", glyph: "round" };
}

/** §6.1.5 两种分发方式。名字照契约，说明写清各自吃谁的带宽——选错方向的代价
 *  只有传过一次 GB 级镜像的人才知道，所以这一句必须写在按钮上而不是文档里。 */
export const SHIP_MODE_META: Record<ShipMode, { zh: string; use: string; note: string }> = {
  ssh: {
    zh: "SSH 直传",
    use: "局域网快路",
    note: "源机 docker save 的流经面板内存直接灌进目标机 docker load，两边都不落盘、两台之间也不用互信。慢的地方是它两头都吃控制台带宽——公网机之间走这条会很难看。",
  },
  download: {
    zh: "控制台下载",
    use: "公网机自取",
    note: "先把镜像发布成制品，目标机自己 curl 控制台公网域名去拉，拉完强制校验 sha256 再 load。控制台只被每台目标机拉一次，不再当转发的管道。",
  },
};

/** §6.1.6 三条渠道。名字与顺序解释见设置页那张表的 ? 气泡。 */
export const CHANNEL_META: Record<ShipChannel, { zh: string }> = {
  gitee: { zh: "Gitee" },
  github: { zh: "GitHub" },
  console: { zh: "控制台" },
};

/** 最新制品在某渠道的推送状态（image_channels）。null=那一行不存在=从没推过。 */
export const CHANNEL_STATE_META: Record<Exclude<ChannelState, null>, { zh: string; tone: Tone }> = {
  ok: { zh: "已推送", tone: "ok" },
  pending: { zh: "推送中", tone: "warn" },
  failed: { zh: "推送失败", tone: "err" },
};

/** deploy 工单的来路（§4：服务端入队时按当时状态定死，工单页据此出标题与完成文案，
 *  不靠 URL 猜）。同一个 deploy 工单三种来路，标题要是都写「开通部署」，点「升级 / 重建」
 *  跳过去看着像又开了一台新的。 */
export const PURPOSE_META: Record<JobPurpose, { zh: string }> = {
  create: { zh: "开通部署" },
  upgrade: { zh: "升级 / 重建" },
  restore: { zh: "恢复实例" },
};

export const JOB_STATE_META: Record<JobState, { zh: string; tone: Tone }> = {
  pending: { zh: "排队中", tone: "wait" },
  running: { zh: "进行中", tone: "live" },
  succeeded: { zh: "已完成", tone: "ok" },
  failed: { zh: "失败", tone: "err" },
};

export const STEP_STATE_META: Record<StepState, { zh: string; tone: Tone }> = {
  pending: { zh: "待执行", tone: "wait" },
  running: { zh: "执行中", tone: "live" },
  succeeded: { zh: "完成", tone: "ok" },
  failed: { zh: "失败", tone: "err" },
  skipped: { zh: "跳过", tone: "off" },
};

export function maskMiddle(v: string, keepHead = 4, keepTail = 3): string {
  if (v.length <= keepHead + keepTail + 2) return v;
  return `${v.slice(0, keepHead)}········${v.slice(-keepTail)}`;
}

/** 一键地址里的 token 段打码：只藏 token 值，域名照常可读。
 *  形态与"登录密钥"那一行统一（头 4 尾 3），否则同一枚密钥在同一个页面上
 *  一处显示 aftb········FFI、一处显示 ········，核对的人会以为不是同一枚。 */
export function maskUrlToken(url: string): string {
  return url.replace(/(token=)([^&#]+)/, (_m, p: string, tok: string) => `${p}${maskMiddle(tok)}`);
}
