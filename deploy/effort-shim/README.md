# home-effort-shim

家庭模型链路上唯一由我们拥有的适配层：

```
DSH(家庭实例) → tdai-proxy(:8096) → home-effort-shim(:8085) → radiance(:8080) → Qwen3.8-Flash-Next
```

DSH ≥ 0.2.1-rc.5 的推理枚举 `off|low|high|max` 与本站 Qwen3.8 chat template 的
`low|medium|xhigh` 不同套，`max`/`high` 直发网关会 400。shim 在代理与网关之间做
一次性平移（`max→xhigh`、`high→medium`，模板认识的值原样透传）。

这样 **记忆代理与 radiance 都保持上游原版**，将来任何一方升级（`start-proxy.sh`
会重新 pull 官方镜像）都不会丢掉这层逻辑。

## 部署（pipboy 192.168.3.109）

```bash
docker build -t home-effort-shim:0.1 deploy/effort-shim
docker run -d --name home-effort-shim --restart unless-stopped \
  --network tdai-memory-stack -p 127.0.0.1:8085:8085 \
  home-effort-shim:0.1
```

代理侧唯一接线：`deploy/global-images/.proxy-config/config.yaml` 的
`upstream.url` 指 `http://home-effort-shim:8085`（容器名走 docker DNS），
改完 `docker restart tdai-proxy`。

## 验证 / 撤法

- `curl -s 127.0.0.1:8085/healthz` → `ok`
- 发 `"thinking":{"type":"enabled"},"output_config":{"effort":"max"}` 到
  `:8096/dsh/default/v1/messages` 应 200 且带 thinking 块；
  `docker logs home-effort-shim` 有 `translate … max>xhigh -> 200` 日志。
- 撤掉这层：把 `upstream.url` 改回 `http://192.168.3.109:8080`、重启 tdai-proxy
  （但 DSH 侧得先降回能直发的档位，否则 max 会 400）。
