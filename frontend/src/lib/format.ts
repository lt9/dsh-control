// 时间与状态语义。所有时间一律按 Asia/Shanghai 显示（§9）。

import type { InstanceState, JobKind, JobState, StepState } from "../api/client";

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

/** 状态语义全站唯一：running 绿 / unhealthy·restarting 琥珀 / error 红 / released 灰（§9）。
 *  tone 决定颜色，glyph 决定形状——颜色之外再叠一层形状冗余，不单靠色。 */
export type Tone = "ok" | "warn" | "err" | "off" | "live" | "wait";
export const STATE_META: Record<InstanceState, { zh: string; tone: Tone; glyph: string }> = {
  running:    { zh: "运行中", tone: "ok",   glyph: "round" },
  unhealthy:  { zh: "不健康", tone: "warn", glyph: "triangle" },
  restarting: { zh: "重启中", tone: "warn", glyph: "triangle" },
  error:      { zh: "故障",   tone: "err",  glyph: "cross" },
  released:   { zh: "已释放", tone: "off",  glyph: "hollow" },
  deploying:  { zh: "部署中", tone: "live", glyph: "half" },
  pending:    { zh: "待部署", tone: "wait", glyph: "hollow" },
};

export const JOB_META: Record<JobKind, { zh: string }> = {
  deploy: { zh: "开通部署" },
  restart: { zh: "重启" },
  release: { zh: "释放" },
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

/** 一键地址里的 token 段打码：只藏 token 值，域名照常可读。 */
export function maskUrlToken(url: string): string {
  return url.replace(/(token=)[^&#]+/, (_m, p: string) => `${p}········`);
}
