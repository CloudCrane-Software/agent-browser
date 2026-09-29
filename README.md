# agent-browser

原子浏览器能力（atomic-browser）。一句话：**给 agent 的可复用浏览器原子技能：网页访问、信息提取、页面操作。**

## 定位

- 《建设方案-多Agent系统与GitOps》（PROP-0001）第 8 节团队分工中 `atomic-browser` 角色的承载仓库；随 M3"原子能力开源"（WO-0008）交付。
- 与 openJiuwen 原生的边界按第 0 节第 4 条与第 4.9 节处理：原生 `channels/browser` 与 harness 浏览器工具能直接用的直接用；本仓库只沉淀**原子化、可独立复用**的浏览器能力，不做第二个决策点。

## 现状（v0.2：实弹 PlaywrightDriver + 池化 + 无头服务）

| 组件 | 说明 |
| --- | --- |
| `src/agent_browser/driver.py` | `BrowserDriver` 协议（goto / click / type / extract / screenshot / close）+ `FakeDriver` 内存实现 + `DriverError` + `is_browser_driver` 结构化检查 |
| `src/agent_browser/playwright_driver.py` | **实弹驱动**：playwright + chrome-headless-shell（v1.49+ headless 默认即 shell）；BP §2.5 flags 基线（`DEFAULT_LAUNCH_ARGS`，线1 `launch-profile.json` 存在即优先）；extract 走 **L1 结构化优先**（`aria_snapshot()` YAML 树 → markdown，含 `[ref=eN]` 动作句柄；失败降级 `text_content()`）；**懒加载**——本包 import 不连带 playwright，缺包时首个动作抛带安装指引的 `DriverError` |
| `src/agent_browser/sessions.py` | `SessionManager`（FakeDriver storage 命名空间语义）+ **`BrowserPool`**：借出/归还、`max_size` 默认 8（BP §2.5 口径 32G/8C 20–40 并发的保守起步）、每会话独立 `user-data-dir` persistent context 进程级隔离、看门狗按 user-data-dir 标记收割孤儿进程树 + 空闲 TTL 收割、归还时 `context.close()+browser.close()` 双保险 + 进程树兜底杀 + 目录清焚 |
| `src/agent_browser/server.py` | **无头服务**（纯 stdlib）：`GET /health`；`POST /session {allowlist[, url]}`（allowlist 非空硬校验，fail-closed）；`POST /session/{id}/{goto,click,type,extract,screenshot,close}`；默认绑 `100.64.0.7:8125` **tailnet-only**（无 TLS/认证，靠 tailnet 边界，绝不改绑 0.0.0.0）；每会话单线程执行器保 playwright 线程亲和 |
| `src/agent_browser/task.py` | `BrowserTask` + `run_task`：动作循环、**allowlist 硬拦**（含重定向后最终 URL 复检）、**动作数/超时双熔断**、每动作审计事件 |
| `deploy/` | GPU 机（anolis-gpu-01）`agent-browser.service` systemd 单元 + 部署手册（`README-gpumachine.md`：四坑防护清单） |
| `tests/` | pytest 73 例：原 26 例（allowlist/熔断/审计卫生/会话隔离，全数保留）+ 47 例新增（mock playwright 协议层 18、池化/看门狗 12、HTTP 服务栈 17） |

## 安全语义（确定性系统决定权限）

- **allowlist 硬拦**：fail-closed——`domain_allowlist` 为空直接拒绝启动；host 必须精确或子域命中；goto 的**最终 URL**（真实驱动重定向后）也必须命中；server 层同一套 `host_allowed`；
- **审计卫生**：`type` 动作只记 `value_len`，输入文本不进审计、HTTP 响应不回显；截图只记字节长度，**本仓不上传任何截图**，字节留在任务进程内存；
- **熔断**：动作数预算（含隐式 start_url 首跳）与超时预算到点即停，事件落审计；
- **僵尸治理**（BP §2.5 教训：`driver.close()` 只关标签不杀进程）：驱动三层双保险关闭 → 池看门狗孤儿收割（ppid==1 且 cmdline 含本池 context 根）→ 归还时进程树兜底杀 + user-data-dir 用完即焚；服务级 `KillMode=mixed` 兜底。

## 用法

```bash
python -m pytest            # 73 例全绿（核心零第三方依赖，Python 3.9+；playwright 可选）

# FakeDriver（零依赖演示）
PYTHONPATH=src python -c "
from agent_browser import FakeDriver, PageSpec, BrowserTask, Action, run_task
d = FakeDriver(pages={'https://example.com/': PageSpec(elements=('h1',), texts={'h1': 'Example'})})
task = BrowserTask(start_url='https://example.com/', domain_allowlist=['example.com'],
                   tenant_id='t1', actions=[Action('extract', 'h1')])
print(run_task(d, task))
"

# 实弹驱动（需先装：pip install 'playwright>=1.49' && playwright install chromium-headless-shell）
PYTHONPATH=src python -c "
from agent_browser import PlaywrightDriver, BrowserTask, Action, run_task
d = PlaywrightDriver()   # launch 参数：线1 launch-profile.json 优先，缺省=BP §2.5 基线
task = BrowserTask(start_url='https://example.com/', domain_allowlist=['example.com'],
                   tenant_id='t1', actions=[Action('extract', 'body')])
print(run_task(d, task).data)
"

# 无头服务（GPU 机，tailnet-only）
python -m agent_browser.server        # 绑 100.64.0.7:8125；env 见 deploy/agent-browser.service
```

## 已知边界（如实）

- playwright 为**可选依赖**（lazy import），CI 与 FakeDriver 路径零依赖；
- `[ref=eN]` → `aria-ref=eN` 选择器映射的引擎侧语法以部署期实测为准 [待]；
- `--no-sandbox` 运行（BP §2.5 容器未建时的基线）；seccomp 白名单保 Chrome 沙箱、容器化属后续线 [待]；
- 线1 `launch-profile.json` 未交付时默认即 BP flags 硬编码基线（模块内已留查找路径）。

## 状态

- ~~M0 骨架~~；~~M3/WO-0008：协议 + FakeDriver + 任务治理 + 会话隔离~~
- **线2（本次）：实弹 PlaywrightDriver（L1 extract / BP flags / 懒加载）+ BrowserPool（user-data-dir 隔离 / 双保险关闭 / 看门狗孤儿收割）+ 无头服务（tailnet-only）+ systemd 部署件 + 73 例 pytest 全绿。**

## License

Apache-2.0
