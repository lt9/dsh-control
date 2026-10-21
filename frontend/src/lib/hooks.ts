import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Job, type LogItem, type Step } from "../api/client";

/** 进页取一次 + 手动刷新；失败不清旧数据。 */
export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true; // StrictMode 下 remount 会先跑上一轮的 cleanup，这里必须复位
    return () => { alive.current = false; };
  }, []);
  const run = useCallback(async () => {
    setLoading(true);
    try {
      const d = await fn();
      if (alive.current) { setData(d); setError(null); }
    } catch (e) {
      if (alive.current) setError(e instanceof Error ? e.message : String(e));
    } finally {
      if (alive.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  useEffect(() => { void run(); }, [run]);
  return { data, error, loading, reload: run, setData };
}

export type JobFeed = {
  job: Job | null;
  logs: LogItem[];
  transport: "sse" | "poll";
  error: string | null;
};

/**
 * 任务实时进度。两条路都实现：SSE `/api/jobs/{id}/stream`（step/log/done）为主；
 * 连不上或 3 秒收不到第一帧，自动回落轮询 GET /jobs/{id} + /logs?after=。
 */
export function useJobFeed(jobId: number | null): JobFeed {
  const [job, setJob] = useState<Job | null>(null);
  const [logs, setLogs] = useState<LogItem[]>([]);
  const [transport, setTransport] = useState<"sse" | "poll">("sse");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (jobId == null) return;
    let closed = false;
    let pollTimer: number | undefined;
    let guardTimer: number | undefined;
    let es: EventSource | null = null;
    let sawSse = false;
    let nextLogId = 0;
    setLogs([]); setJob(null); setError(null); setTransport("sse");

    const pushLogs = (items: LogItem[]) => {
      const fresh = (items ?? []).filter((i) => i.id > nextLogId);
      if (!fresh.length) return;
      nextLogId = Math.max(nextLogId, ...fresh.map((i) => i.id));
      setLogs((prev) => [...prev, ...fresh].slice(-800));
    };
    const mergeStep = (raw: Step | { steps?: Step[]; state?: Job["state"] }) => {
      setJob((prev) => {
        const base: Job = prev ?? { kind: "deploy", state: "running", steps: [] };
        if (Array.isArray((raw as { steps?: Step[] }).steps)) {
          return { ...base, ...(raw as Job) };
        }
        const step = raw as Step;
        const steps = base.steps.slice();
        const at = steps.findIndex((s) => s.seq === step.seq);
        if (at >= 0) steps[at] = step; else steps.push(step);
        steps.sort((a, b) => a.seq - b.seq);
        return { ...base, steps };
      });
    };
    const stopAll = () => {
      if (pollTimer) { clearInterval(pollTimer); pollTimer = undefined; }
      if (es) { es.close(); es = null; }
    };
    const startPoll = () => {
      if (closed || pollTimer) return;
      if (es) { es.close(); es = null; }
      setTransport("poll");
      const tick = async () => {
        try {
          const [j, l] = await Promise.all([api.job(jobId), api.jobLogs(jobId, nextLogId)]);
          if (closed) return;
          setJob(j); pushLogs(l.items ?? []);
          if (j.state === "succeeded" || j.state === "failed") {
            if (pollTimer) { clearInterval(pollTimer); pollTimer = undefined; }
          }
        } catch (e) {
          if (!closed) setError(e instanceof Error ? e.message : String(e));
        }
      };
      void tick();
      pollTimer = window.setInterval(() => void tick(), 1600);
    };

    try {
      es = new EventSource(api.jobStreamUrl(jobId), { withCredentials: true });
      es.addEventListener("step", (e) => {
        sawSse = true;
        try { mergeStep(JSON.parse((e as MessageEvent).data)); } catch { /* 忽略坏帧 */ }
      });
      es.addEventListener("log", (e) => {
        sawSse = true;
        try {
          const d = JSON.parse((e as MessageEvent).data);
          pushLogs(Array.isArray(d) ? d : [d]);
        } catch { /* 忽略坏帧 */ }
      });
      es.addEventListener("done", () => {
        sawSse = true;
        void api.job(jobId)
          .then((j) => { if (!closed) setJob(j); })
          .catch(() => { if (!closed) setJob((p) => (p ? { ...p, state: "succeeded" } : p)); });
        if (es) { es.close(); es = null; }
      });
      es.onerror = () => startPoll();
      // SSE 建连成功但一直无帧（反代吞流）也要回落。
      guardTimer = window.setTimeout(() => { if (!sawSse) startPoll(); }, 3000);
    } catch {
      startPoll();
    }

    return () => {
      closed = true;
      if (guardTimer) clearTimeout(guardTimer);
      stopAll();
    };
  }, [jobId]);

  return { job, logs, transport, error };
}
