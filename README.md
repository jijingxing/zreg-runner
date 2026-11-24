# zreg-actions

z.ai 批量注册 + 激活管线，**GitHub Actions / 任意 Linux 同一份代码**。
免费代理池承担全部 z.ai 流量（滑块 + API），配额到顶换代理（秒级），不再重启路由。

## 结构

```
proxy_pool.py              免费代理抓取+验活（能通 chat.z.ai 才留）
actions_reg.py             主循环：Xvfb+chrome(--proxy-server)+dragd 自管
├─ reg_loop.py             单轮：开题→洞检测(blur-NCC)→human_track 轨迹拖→T001→signup
├─ dragd2.py               CDP 桥（HTTP 19333：/eval /tabs /dragto3）
│   └─ captcha_lab_human_track.py   feedback 变体人类轨迹
├─ zreg_min/               激活最小集（stdlib only）
│   ├─ mailbox.py          CloudMail 客户端（catch-all 全局桶）
│   └─ http_box.py         http_json + oauth_http（走当前代理）
└─ 产物: accounts.jsonl + zreg_state_out/ + cred_out/ (+ collector 推送)
```

## 本地跑（盒子/任意机）

```bash
pip install websockets numpy pillow
python3 proxy_pool.py --n 20          # 先建代理池
python3 actions_reg.py --target 20    # 注册+当场激活 20 个
```

## GitHub Actions

1. 建 **private** 仓库，推本目录。
2. VPS 起 collector（`zreg_collector.py` + nginx TLS），拿到 URL 和 token。
3. Repo Settings → Secrets：
   - `COLLECTOR_URL` = `https://<your-collector-host>/`
   - `COLLECTOR_TOKEN` = collector 的 token
4. Actions → register → Run workflow，填 target。

## 配额模型（实测 2026-11-22）

- `signup:ip`（z.ai 429）：~12-20 个/14h/IP，signup POST 与 finish_signup 共用 →
  每代理约 16 个号自动轮换（`signups_this_proxy>=16` 或 405/429/F0xx 连墙即换）
- WAF 405（阿里层）：同上触发轮换
- 滑块通过率 ~40%（err 集中 0.2-1.5px），失败只是浪费一轮，不影响账号
- 无效 token（~20%）：resend_email 自愈，重落库约 1 分钟，pending 下轮重试

## 注意

- 代理是明文 HTTP 代理：z.ai 全程 HTTPS，代理只见 CONNECT，凭据安全。
- 免费代理质量波动大；池子 2h 过期自动重抓。
- F0xx streak=6 / 基础错误 streak=3 都会触发换代理或重启栈，无需人工。
