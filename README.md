# agent-browser

原子浏览器能力（atomic-browser）。一句话：**给 agent 的可复用浏览器原子技能：网页访问、信息提取、页面操作。**

## 定位

- 《建设方案-多Agent系统与GitOps》（PROP-0001）第 8 节团队分工中 `atomic-browser` 角色的承载仓库；随 M3"原子能力开源"（WO-0008）交付。
- 与 openJiuwen 原生的边界按第 0 节第 4 条与第 4.9 节处理：原生 `channels/browser` 与 harness 浏览器工具能直接用的直接用；本仓库只沉淀**原子化、可独立复用**的浏览器能力，不做第二个决策点。

## 现状（FakeDriver MVP：不驱动任何真实浏览器）

| 组件 | 说明 |
| --- | --- |
| `src/agent_browser/driver.py` | `BrowserDriver` 协议（goto / click / type / extract / screenshot / close）+ `FakeDriver` 内存实现 + `DriverError` + `is_browser_driver` 结构化检查 |
| `src/agent_browser/task.py` | `BrowserTask`（url / domain_allowlist / max_actions / timeout_s / tenant_id / session_id）+ `run_task`：动作循环、**allowlist 硬拦**（越域 URL → BLOCKED，导航不发生）、**动作数/超时双熔断**、每动作审计事件 |
| `src/agent_browser/sessions.py` | `SessionManager`：session_id → 独立 storage 命名空间 + 独立驱动实例；destroy 即关闭并摘除（不复用，防串会话） |
| `tests/` | pytest 26 例：allowlist（越域/相似域/子域/非 http/重定向后越域）、熔断、审计卫生、会话隔离 |

## 安全语义（确定性系统决定权限）

- **allowlist 硬拦**：fail-closed——`domain_allowlist` 为空直接拒绝启动；host 必须精确或子域命中；goto 的**最终 URL**（真实驱动重定向后）也必须命中；
- **审计卫生**：`type` 动作只记 `value_len`，输入文本不进审计；截图只记字节长度，**本仓不上传任何截图**，字节留在任务进程内存；
- **熔断**：动作数预算（含隐式 start_url 首跳）与超时预算到点即停，事件落审计。

## 用法

```bash
python -m pytest            # 全绿（零第三方依赖，Python 3.9+）

PYTHONPATH=src python -c "
from agent_browser import FakeDriver, PageSpec, BrowserTask, Action, run_task
d = FakeDriver(pages={'https://example.com/': PageSpec(elements=('h1',), texts={'h1': 'Example'})})
task = BrowserTask(start_url='https://example.com/', domain_allowlist=['example.com'],
                   tenant_id='t1', actions=[Action('extract', 'h1')])
print(run_task(d, task))
"
```

## 接入真实驱动（TODO，留缝不留实现）

- playwright 驱动：实现 `BrowserDriver` 协议六方法（`is_browser_driver` 可校验），
  以 `driver_factory(storage)` 形式注入 `SessionManager`；goto 必须返回**最终 URL**
  供 allowlist 复检；
- 真实 cookie 隔离：`sessions.py` 当前以独立 storage 命名空间固化隔离语义，
  playwright 接入时映射到独立 browser context；
- 以上均为 TODO，本仓当前不含任何真实浏览器/网络代码。

## 状态

- ~~M0 骨架（README / LICENSE / .gitignore）~~
- **M3/WO-0008（本次）：协议 + FakeDriver + 任务治理（allowlist/熔断/审计）+ 会话隔离语义 + pytest 全绿；playwright 等真实驱动未接入（仅协议缝）。**

## License

Apache-2.0
