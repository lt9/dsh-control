// home-effort-shim —— 家庭模型链路上唯一由我们拥有的适配层。
//
// 位置：记忆代理（agentmemory/memory-proxy，上游镜像）→ **本 shim** → radiance 网关。
// 职责：DSH rc.5 的推理枚举 `off|low|high|max` 与自家 Qwen3.8 chat template 的
// `low|medium|xhigh` 不同套，`max`/`high` 会被网关 400 拒（2026-10-30 现网实见）。
// 这里做一次性平移；两个第三方组件（代理、网关）都保持上游原样，将来随便升级。
//
// 除 POST JSON 请求体里的 effort 字段外，一切原样透传（含 SSE 流式）。
// 记档与撤法见 docs/site/MEMORY-P0.md。

import http from "node:http";

const UPSTREAM = process.env.SHIM_UPSTREAM || "http://192.168.3.109:8080";
const PORT = Number(process.env.SHIM_PORT || 8085);

// 本站模型 chat template 认识的档位；认识的原样过，不认识才映射。
const TEMPLATE_RUNGS = new Set(["low", "medium", "xhigh", "none", "minimal"]);
// DSH rc.5 枚举 → 本站档位（max=最高档→xhigh；high→medium，语义居中）。
const EFFORT_MAP = { max: "xhigh", high: "medium" };

function translateBody(body) {
  const changes = [];
  if (body === null || typeof body !== "object" || Array.isArray(body)) return changes;
  const oc = body.output_config;
  if (oc && typeof oc === "object" && typeof oc.effort === "string") {
    const from = oc.effort;
    const to = TEMPLATE_RUNGS.has(from) ? from : EFFORT_MAP[from];
    if (to && to !== from) { oc.effort = to; changes.push(`output_config.effort:${from}>${to}`); }
  }
  if (typeof body.reasoning_effort === "string") {
    const from = body.reasoning_effort;
    const to = TEMPLATE_RUNGS.has(from) ? from : EFFORT_MAP[from];
    if (to && to !== from) { body.reasoning_effort = to; changes.push(`reasoning_effort:${from}>${to}`); }
  }
  return changes;
}

const HOP_BY_HOP = new Set(["connection", "keep-alive", "transfer-encoding", "content-length", "content-encoding"]);

const server = http.createServer((req, res) => {
  if (req.url === "/healthz") { res.writeHead(200, { "content-type": "text/plain" }); res.end("ok"); return; }

  const chunks = [];
  req.on("data", (c) => chunks.push(c));
  req.on("end", async () => {
    let payload = Buffer.concat(chunks);
    const ct = String(req.headers["content-type"] || "");
    let changes = [];
    if (req.method === "POST" && ct.includes("json") && payload.length > 0) {
      try {
        const body = JSON.parse(payload.toString("utf8"));
        changes = translateBody(body);
        if (changes.length) payload = Buffer.from(JSON.stringify(body));
      } catch { /* 非法 JSON：原样透传，让上游报它自己的错 */ }
    }

    const headers = {};
    for (const [k, v] of Object.entries(req.headers)) {
      if (k === "host" || k === "content-length" || HOP_BY_HOP.has(k)) continue;
      if (v !== undefined) headers[k] = v;
    }
    try {
      const upstream = await fetch(new URL(req.url, UPSTREAM), {
        method: req.method, headers,
        body: req.method === "GET" || req.method === "HEAD" || payload.length === 0 ? undefined : payload,
        duplex: "half",
      });
      const outHeaders = {};
      upstream.headers.forEach((v, k) => { if (!HOP_BY_HOP.has(k)) outHeaders[k] = v; });
      res.writeHead(upstream.status, outHeaders);
      if (upstream.body) {
        const { Readable } = await import("node:stream");
        Readable.fromWeb(upstream.body).pipe(res);
      } else res.end();
      if (changes.length) console.log(`translate ${req.method} ${req.url} ${changes.join(" ")} -> ${upstream.status}`);
    } catch (e) {
      res.writeHead(502, { "content-type": "application/json" });
      res.end(JSON.stringify({ type: "error", error: { type: "bad_gateway", message: `effort-shim upstream failed: ${e.message}` } }));
      console.error(`502 ${req.method} ${req.url}: ${e.message}`);
    }
  });
});

server.listen(PORT, "0.0.0.0", () => console.log(`home-effort-shim :${PORT} -> ${UPSTREAM} (map: max->xhigh, high->medium)`));
