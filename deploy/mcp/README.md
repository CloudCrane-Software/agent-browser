# browser-mcp · agent-browser 能力 → jiuwenswarm MCP 工具面（线4，2026-09-30）

把本仓线2 无头浏览器服务（HTTP API）与线3 搜索/抓取端点包装成 MCP tools，
挂进 GPU 机 jiuwenswarm 的 `mcp.servers`，使集群内 agent 把浏览器/搜索/抓取
当作一等通用工具调用。**零重复实现**：本目录只有 stdio 转调层，全部语义
（allowlist fail-closed、重定向落点复检、审计、type 不回显）由线2/线3 服务端
负责并各自带测试。

```
agent（jiuwenswarm 深度代理）
  │ MCP stdio（jiuwenswarm-app 按 config.yaml mcp.servers 拉起）
  ▼
browser_mcp_server.py（fastmcp 2.14.7，本文件同目录）   ← 唯一新增物
  │ HTTP（tailnet 环回）
  ▼
agent-browser 无头服务  100.64.0.7:8125（线2，anuser 用户级 systemd）
  ▼
PlaywrightDriver + BrowserPool → chrome-headless-shell（线1 引擎底座）
```

## 工具面（10 个，全部转调）

| tool | 上游端点 | 返回/语义 |
| --- | --- | --- |
| `browser_health` | `GET /health` | 服务与池状态，不占会话配额 |
| `browser_open(allowlist, url?)` | `POST /session` | `{"session_id"}`；allowlist 非空 fail-closed（精确或子域后缀，仅 http/https） |
| `browser_goto(session_id, url)` | `POST /session/{id}/goto` | `{"url": 最终URL}`；含重定向落点复检 |
| `browser_click(session_id, selector)` | `…/click` | `{"clicked"}`；支持 CSS / aria `[ref=eN]` 句柄 |
| `browser_type(session_id, selector, text)` | `…/type` | 只回 `typed_len`——文本不回显不进审计（仓红线） |
| `browser_extract(session_id, selector?)` | `…/extract` | `{"text"}`：BP L1 结构化优先（aria_snapshot YAML→markdown），失败降级纯文本 |
| `browser_screenshot(session_id)` | `…/screenshot` | PNG `ImageContent`（BP L3 视觉兜底） |
| `browser_close(session_id)` | `…/close` | 归还销毁；与 open 配对（池上限 8） |
| `browser_search(query, provider?, limit?)` | `POST /search`（线3） | DDG+Bing 兜底链，≤10 条去重；**未并入前如实降级 NOT_DEPLOYED** |
| `browser_fetch(url)` | `POST /fetch`（线3） | 两栈路由抓正文；fetch allowlist fail-closed；同上降级 |

错误纪律：非 2xx 一律 `ToolError` 透传服务端错误体（`ALLOWLIST_VIOLATION` /
`POOL_UNAVAILABLE` / `TIMEOUT` / `ALLOWLIST_VIOLATION_REDIRECT`…），连接不可达
报服务地址；不静默重试、不冒充成功。

## 已部署实况（anolis-gpu-01，2026-09-30 线4 实装）

- 脚本：`/opt/gpumachine/agent-browser/mcp/browser_mcp_server.py`（anuser 属主）
- config：`/opt/gpumachine/jiuwenswarm/home/.jiuwenswarm/config/config.yaml`
  （root 属主；`mcp.servers` 由 `[]` 外科替换为下条目，注释保留；改前备份
  `config.yaml.bak-line4-20260930023622`）

```yaml
mcp:
  servers:
    - name: browser
      enabled: true
      transport: stdio
      command: /opt/gpumachine/jiuwenswarm/venv/bin/python
      args: ["/opt/gpumachine/agent-browser/mcp/browser_mcp_server.py"]
      env:
        AB_BROWSER_URL: "http://100.64.0.7:8125"
```

- 生效：`sudo systemctl restart jiuwenswarm-app`（服务 User=root，
  WorkingDirectory=/opt/gpumachine/jiuwenswarm/home，故读上述 config.yaml）。
  MCP 配置在 team/swarm 组装时消费（`jiuwenswarm/agents/swarm/assembly.py` 经
  `build_mcp_server_configs`），无需额外动作。

## 验证证据（2026-09-30 当日实跑）

1. **stdio 端到端**（jiuwenswarm venv python 以 config 同款命令拉起 + fastmcp
   Client）：`list_tools` 10 个 browser_* 工具；真浏览器链
   open(`["example.com"]`)→goto→extract（L1 aria markdown：
   `- paragraph: This domain is for use in documentation examples…`）→
   screenshot（ImageContent）→close 全通；空 allowlist 负例被拒；
   search/fetch 返回 NOT_DEPLOYED 降级语。公网页次消耗 1（预算 ≤5/线）。
2. **jiuwenswarm 代码路径生效验证**（服务同款函数，非自证）：
   `get_mcp_servers()` → `AgentWebSocketServer._fetch_mcp_tools_from_config()`
   （openjiuwen ToolMgr stdio 真连）→ `Retrieved 10 tools from Stdio server`，
   10/10 命中，`VERDICT: BROWSER_TOOLS_LIVE`。
3. 服务状态：`systemctl restart` 后 `is-active=active`，journal 无 MCP 报错。

未做（如实）：经 jiuwenswarm 真实 agent 会话（LLM 在环）调用 browser 工具的
端到端——本轮模型调用预算为 0；以上验证已覆盖「配置→拉起→list_tools→真浏览器」
全链，差 LLM 会话一跳，属后续工单。

## Higress 判断（不接，依据）

浏览器不是模型流量：工具面在执行面本地（stdio 由 AgentServer 进程拉起），
上游是 GPU 机 tailnet 上的 agent-browser 服务，全链路无一次 LLM API 调用。
Higress 是体系内**唯一模型入口/模型路由唯一决策点**——browser MCP 不构成
第二个模型端点（对照 SYSTEM-GUIDE §5 红线），也不应经模型网关绕行增加一跳。
若未来 browser-use 类 LLM 驱动框架接入（本轮明确不做），其模型流量才需走
Higress consumer。

## 回滚

```bash
# GPU 机 root：
cp -a /opt/gpumachine/jiuwenswarm/home/.jiuwenswarm/config/config.yaml.bak-line4-20260930023622 \
      /opt/gpumachine/jiuwenswarm/home/.jiuwenswarm/config/config.yaml
systemctl restart jiuwenswarm-app
# 脚本目录可留可删：/opt/gpumachine/agent-browser/mcp/（未被 config 引用即惰性）
```

## 依赖与许可

- fastmcp 2.14.7（jiuwenswarm venv 既有，未新装）；HTTP 客户端全 stdlib。
- 本文件不引入 BP 排除项（Playwright MCP 参考其 a11y snapshot 思路，但实现
  完全自有：转调自有 Driver 服务，未引入 vercel agent-browser 等排除项代码）。
