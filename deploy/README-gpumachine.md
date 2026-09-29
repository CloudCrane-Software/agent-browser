# GPU 机（anolis-gpu-01）部署手册 — agent-browser 无头服务

> 蓝图：BP（FINAL_agent_browser_blueprint.md）路线 D 首选形态——**pip venv 装
> playwright + chrome-headless-shell**（比容器更轻；容器化 [待] 后续线）。
> 目标机：tailnet `100.64.0.7`，用户 `anuser`（sudo 组）。

## 0. 落位

| 路径 | 用途 |
| --- | --- |
| `/opt/gpumachine/agent-browser/venv` | 独立 venv（不污染 jiuwenswarm venv） |
| `/opt/gpumachine/agent-browser/app` | 本仓代码（`src/agent_browser`） |
| `/opt/gpumachine/agent-browser/contexts` | 池的每会话 user-data-dir 根（用完即焚） |
| `/opt/gpumachine/agent-browser/launch-profile.json` | 线1 交付物落点（存在即优先于 BP 默认 flags） |
| `/etc/systemd/system/agent-browser.service` | 服务单元（本仓 `deploy/agent-browser.service`） |

## 1. 安装序列（GPU 机上执行）

```bash
# ① venv + playwright（pip 源不通时加清华镜像 -i https://pypi.tuna.tsinghua.edu.cn/simple）
python3 -m venv /opt/gpumachine/agent-browser/venv
/opt/gpumachine/agent-browser/venv/bin/pip install 'playwright>=1.49'
# ② chrome-headless-shell（playwright v1.49+ headless 默认即它，BP §2.1）
/opt/gpumachine/agent-browser/venv/bin/playwright install chromium-headless-shell
#   下载慢：PLAYWRIGHT_DOWNLOAD_HOST=https://cdn.npmmirror.com/binaries/playwright 重试
# ③ 四坑防护（BP §4.1）——按下表逐项装齐并留证
```

四坑核对表（安装后逐项 `rpm -q` 留证）：

| 坑 | 处置 | 验证 |
| --- | --- | --- |
| CJK 字体（豆腐块） | `dnf install google-noto-sans-cjk-ttc-fonts fontconfig` + `fc-cache -f` 预热 | `fc-list :lang=zh | head -1` |
| /dev/shm 64MB | flags 基线已带 `--disable-dev-shm-usage`（DEFAULT_LAUNCH_ARGS） | 无需另配 |
| NSS/证书链 | `dnf install ca-certificates nss nspr` + `update-ca-trust` | `curl -sI https://example.com` |
| ANCK userns / Chrome 沙箱 | 本部署 `--no-sandbox`（容器边界未建，BP §2.5 基线默认） | [待] seccomp 方案后回补 |

## 2. 服务

```bash
sudo cp deploy/agent-browser.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now agent-browser
journalctl -u agent-browser -n 20 --no-pager      # 看到 "serving on http://100.64.0.7:8125"
```

## 3. 验收（tailnet 内任一机器）

```bash
curl -s http://100.64.0.7:8125/health | python3 -m json.tool
SID=$(curl -s -X POST http://100.64.0.7:8125/session \
  -d '{"allowlist": ["example.com"]}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["session_id"])')
curl -s -X POST http://100.64.0.7:8125/session/$SID/goto -d '{"url": "https://example.com/"}'
curl -s -X POST http://100.64.0.7:8125/session/$SID/extract -d '{}'   # → Example Domain
curl -s -X POST http://100.64.0.7:8125/session/$SID/close -d '{}'
```

公网站点预算：验收全程 ≤5 页次（本轮实际 1 页次=example.com）。

## 4. 已知边界（如实）

- 无 TLS/认证：**仅 tailnet 边界**——服务硬绑 `100.64.0.7`，改动绑定即改 systemd 单元（元资产纪律：改动走 PR）。
- `--no-sandbox`：seccomp 白名单保 Chrome 沙箱是 BP 规模化层内容，本轮未做 [待]。
- aria-ref 句柄（`[ref=eN]` → `aria-ref=eN`）的引擎侧语法以部署期实测为准 [待]。
