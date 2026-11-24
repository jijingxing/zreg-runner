#!/usr/bin/env python3
"""captcha-dragd v2 — 在本机（用户 Linux 盒子）用本地 CDP 发真人节奏鼠标事件。

v2 新增 /dragto：闭环拖拽。
  阿里云滑块拖动时，拼图块并不是 1:1 跟随指针（实测 ~0.6，且随速度变），
  所以先用增益估计 + 末端微调收敛到目标像素，松手前位置必须已经对准。

接口：
  POST /drag    {sx,sy,dx[,dur_ms]}           —— 开环拖 dx 像素（保留）
  POST /dragto  {sx,sy,target_x[,dur_ms]}     —— 闭环，把拼图块左边缘拖到 target_x（页面坐标）
  POST /state   {}                            —— 读当前滑块/拼图/弹窗状态
  POST /eval    {expr}                        —— 在页面里执行 JS
"""
import asyncio
import json
import math
import random
import statistics
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import websockets

sys.path.insert(0, '/home/jijingxing')
from captcha_lab_human_track import generate_drag

import os
CDP_LIST = os.environ.get('DRAGD_CDP', 'http://127.0.0.1:9222/json/list')

# ---- 二次位移响应模型（sliderlab 实测，标定见 calib_cdp.py）----
# piece.left = A*m + B*m²，m=指针位移
A_CDP, B_CDP = 0.0769, 0.003552

VERDICTS = []  # (code, passed) 最近判定码，由 verify watcher 收集

PICK_JS = """JSON.stringify((() => {
  const pick=(id)=>{let b=null,a=0;for(const el of document.querySelectorAll('#'+id)){
    const r=el.getBoundingClientRect();
    if(r.width>5&&r.height>5&&r.width*r.height>a){a=r.width*r.height;b=el;}}return b;};
  const win=document.querySelector('#aliyunCaptcha-window-float');
  const tb=document.querySelector('#aliyunCaptcha-captcha-text-box');
  const f=e=>{if(!e)return null;const r=e.getBoundingClientRect();
    return {x:+r.x.toFixed(2),y:+r.y.toFixed(2),w:+r.width.toFixed(1),h:+r.height.toFixed(1)};};
  return {show:!!(win&&win.classList.contains('window-show')),
          verified:!!(tb&&tb.classList.contains('verified')),
          slider:f(pick('aliyunCaptcha-sliding-slider')),
          piece:f(pick('aliyunCaptcha-puzzle')),
          bg:f(pick('aliyunCaptcha-img'))};
})())"""


def page_ws():
    lst = json.loads(urllib.request.urlopen(CDP_LIST, timeout=5).read())
    pages = [x for x in lst if x.get('type') == 'page'
             and ('chat.z.ai' in x.get('url', '') or 'zclaim' in x.get('url', ''))]
    if not pages:
        return None
    # 领计划页存在时优先（claim 在两轮注册之间串行执行，用完即关）
    t = (next((x for x in pages if 'zclaim' in x.get('url', '')), None)
         or next((x for x in pages if 'fuckzaicn' in x.get('url', '')), pages[0]))
    return t['webSocketDebuggerUrl'] if t else None


