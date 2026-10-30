# dsh-control 的 agent 作业规则

控制台（FastAPI+React，容器 `dsh-control` :13300）经 SSH 管理容器化 dsh 实例舰队。
本文件是给在此仓库干活的 agent 的硬规则，全部来自现网事故教训（详账 docs/site/MEMORY-P0.md）。

## 验收协议（不可绕过）

1. **模型/升级类大动作**：先在 nas-8 建测试容器（生产数据副本、`--user 1000:1000`、
   端口 14895 类），点对点跑通一次**真实对话**（发消息拿到回复）才算完成。
2. **记忆/功能类改动**：用 CDP（browser-use）真机操作页面体验功能，并到**记忆管理端**
   （hub :8125 面板 Chat_Memory / core write-l0 日志 / conversations jsonl）核对真实
   记录与沉淀。"容器日志没报错""徽标正常"都算表象，不算证据。
3. 日常运维动作走**控制台页面**（CDP 驱动）；只有镜像构建/发布准备允许 shell。
4. 生产实例白天不重启；用户说"开始搞"才是窗口期。重启/升级优先页面工单（jobs）。
5. 给非开发者（她）的功能只留**一条正确路径**：不做多选择器、多开关并存。

## 已固化的坑（改代码前先读）

- 会话投影 `modelSelection.lastUsed` 是事件日志回放**派生**的，直接改
  `storages/session_projcache/` 会被回写覆盖。换绑唯一正解：选择器选中→pending 落盘→
  下一条消息自愈。选择器插件 `ui-model-selection`（profile 里 disabled）。
- `ui-plugin-manager`：rc.5 bundle 出厂 disabled:true，profile 必须**显式** disabled:false。
- 记忆接线三件套（agent+task+user-session-default）必须在该用户**第一次聊天前**配齐
  （已并入 `ensure_instance_user`）。缺一个，代理首轮写死 bypassed 绑定，记忆空转；
  清毒=删 tdai-proxy proxy.db `sessions` 行 + 重启 tdai-proxy（内存缓存会留着毒）。
- home 层补丁是**整段替换**不是深合并；同 id 块后写优先。改 home 层用行级追加，
  别用 split 截断（截掉后面块炸过启动）。
- docker cp 进容器的文件必须 644（write 工具默认 600 会 EACCES 崩溃循环）。
- 复杂脚本一律落成文件再 scp+docker cp，别在 ssh 套引号里拼 heredoc。
- 109（记忆栈）登录：`ssh -i ~/.ssh/dsh-control_ed25519 twan@192.168.3.109`；
  nas-8 是 `-p 2333 tuan@192.168.3.8`；VPS 是 `-p 23654 -i secrets/vps_ed25519 root@8.134.168.14`。
- 控制台代码热更目标 = 容器 `dsh-control`（本机），两处都要 cp：
  `/usr/local/lib/python3.12/site-packages/dsh_control/...` 与 `/app/src/dsh_control/...`。
  别碰 tierai-prod-* 容器（同机跑的另一个产品）。