# Start Plan 领取页：cn 区 11xygtvd widget（官方 client/configs 下发的 claim 场景）。
# bizResult:false → SDK 自动出新题，同一 tab 可连刷多个 token。
ZCLAIM_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>zclaim</title>
<style>body{margin:0;background:#161a24;display:flex;flex-direction:column;align-items:center;justify-content:center;height:100vh;gap:10px}
#captcha-element{width:376px;min-height:56px;background:#fff;border-radius:8px}
#go{width:376px;height:40px;border:0;border-radius:8px;background:#5b6cff;color:#fff}</style></head><body>
<div id="captcha-element"></div><button id="go">start</button>
<script>
window.__tokens=[];window.__err='';
const s=document.createElement('script');
s.src='https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js';
s.onerror=()=>{window.__err='script-load-fail'};
s.onload=()=>window.initAliyunCaptcha({SceneId:'11xygtvd',region:'cn',prefix:'no8xfe',mode:'embed',
 element:'#captcha-element',button:'#go',language:'en',slideStyle:{width:360,height:40},
 captchaVerifyCallback:async(p)=>{window.__tokens.push(p);return {captchaResult:true,bizResult:false};},
 onError:(e)=>{window.__err=String(e&&e.message||e)},getInstance:(i)=>{window.__inst=i}});
document.head.appendChild(s);
</script></body></html>"""

def browser_ws():
    """浏览器级 WS（/json/version），无页面时也能 Target.createTarget。"""
    v = json.loads(urllib.request.urlopen(
        CDP_LIST.replace('/json/list', '/json/version'), timeout=5).read())
    return v.get('webSocketDebuggerUrl')


def main_srv():
    port = int(os.environ.get('DRAGD_PORT', '19222'))
    srv = ThreadingHTTPServer(('127.0.0.1', port), H)
    print(f'dragd v2 on 127.0.0.1:{port} cdp={CDP_LIST}', flush=True)
    srv.serve_forever()


class Conn:
    """单连接 + 请求/响应配对（输入事件 fire-and-forget，读取要等回复）。"""

    def __init__(self, ws):
        self.ws = ws
        self.n = 0

    async def send(self, method, params=None):
        self.n += 1
        i = self.n
        await self.ws.send(json.dumps({'id': i, 'method': method, 'params': params or {}}))
        return i

    async def fire(self, method, params=None):
        self.n += 1
        await self.ws.send(json.dumps({'id': self.n, 'method': method, 'params': params or {}}))

    async def wait(self, i, timeout=10):
        dl = time.time() + timeout
        while time.time() < dl:
            m = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=max(0.5, dl - time.time())))
            if m.get('id') == i:
                return m
        raise TimeoutError('cdp wait')

    async def eval(self, expr):
        i = await self.send('Runtime.evaluate',
                            {'expression': expr, 'return_by_value': True, 'awaitPromise': True})
        m = await self.wait(i)
        return (m.get('result') or {}).get('result', {}).get('value')

    async def state(self):
        v = await self.eval(PICK_JS)
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:
                return None
        return v

    async def mouse(self, type_, x, y, button='none', buttons=0, click=0):
        p = {'type': type_, 'x': round(x, 2), 'y': round(y, 2), 'button': button,
             'buttons': buttons, 'clickCount': click}
        await self.fire('Input.dispatchMouseEvent', p)


def plan_offsets_from(remain, from_off, dur_left, t_offset):
    """从 from_off 继续，在 dur_left 内再走 remain。前快后慢。"""
    pts = []
    t = t_offset
    end = t_offset + dur_left
    cur = from_off
    while t < end:
        p = (t - t_offset) / max(dur_left, 1)
        target = from_off + remain * (1 - (1 - p) ** 1.8)
        cur = target
        y = (random.random() - 0.5) * 1.4
        pts.append((t, cur, y))
        t += random.uniform(10, 15)
    pts.append((end, from_off + remain, 0.0))
    return pts


def plan_offsets(total, dur_ms):
    """生成 (t_ms, piece_offset, y_offset) 计划 —— 拟合真人 T001 样本：
    前 10% 时间走 ~70% 位移，但峰值速度压在 1050px/s 内；之后细步单调逼近；
    中途 1-2 次瞄准停顿（250-500ms，位移几乎为 0 但有 1-2px 抖动）；
    绝不超冲（offset 单调不减）。"""
    pts = []
    t = 0.0
    dur_s = dur_ms / 1000.0
    # 主冲刺段：前 55% 的时间走 72% 的位移，速度上限 1000px/s
    burst_ms = dur_ms * 0.55
    burst_px = total * 0.72
    v_max = 1000.0
    # 冲刺内部再分两小段，避免起步速度尖峰
    cur = 0.0
    while t < burst_ms:
        p = t / burst_ms
        target = burst_px * (1 - (1 - p) ** 1.9)
        step = target - cur
        # 限速
        max_step = v_max * 0.012
        if step > max_step:
            step = max_step
        cur += step
        y = (random.random() - 0.5) * 1.6
        pts.append((t, cur, y))
        t += random.uniform(10, 15)
    # 瞄准停顿 1
    pause1 = random.uniform(230, 420)
    pt_end = t + pause1
    while t < pt_end:
        cur += random.choice([-0.6, 0.4, 0.7]) * random.random()
        pts.append((t, cur, (random.random() - 0.5) * 1.2))
        t += random.uniform(30, 70)
    # 细步段：剩下 28% 位移用 5-14px 小步走完，间隔 60-130ms
    remain = total - cur
    n = max(3, int(remain / 9))
    per = remain / n
    for k in range(n):
        t += random.uniform(60, 130)
        cur += per * random.uniform(0.85, 1.12)
        pts.append((t, min(cur, total), (random.random() - 0.5) * 1.4))
    # 瞄准停顿 2（松手前）
    pause2 = random.uniform(260, 520)
    pt_end = t + pause2
    while t < pt_end:
        pts.append((t, cur + (random.random() - 0.5) * 0.8, (random.random() - 0.5) * 1.0))
        t += random.uniform(40, 90)
    # 收尾
    pts.append((t + 30, total, 0.0))
    return pts


async def drag_to(ws, sx, sy, target_x, dur_ms=None, calib=None):
    """开环+单次标定：g=条位移/指针位移（每题标定一次），主拖 pointer=target/g。
    轨迹 T001 形状 + 连续蠕动收尾。calib=dict(g=...) 由调用方缓存复用。"""
    c = Conn(ws)
    st = await c.state()
    if not st or not st.get('show') or not st.get('slider') or not st.get('piece') or not st.get('bg'):
        return {'ok': False, 'err': 'widget-not-ready', 'state': st}
    bg_x = st['bg']['x']
    for _ in range(12):
        off0 = st['piece']['x'] - bg_x
        if abs(off0) < 3: break
        await asyncio.sleep(0.2)
        st = await c.state()
        if not st or not st.get('piece'):
            return {'ok': False, 'err': 'strip-not-reset'}
    off0 = st['piece']['x'] - bg_x
    need = (target_x - bg_x) - off0          # 条需要走的距离

    g = (calib or {}).get('g', 0.99)   # 条/指针稳态增益（实测默认）
    if not (0.25 < g < 1.2): g = 0.99
    if calib is not None: calib['g'] = g

    # —— 主拖 ——
    off_target = max(10.0, min(250.0, need / g))
    if dur_ms is None:
        dur_ms = int(min(4200, max(1300, off_target * 15.5 + random.uniform(300, 600))))

    # 自然接近漫游
    roam_start = (sx - random.uniform(180, 420), sy - random.uniform(120, 260))
    steps = random.randint(28, 44)
    for k in range(steps):
        p_ = (k + 1) / steps
        rx = roam_start[0] + (sx - roam_start[0]) * p_ + random.uniform(-14, 14) * (1 - p_)
        ry = roam_start[1] + (sy - roam_start[1]) * p_ + random.uniform(-10, 10) * (1 - p_)
        await c.mouse('mouseMoved', rx, ry)
        await asyncio.sleep(random.uniform(0.02, 0.075))
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await c.mouse('mouseMoved', sx + random.uniform(-2.5, 2.5), sy + random.uniform(-2.5, 2.5))
    await asyncio.sleep(random.uniform(0.16, 0.34))
    await c.mouse('mousePressed', sx, sy, 'left', 1, 1)
    await asyncio.sleep(random.uniform(0.09, 0.18))

    pts = plan_offsets(off_target, dur_ms)
    gaps, last, t0 = [], time.time(), time.time()
    sent = 0
    g_meas = None
    settle_err_history = []
    probe_idx = int(len(pts) * 0.55)     # 55% 处在线校准
    for idx, (t_ms, p_off, dy) in enumerate(pts):
        now = (time.time() - t0) * 1000
        if t_ms > now:
            await asyncio.sleep((t_ms - now) / 1000)
        await c.mouse('mouseMoved', sx + p_off, sy + dy, 'none', 1)
        sent += 1
        n2 = time.time()
        gaps.append(round((n2 - last) * 1000, 1))
        last = n2
        # —— 在线校准：55% 处读条，量真实增益，重排剩余轨迹 ——
        if idx == probe_idx:
            await asyncio.sleep(0.05)
            cur = await c.state()
            if cur and cur.get('piece') and cur.get('bg'):
                strip_moved = (cur['piece']['x'] - cur['bg']['x']) - off0
                if p_off > 25 and strip_moved > 10:
                    g_meas = strip_moved / p_off
                    if 0.4 < g_meas < 1.3:
                        g = g_meas
                        # 剩余目标修正
                        rem_strip = (target_x - bg_x) - (off0 + strip_moved)
                        rem_ptr = rem_strip / g
                        new_total = p_off + rem_ptr
                        pts = pts[:idx+1] + plan_offsets_from(new_total - p_off, p_off, dur_ms - t_ms, t_ms)
                        off_target = new_total

    # 连续蠕动收尾：误差≤1px 停；大误差用"前瞻追赶"突破低通死区
    cur_off = off_target
    corrections = 0
    settle_err = None
    hit_end = False
    trace = []
    t_end = time.time() + 6.0
    last_read = 0.0
    while time.time() < t_end:
        await asyncio.sleep(random.uniform(0.07, 0.11))
        err = None
        if time.time() - last_read > 0.18:
            cur = await c.state()
            last_read = time.time()
            if cur and cur.get('piece') and cur.get('bg'):
                strip_left = cur['piece']['x'] - cur['bg']['x']
                err = (target_x - cur['bg']['x']) - strip_left
                settle_err = err
                settle_err_history.append(err)
                trace.append((round(time.time() % 1000, 2), round(cur_off, 1),
                              round(strip_left, 1), round(err, 1)))
                if abs(err) <= 1.0: break
                if cur_off >= 252 and err > 2:
                    hit_end = True; break
        step = random.uniform(0.6, 1.6)
        if err is not None:
            if err > 8:
                # 前瞻追赶：指针冲过目标 err/g + 5~9px，突破滤波死区
                lead_off = (cur_off + err / g + random.uniform(5, 9))
                step = min(lead_off - cur_off, random.uniform(6, 14))
            elif err > 6:
                step = random.uniform(1.2, 2.4)        # 还差得远，蠕快些
            elif err < -0.5:
                step = -min(random.uniform(0.6, 1.4), abs(err) * 0.5)  # 过冲回收
            elif abs(err) < 4:
                step *= 0.6
        cur_off = max(0.0, min(256.0, cur_off + step))
        await c.mouse('mouseMoved', sx + cur_off, sy + random.uniform(-0.8, 0.8), 'none', 1)
        corrections += 1

    await asyncio.sleep(random.uniform(0.22, 0.5))
    await c.mouse('mouseReleased', sx + cur_off, sy + random.uniform(-1, 1), 'left', 0, 1)
    for k in range(random.randint(6, 12)):
        await c.mouse('mouseMoved', sx + cur_off + random.uniform(8, 30) * k / 3,
                      sy + random.uniform(-40, -10) - k * random.uniform(1, 6))
        await asyncio.sleep(random.uniform(0.03, 0.09))
    final = None
    try:
        st2 = await c.state()
        final = (st2 or {}).get('piece', {}).get('x') if st2 else None
    except Exception:
        pass
    import statistics as _st
    pacing = {'n': len(gaps), 'med': _st.median(gaps) if gaps else 0,
              'p90': sorted(gaps)[int(len(gaps) * 0.9)] if gaps else 0,
              'max': max(gaps) if gaps else 0}
    return {'ok': True, 'g': round(g, 4), 'events': sent, 'dur': dur_ms,
            'off_target': round(off_target, 1), 'target': round(target_x, 2),
            'final_piece': round(final, 2) if final else None,
            'settle_err': round(settle_err, 2) if settle_err is not None else None,
            'corrections': corrections, 'pointer_off': round(cur_off, 1),
            'hit_end': hit_end, 'pacing': pacing,
            'trace': trace[-12:]}


async def drag_to3(ws, sx, sy, target_x):
    """sliderlab 方案：二次反解 + feedback 人形轨迹 + 不回正松手 + 判定码拦截。"""
    c = Conn(ws)
    st = await c.state()
    if not st or not st.get('show') or not st.get('slider') or not st.get('piece') or not st.get('bg'):
        return {'ok': False, 'err': 'widget-not-ready', 'state': st}
    bg_x = st['bg']['x']
    for _ in range(12):
        off0 = st['piece']['x'] - bg_x
        if abs(off0) < 3:
            break
        await asyncio.sleep(0.2)
        st = await c.state()
        if not st or not st.get('piece'):
            return {'ok': False, 'err': 'strip-not-reset'}
    off0 = st['piece']['x'] - bg_x
    need = (target_x - bg_x) - off0          # 条需走的距离 L（页面 px）

    # 二次反解：L = A*m + B*m²  =>  m = (-A + sqrt(A²+4BL)) / 2B
    disc = A_CDP ** 2 + 4 * B_CDP * max(need, 1.0)
    if disc < 0:
        return {'ok': False, 'err': 'bad-target', 'need': need}
    D = (-A_CDP + math.sqrt(disc)) / (2 * B_CDP)

    # z.ai 场景容差紧（+1.3px 过冲即 F015），不故意偏，精确对准
    bias_px = 0.0
    err_piece = 0.0

    track = generate_drag(D, bias_px=bias_px, variant="feedback")
    dur_ms = sum(t[2] for t in track)

    # 自然接近漫游（保留我们的：真人拖前有大幅游走）
    roam_start = (sx - random.uniform(180, 420), sy - random.uniform(120, 260))
    steps = random.randint(16, 26)
    for k in range(steps):
        p_ = (k + 1) / steps
        rx = roam_start[0] + (sx - roam_start[0]) * p_ + random.uniform(-14, 14) * (1 - p_)
        ry = roam_start[1] + (sy - roam_start[1]) * p_ + random.uniform(-10, 10) * (1 - p_)
        await c.mouse('mouseMoved', rx, ry)
        await asyncio.sleep(random.uniform(0.015, 0.05))
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await c.mouse('mouseMoved', sx + random.uniform(-2.5, 2.5), sy + random.uniform(-2.5, 2.5))
    await asyncio.sleep(random.uniform(0.16, 0.34))
    await c.mouse('mousePressed', sx, sy, 'left', 1, 1)
    await asyncio.sleep(random.uniform(0.05, 0.11))

    # 轨迹注入（y 累计，防丢形态）+ 中段两点实测标定本站 A/B
    # 拼图块的 left 随指针走是 transition 动画，低通滞后；在匀速段两个
    # 时点各读一次 piece.left / 指针 cum，用增量比估计瞬时斜率。
    t0 = time.perf_counter()
    sched = 0.0
    cum = 0.0
    cum_dy = 0.0
    n_events = 0
    calib_pts = []          # (cum, piece_left) 标定点
    probe_marks = (0.30, 0.55)   # 主段位移进行中时标定（feedback 轨迹前 55% 走完大半）
    n_total = len(track)
    for idx, (dx, dy, dt_ms) in enumerate(track):
        sched += dt_ms
        cum += dx
        cum_dy += dy
        await c.mouse('mouseMoved', sx + cum, sy + cum_dy, 'none', 1)
        n_events += 1
        el = (time.perf_counter() - t0) * 1000
        if sched > el:
            await asyncio.sleep((sched - el) / 1000)
        frac = (idx + 1) / n_total
        for pm in probe_marks:
            if abs(frac - pm) < 0.004 and not any(p[2] == pm for p in calib_pts):
                await asyncio.sleep(0.06)   # 等 transition 追一点
                stc = await c.state()
                if stc and stc.get('piece'):
                    calib_pts.append((cum, stc['piece']['x'] - bg_x, pm))

    # —— 残差补偿：每帧回读驱动，斜率在线更新 ——
    # feedback 轨迹末端是甩鞭+回拉，主段读数滞后大。统一策略：
    # 静置后回读，只做小步慢推；若已过冲（piece > need）绝不前推。
    if calib_pts:
        last_cum, last_pl, _ = calib_pts[-1]
        if len(calib_pts) >= 2 and abs(calib_pts[-1][0] - calib_pts[0][0]) > 3.0:
            c1, p1, _ = calib_pts[0]
            c2, p2, _ = calib_pts[-1]
            k_inst = (p2 - p1) / (c2 - c1)
        else:
            k_inst = last_pl / max(last_cum, 1e-6)
        k_inst = max(0.3, min(1.6, k_inst))

    # 先静置让甩鞭的 transition 追平，再决定补多少
    await asyncio.sleep(random.uniform(0.10, 0.20))
    for i in range(6):
        stc = await c.state()
        if not (stc and stc.get('piece')):
            break
        cur_pl = stc['piece']['x'] - bg_x
        rem = need - cur_pl - 1.8      # 故意欠冲 1.5-2.5px：实测欠冲宽容，过冲即死
        if abs(rem) <= 0.3:
            break
        if rem < -0.3:
            # 已过冲：只能原等待（transition 可能回不来），不再前推，break
            break
        step = rem / max(k_inst, 0.25) * 0.8
        step = max(-3.0, min(3.0, step))   # 小步钳死，绝不跟随甩鞭量级
        if abs(step) < 0.12:
            step = 0.12
        cum += step + random.uniform(-0.2, 0.2)
        cum_dy += random.uniform(-0.4, 0.4)
        await c.mouse('mouseMoved', sx + cum, sy + cum_dy, 'none', 1)
        n_events += 1
        await asyncio.sleep(random.uniform(0.03, 0.06))

    # 二次静置：欠冲 >2.5px 时最多补一次到 -1.2~-2 区间（目标欠冲 1.5±0.3）
    await asyncio.sleep(random.uniform(0.08, 0.15))
    st2 = await c.state()
    if st2 and st2.get('piece'):
        cur_pl = st2['piece']['x'] - bg_x
        rem = need - cur_pl - 1.6
        if 0.3 < rem < 6.0:
            cum += rem / max(k_inst, 0.25) * 0.7 + random.uniform(-0.12, 0.12)
            cum_dy += random.uniform(-0.3, 0.3)
            await c.mouse('mouseMoved', sx + cum, sy + cum_dy, 'none', 1)
            n_events += 1
            await asyncio.sleep(random.uniform(0.12, 0.2))
    st2 = await c.state()
    final_left = (st2['piece']['x'] - bg_x) if st2 and st2.get('piece') else None
    err = need - (final_left or 0)
    await asyncio.sleep(random.uniform(0.01, 0.04))
    await c.mouse('mouseReleased', sx + cum, sy + cum_dy, 'left', 0, 1)

    # 松手后漫游（手臂惯性）
    for k in range(random.randint(4, 7)):
        await c.mouse('mouseMoved', sx + cum + random.uniform(8, 30) * k / 3,
                      sy + cum_dy + random.uniform(-40, -10) - k * random.uniform(1, 6))
        await asyncio.sleep(random.uniform(0.02, 0.06))

    return {'ok': True, 'D': round(D, 1), 'bias_px': round(bias_px, 2),
            'err_piece_target': round(err_piece, 2), 'final_err': round(err, 2) if err is not None else None,
            'events': n_events, 'dur_ms': int(dur_ms),
            'final_piece': round(final_left, 2) if final_left is not None else None,
            'need': round(need, 2), 'target': round(target_x, 2),
            'calib_pts': [(round(c_, 1), round(p_, 2)) for c_, p_, _ in calib_pts]}


async def watch_verdicts(ws_url, deadline):
    """第二条 ws 连接监听 Network.responseReceived，抓 -verify 响应判定码。"""
    try:
        async with websockets.connect(ws_url, max_size=20_000_000) as ws:
            await ws.send(json.dumps({'id': 1, 'method': 'Network.enable', 'params': {}}))
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.3, deadline - time.time()))
                except asyncio.TimeoutError:
                    break
                m = json.loads(raw)
                if m.get('method') != 'Network.responseReceived':
                    continue
                resp = (m.get('params') or {}).get('response') or {}
                url = resp.get('url', '')
                if '-verify.' not in url:
                    continue
                # 发 Resource.loadContents 拿正文（简化：直接 fetch）
                rid = (m['params']).get('requestId')
                await ws.send(json.dumps({'id': 999, 'method': 'Network.getResponseBody',
                                          'params': {'requestId': rid}}))
                # 回复异步到达，宽松收：下一帧找 id=999
                while time.time() < deadline:
                    try:
                        raw2 = await asyncio.wait_for(ws.recv(), timeout=3)
                    except asyncio.TimeoutError:
                        break
                    m2 = json.loads(raw2)
                    if m2.get('id') == 999:
                        body = (m2.get('result') or {}).get('body', '')
                        try:
                            data = json.loads(body)
                            code = ((data.get('Result') or {}).get('VerifyCode')
                                    if isinstance(data.get('Result'), dict) else '') or data.get('result') or ''
                            code = (code or '').upper()
                            if code:
                                VERDICTS.append({'code': code, 'ok': code == 'T001', 't': time.time()})
                        except Exception:
                            pass
                        break
    except Exception as e:
        VERDICTS.append({'code': 'WATCH_ERR', 'ok': False, 'err': repr(e)[:120], 't': time.time()})


async def drag_open(ws, sx, sy, dx, dur_ms=None):
    """开环版本（保留）：指针拖 dx 像素。"""
    c = Conn(ws)
    if dur_ms is None:
        dur_ms = random.randint(950, 1350)
    await c.mouse('mouseMoved', sx + random.uniform(-2, 2), sy + random.uniform(-2, 2))
    await asyncio.sleep(random.uniform(0.08, 0.18))
    await c.mouse('mousePressed', sx, sy, 'left', 1, 1)
    await asyncio.sleep(random.uniform(0.08, 0.2))
    pts = plan_offsets(dx, dur_ms)
    gaps, last, t0 = [], time.time(), time.time()
    for (t_ms, p_off, dy) in pts:
        now = (time.time() - t0) * 1000
        if t_ms > now:
            await asyncio.sleep((t_ms - now) / 1000)
        await c.mouse('mouseMoved', sx + p_off, sy + dy, 'none', 1)
        n2 = time.time()
        gaps.append(round((n2 - last) * 1000, 1))
        last = n2
    cur = dx
    for _ in range(random.randint(1, 3)):
        cur += random.choice([-1, 1]) * random.uniform(0.8, 2.5)
        await asyncio.sleep(random.uniform(0.03, 0.09))
        await c.mouse('mouseMoved', sx + cur, sy + random.uniform(-1.5, 1.5), 'none', 1)
    await asyncio.sleep(random.uniform(0.25, 0.6))
    await c.mouse('mouseReleased', sx + cur, sy + random.uniform(-1, 1), 'left', 0, 1)
    pacing = {'n': len(gaps), 'med': statistics.median(gaps) if gaps else 0,
              'p90': sorted(gaps)[int(len(gaps) * 0.9)] if gaps else 0,
              'max': max(gaps) if gaps else 0}
    return {'ok': True, 'events': len(pts), 'dur': round(dur_ms), 'final_dx': round(cur, 1),
            'pacing': pacing}


def run(coro):
    return asyncio.run(coro)


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.split('?')[0].rstrip('/').endswith('zclaim.html'):
            b = ZCLAIM_PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(b)))
            self.end_headers()
            self.wfile.write(b)
            return
        self._json(200, {'ok': True, 'pid': __import__('os').getpid()})

    def _ret(self, code, obj, tag=''):
        print(f'[req] {self.path.split("?")[0]} {tag} -> {code} '
              f'{repr(obj.get("err", ""))[:80]}', flush=True)
        self._json(code, obj)

    def do_POST(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(n) or b'{}')
        except Exception as e:
            return self._ret(400, {'ok': False, 'err': f'bad-json {e}'}, 'bad-json')
        path = self.path.split('?')[0]
        # /tabs list 纯 HTTP 读 CDP，不要求 chat.z.ai 页面已加载（就绪探测依赖它）
        if path == '/tabs' and body.get('action', 'list') == 'list':
            try:
                lst = json.loads(urllib.request.urlopen(CDP_LIST, timeout=5).read())
                pages = [x for x in lst if x.get('type') == 'page']
                return self._ret(200, {'ok': True, 'tabs': [
                    {'id': p['id'], 'url': p.get('url', '')} for p in pages]}, 'tabs-list')
            except Exception as e:
                return self._ret(500, {'ok': False, 'err': repr(e)[:120]}, 'tabs-list')
        ws_url = page_ws() or (browser_ws() if path == '/tabs' else None)
        if not ws_url:
            return self._ret(503, {'ok': False, 'err': 'no-chat.z.ai-page'}, 'no-page')

        async def go():
            async with websockets.connect(ws_url, max_size=20_000_000) as ws:
                if path == '/shot':
                    # 区域截图（PNG base64）：调试新题型用
                    c = Conn(ws)
                    i = await c.send('Page.captureScreenshot', {'format': 'png', 'clip': {
                        'x': float(body.get('x', 0)), 'y': float(body.get('y', 0)),
                        'width': float(body.get('w', 400)), 'height': float(body.get('h', 300)), 'scale': 1}})
                    m = await c.wait(i, timeout=15)
                    return {'ok': True, 'data': (m.get('result') or {}).get('data', '')}
                if path == '/click':
                    # 可信点击（CDP 输入，isTrusted=true）：阿里 SDK embed 模式不认页面内 JS .click()
                    x, y = float(body['x']), float(body['y'])
                    c = Conn(ws)
                    await c.fire('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x - 4, 'y': y + 3})
                    await asyncio.sleep(0.06)
                    await c.fire('Input.dispatchMouseEvent', {'type': 'mouseMoved', 'x': x, 'y': y})
                    await asyncio.sleep(0.05)
                    await c.fire('Input.dispatchMouseEvent', {'type': 'mousePressed', 'x': x, 'y': y,
                                                              'button': 'left', 'buttons': 1, 'clickCount': 1})
                    await asyncio.sleep(0.09)
                    await c.fire('Input.dispatchMouseEvent', {'type': 'mouseReleased', 'x': x, 'y': y,
                                                              'button': 'left', 'buttons': 0, 'clickCount': 1})
                    return {'ok': True, 'x': x, 'y': y}
                if path == '/dragto3':
                    # 开 watcher 抓判定码，拖完等 1s 读最新判定
                    while len(VERDICTS):
                        VERDICTS.pop()
                    watch = asyncio.create_task(watch_verdicts(ws_url, time.time() + 25))
                    try:
                        r = await drag_to3(ws, float(body['sx']), float(body['sy']),
                                           float(body['target_x']))
                        dl = time.time() + 10.0
                        vs = []
                        while time.time() < dl:
                            vs = [v for v in VERDICTS if v.get('code') not in ('WATCH_ERR',)]
                            if vs:
                                break
                            await asyncio.sleep(0.3)
                        r['verdicts'] = vs
                        if vs:
                            r['verdict'] = vs[-1]['code']
                            r['passed'] = vs[-1]['ok']
                        return r
                    finally:
                        watch.cancel()
                if path == '/dragto':
                    return await drag_to(ws, float(body['sx']), float(body['sy']),
                                         float(body['target_x']), body.get('dur_ms'))
                if path == '/drag':
                    return await drag_open(ws, float(body['sx']), float(body['sy']),
                                           float(body['dx']), body.get('dur_ms'))
                if path == '/state':
                    return {'ok': True, 'state': await Conn(ws).state()}
                if path == '/tabs':
                    lst = json.loads(urllib.request.urlopen(CDP_LIST, timeout=5).read())
                    pages = [x for x in lst if x.get('type') == 'page']
                    act = body.get('action', 'list')
                    if act == 'list':
                        return {'ok': True, 'tabs': [{'id': p['id'], 'url': p.get('url', '')} for p in pages]}
                    if act == 'close':
                        tid = body.get('id')
                        c2 = Conn(ws)
                        await c2.send('Target.closeTarget', {'targetId': tid})
                        return {'ok': True, 'closed': tid}
                    if act == 'new':
                        c2 = Conn(ws)
                        i = await c2.send('Target.createTarget',
                                          {'url': body.get('url', 'about:blank')})
                        m = await c2.wait(i, timeout=10)
                        return {'ok': True, 'targetId': (m.get('result') or {}).get('targetId')}
                    return {'ok': False, 'err': 'bad-action'}
                if path == '/eval':
                    return {'ok': True, 'value': await Conn(ws).eval(body['expr'])}
                return {'ok': False, 'err': 'unknown-path'}

        try:
            res = run(go())
        except Exception as e:
            res = {'ok': False, 'err': repr(e)[:200]}
        self._ret(200 if res.get('ok') else 500, res, body.get('action', ''))


def main():
    main_srv()


if __name__ == '__main__':
    main()
