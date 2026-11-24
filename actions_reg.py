#!/usr/bin/env python3
"""actions_reg.py — 位置无关的 z.ai 注册+激活管线（盒子 / GitHub Actions 通用）。

架构：
  免费代理池（proxy_pool.py 验活）→ chrome --proxy-server 全 z.ai 流量走代理
  → 滑块(reg_loop+dragd) → signup → 当场内联激活(CloudMail→verify→finish→oauth 同代理)
  → 账号/凭据推 collector（可选，无 collector 就落本地）

配额轮换：signup:ip 到顶(405/429)或 F0xx 连墙 → 换下一个代理重启 chrome（秒级，无路由重启）。

用法: python3 actions_reg.py --target 10 [--rounds-max 0]
环境变量:
  COLLECTOR_URL / COLLECTOR_TOKEN  推送目标（可选）
  PROXY_N=20 PROXY_MAXFETCH=400    代理池参数
  CHROME_EXTRA="--no-sandbox"      GHA runner 需要
"""
import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

import numpy  # noqa: F401  (reg_loop 依赖)
import reg_loop as R
from proxy_pool import check as proxy_check, fetch_list as proxy_fetch

TARGET = 10
LANE_ID = os.environ.get('LANE_ID', '')
CDP_PORT = int(os.environ.get('CDP_PORT', '9333'))
DRAGD_PORT = int(os.environ.get('DRAGD_PORT', '19333'))
DISPLAY_NUM = os.environ.get('DISPLAY_NUM', '98')
PROXY_N = int(os.environ.get('PROXY_N', '48'))
PROXY_MAXFETCH = int(os.environ.get('PROXY_MAXFETCH', '800'))
COLLECTOR_URL = os.environ.get('COLLECTOR_URL', '')
COLLECTOR_TOKEN = os.environ.get('COLLECTOR_TOKEN', '')
ACCT_F = f'{BASE}/accounts.jsonl'
STATE_F = f'{BASE}/actions_state{"_lane" + LANE_ID if LANE_ID else ""}.json'
LOG_F = f'{BASE}/actions_reg.log'
PROXY_F = os.environ.get('LANE_PROXY_FILE', f'{BASE}/proxies.json')
PW = os.environ.get('ZREG_PW', '')            # 账号统一密码：secrets
F_WALL = 6          # F0xx 连墙 N 次 → 换代理
ERR_WALL = 3        # 基础设施错误连墙 N 次 → 重启栈（同代理）

_procs = {'xvfb': None, 'chrome': None, 'dragd': None}
_cur_tab = None


_EMAIL_RE = re.compile(r'([A-Za-z0-9]{5})[A-Za-z0-9]{3,}(@[A-Za-z0-9.-]+)')


def log(m):
    # 仓库可能公开：job 日志对外可见，邮箱一律打码（凭据不进日志）
    tag = f'[L{LANE_ID}]' if LANE_ID else ''
    m = _EMAIL_RE.sub(lambda x: f'{x.group(1)}…{x.group(2)}', str(m))
    line = f'[{time.strftime("%m-%d %H:%M:%S")}]{tag} {m}'
    print(line, flush=True)
    with open(LOG_F, 'a') as f:
        f.write(line + '\n')


def load_state():
    try:
        return json.load(open(STATE_F))
    except Exception:
        return {'ok': 0, 'rounds': 0, 'f_streak': 0, 'err_streak': 0,
                'proxy_idx': 0, 'signups_this_proxy': 0, 'pending_act': []}


def save_state(st):
    tmp = STATE_F + '.tmp'
    json.dump(st, open(tmp, 'w'))
    os.replace(tmp, STATE_F)


# ---------------------------------------------------------------- proxy pool
def load_proxies(refresh=False):
    age = time.time() - os.path.getmtime(PROXY_F) if os.path.exists(PROXY_F) else 1e9
    if not refresh and age < 7200:
        pl = json.load(open(PROXY_F))
        if pl:
            return pl
    log(f'fetching proxy pool (max {PROXY_MAXFETCH})...')
    from concurrent.futures import ThreadPoolExecutor
    import random as _rnd
    cand = proxy_fetch()
    _rnd.shuffle(cand)                      # 候选 1.3 万：各 job 随机取样，避免都挤在字典序前 800 个出口上
    cand = cand[:PROXY_MAXFETCH]
    with ThreadPoolExecutor(max_workers=80) as ex:
        rs = [r for r in ex.map(proxy_check, cand) if r]
    rs.sort(key=lambda x: x['latency_ms'])
    log(f'proxy pool alive: {len(rs)}/{len(cand)}')
    json.dump(rs[:PROXY_N], open(PROXY_F, 'w'))
    return rs[:PROXY_N]


# ---------------------------------------------------------------- stack
def _which(*names):
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    return None


def _kill_stack():
    for k in ('chrome', 'dragd'):      # Xvfb 是多 lane 共用的，谁都不许在这里杀
        p = _procs.get(k)
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
    time.sleep(2)
    for k in ('chrome', 'dragd'):
        p = _procs.get(k)
        if p and p.poll() is None:
            try:
                p.kill()
            except Exception:
                pass
        _procs[k] = None


_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_ok(url, timeout=5, data=None):
    try:
        rq = urllib.request.Request(url, data=data,
                                    headers={'Content-Type': 'application/json'})
        _NO_PROXY_OPENER.open(rq, timeout=timeout).read(100)
        return True
    except Exception:
        return False


# 阿里验证码全家桶直连（zreg 实证：这些 CDN 不承担 z.ai 风控打分，走代理只会拖慢+变 NO-WIDGET/F015）
# 注意：chromium --proxy-bypass-list 用分号分隔，逗号会被当成一个主机名（实测踩坑）
ALI_BYPASS = ('g.alicdn.com;o.alicdn.com;static-captcha.aliyuncs.com;'
              'no8xfe.captcha-open.aliyuncs.com;no8xfe-verify.captcha-open.aliyuncs.com;'
              'upload.captcha-open.aliyuncs.com;cf.aliyun.com;sdk.rum.aliyuncs.com')


def ensure_xvfb(xvfb_bin=None):
    """多 lane 共用一个 Xvfb：独立会话启动、不归属任何 lane（lane 重启自己的栈不会连带杀掉显示）。"""
    xvfb_bin = xvfb_bin or _which('Xvfb') or os.path.expanduser('~/x-root/usr/bin/Xvfb')
    sock = f'/tmp/.X11-unix/X{DISPLAY_NUM}'
    if not os.path.exists(sock):
        subprocess.Popen(
            [xvfb_bin, f':{DISPLAY_NUM}', '-screen', '0', '1600x1000x24', '-nolisten', 'tcp'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    dl = time.time() + 15
    while time.time() < dl and not os.path.exists(sock):
        time.sleep(0.2)
    return os.path.exists(sock)


def start_stack(proxy_url):
    """启动 Xvfb + chrome(代理) + dragd，条件等待就绪。"""
    _kill_stack()
    xvfb_bin = _which('Xvfb') or os.path.expanduser('~/x-root/usr/bin/Xvfb')
    chrome_bin = _which('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser')
    py = sys.executable
    if not (xvfb_bin and chrome_bin):
        raise RuntimeError(f'missing binaries: xvfb={xvfb_bin} chrome={chrome_bin}')

    ensure_xvfb(xvfb_bin)
    os.environ['DISPLAY'] = f':{DISPLAY_NUM}'

    # persistent profile：整个 run 复用同一 profile（zreg 实证 persistent 是过 F015 的必要条件；
    # 换代理重启栈也保留——指纹/设备历史在 profile 里累积）
    profile = os.environ.get('ZREG_PROFILE_DIR') or f'/tmp/zreg-profile-{os.getpid()}'
    for f in (f'{profile}/SingletonLock', f'{profile}/SingletonCookie', f'{profile}/SingletonSocket'):
        try:
            os.remove(f)
        except OSError:
            pass
    chrome_args = [chrome_bin, f'--user-data-dir={profile}', '--no-first-run',
                   f'--remote-debugging-port={CDP_PORT}', '--disable-dev-shm-usage',
                   f'--proxy-server={proxy_url}',
                   f'--proxy-bypass-list={ALI_BYPASS}',
                   '--window-size=1440,900', '--window-position=0,0',
                   'https://chat.z.ai/auth?redirect_uri=fuckzaicn']
    for extra in os.environ.get('CHROME_EXTRA', '').split():
        chrome_args.insert(1, extra)
    _procs['chrome'] = subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL,
                                        stderr=open(f'{BASE}/chrome.log', 'ab'))

    env = dict(os.environ)
    env['DRAGD_CDP'] = f'http://127.0.0.1:{CDP_PORT}/json/list'
    env['DRAGD_PORT'] = str(DRAGD_PORT)
    env['PYTHONPATH'] = BASE
    _procs['dragd'] = subprocess.Popen(
        [py, '-u', f'{BASE}/dragd2.py'], env=env, cwd=BASE,
        stdout=open(f'{BASE}/dragd.log', 'ab'), stderr=subprocess.STDOUT)

    # 条件等待：chrome CDP + dragd 双就绪（单次 sleep ≤0.5s，总上限 90s）
    dl = time.time() + 90
    while time.time() < dl:
        cdp = _http_ok(f'http://127.0.0.1:{CDP_PORT}/json/version')
        dg = _http_ok(f'http://127.0.0.1:{DRAGD_PORT}/ping')
        if cdp and dg:
            log(f'stack up (proxy={proxy_url})')
            return True
        time.sleep(0.5)
    log('stack start timeout')
    dp = _procs.get('dragd')
    try:
        ss = subprocess.run(['ss', '-tlnp'], capture_output=True, text=True, timeout=5)
        ss_lines = [l for l in ss.stdout.splitlines() if str(DRAGD_PORT) in l or str(CDP_PORT) in l]
    except Exception as e:
        ss_lines = [f'ss fail {e!r}']
    log(f'  diag: chrome.poll={_procs["chrome"] and _procs["chrome"].poll()} '
        f'dragd.poll={dp and dp.poll()} xsock='
        f'{os.listdir("/tmp/.X11-unix") if os.path.isdir("/tmp/.X11-unix") else None} '
        f'cdp_ok={cdp} dragd_ok={dg} ss={ss_lines}')
    for label, url, data in [('GET-ping', f'http://127.0.0.1:{DRAGD_PORT}/ping', None),
                             ('POST-tabs', f'http://127.0.0.1:{DRAGD_PORT}/tabs',
                              json.dumps({'action': 'list'}))]:
        try:
            rq = urllib.request.Request(url, data=data,
                                        headers={'Content-Type': 'application/json'})
            r = _NO_PROXY_OPENER.open(rq, timeout=8).read(200)
            log(f'  probe {label}: OK {r[:120]}')
        except Exception as e:
            log(f'  probe {label}: {repr(e)[:200]}')
    try:
        tail = open(f'{BASE}/dragd.log', 'rb').read()[-800:].decode(errors='replace')
        log(f'  dragd.log tail: {tail!r}')
    except Exception:
        pass
    return False


def stack_healthy():
    return (_procs['chrome'] and _procs['chrome'].poll() is None
            and _http_ok(f'http://127.0.0.1:{DRAGD_PORT}/ping', timeout=6))


# ---------------------------------------------------------------- tabs
def open_new_tab():
    global _cur_tab
    try:
        rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/tabs',
            data=json.dumps({'action': 'new',
                             'url': 'https://chat.z.ai/auth?redirect_uri=fuckzaicn'}).encode(),
            headers={'Content-Type': 'application/json'})
        d = json.loads(_NO_PROXY_OPENER.open(rq, timeout=30).read())
        _cur_tab = d.get('targetId')
        return True
    except Exception as e:
        log(f'open tab fail: {repr(e)[:60]}')
        return False


def close_stale_tabs():
    try:
        rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/tabs',
            data=json.dumps({'action': 'list'}).encode(),
            headers={'Content-Type': 'application/json'})
        d = json.loads(_NO_PROXY_OPENER.open(rq, timeout=20).read())
        for t in d.get('tabs', []):
            if t.get('id') == _cur_tab:
                continue
            if 'chat.z.ai' in t.get('url', ''):
                try:
                    rq2 = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/tabs',
                        data=json.dumps({'action': 'close', 'id': t.get('id')}).encode(),
                        headers={'Content-Type': 'application/json'})
                    _NO_PROXY_OPENER.open(rq2, timeout=15).read()
                except Exception:
                    pass
    except Exception as e:
        log(f'close tabs fail: {repr(e)[:60]}')


def wait_for(cond, timeout=8.0, interval=0.3):
    dl = time.time() + timeout
    while time.time() < dl:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(min(interval, 1.0))
    return False


# ---------------------------------------------------------------- round
def _page_ctx(tag):
    """页面现场快照：URL + 正文切片（NO-WIDGET 等失败路径诊断用）。"""
    try:
        href = R.ev('location.href')
        txt = (R.ev('document.body ? document.body.innerText.slice(0,200) : "NOBODY"')
               or '').replace('\n', ' | ')
        return f'{tag} href={href} txt={txt!r}'
    except Exception as e:
        return f'{tag} ctx-err {repr(e)[:60]}'


IN_TAB_MAX = 14      # 每 tab 页内连刷上限（widget 自动换题，不再重开页面）


def _wait_widget(timeout):
    """等题目真正可见：滑块把手有宽度 **且** 背景图已布局出来（宽度>50）。
    只看 naturalWidth 会把「已加载但隐藏（宽 0）」的图当成就绪（11-24 实测：(0/0) 越界 55 次/10 分钟）。"""
    dl = time.time() + timeout
    st = {}
    while time.time() < dl:
        try:
            st = R.state() or {}
            if _widget_ready(st):
                return st
        except Exception:
            pass
        time.sleep(0.4)
    return st


def _widget_ready(st):
    return bool(st.get('slider') and st.get('bg') and st['bg'].get('w', 0) > 50)


def _force_show_captcha():
    """把阿里验证码窗口（popup/embed）及其隐藏的祖先强制显示出来。"""
    R.ev("""(()=>{const show=e=>{if(!e)return;e.style.display='block';e.style.visibility='visible';e.style.opacity='1';e.classList.add('aliyunCaptcha-show')};
show(document.getElementById('aliyunCaptcha-window-popup'));show(document.getElementById('aliyunCaptcha-window-embed'));
const img=document.getElementById('aliyunCaptcha-img');let p=img;for(let i=0;i<4&&p;i++){if(getComputedStyle(p).display==='none'||getComputedStyle(p).visibility==='hidden')show(p);p=p.parentElement}
return 1})()""")
    time.sleep(1.2)

def one_round(trial):
    """一个 tab：开页+填表一次，页内连刷滑块直到 signup 成功或 widget 死。
    返回 (ok, email, verdict, err)；verdict 尽量透传最后一轮的判定码供配额/F墙分类。"""
    import random
    R.PORT = DRAGD_PORT
    open_new_tab()
    wait_for(lambda: R.ev(
        'document.readyState==="complete" && '
        '(!!document.querySelector("form input") || '
        '[...document.querySelectorAll("a,button")].some(b=>/Continue with Email/i.test(b.textContent||"")))', 8) is True, 45, 0.5)
    close_stale_tabs()
    log(_page_ctx('page-loaded'))
    if (R.ev('(document.body?document.body.innerText:"").length') or 0) < 50:
        R.ev('location.reload(); 1')
        wait_for(lambda: R.ev('document.readyState==="complete"', 8) is True, 30, 0.5)
        if (R.ev('(document.body?document.body.innerText:"").length') or 0) < 50:
            log(_page_ctx('PAGE-BLANK'))
            return False, '-', 'PAGE-BLANK', None
        log('blank recovered after reload')
    # intl 社交落地页 → Continue with Email 进邮箱表单
    R.ev("""(()=>{const b=[...document.querySelectorAll("a,button")].find(b=>/Continue with Email|继续使用邮箱/i.test(b.textContent||''));if(b){b.click();return 1}return 0})()""")
    wait_for(lambda: R.ev('!!document.querySelector("form input")', 6) is True, 15, 0.5)
    # 登录↔注册切换（CN“注册” / intl "Sign up"，避开 Google/Github）
    R.ev("""(()=>{const b=[...document.querySelectorAll("a,button")].find(b=>{const t=(b.textContent||'').trim();return /(sign\s*up|注册)/i.test(t)&&!/google|github|apple|wechat/i.test(t)});if(b){b.click();return 1}return 0})()""")
    wait_for(lambda: R.ev('!!document.querySelector("form input[name=new-password], form input[type=password]")', 6) is True, 10, 0.3)
    email = R.open_challenge()
    # 密码框必须回读校验：React 重渲染可能换掉节点（11-24 实测填完即丢），最多重填 4 次
    _pw_ok = False
    for _try in range(4):
        _pwlen = R.ev("""(()=>{const e=document.querySelector('form input[name="new-password"]')||document.querySelector('form input[type="password"]')||document.querySelector('input[type="password"]');return e?(e.value||'').length:-1})()""")
        if _pwlen == len(PW):
            _pw_ok = True
            break
        R.open_challenge(email=email)
        time.sleep(0.6)
    if not _pw_ok:
        log('WARN password not filled after retries: pwlen=%s %s' % (_pwlen, R.ev("""(()=>{try{const ins=[...document.querySelectorAll('input')].map(e=>[e.name||'-',e.type||'-',e.autocomplete||'-',(e.placeholder||'').slice(0,20),(e.value||'').length,e.closest('form')?'in':'out'].join('|'));return JSON.stringify({inputs:ins,href:location.href})}catch(e){return 'js-err '+String(e).slice(0,80)}})()""")))
    # 新版表单（11-24 出现）：验证码是内嵌按钮「Click to start verification」，必须可信点击才出滑块
    xy = R.ev("""(()=>{const b=[...document.querySelectorAll('button,div,span,a,label')].find(e=>e.children.length<5&&/^\\s*(click to start verification|start verification|点击开始验证|开始验证|点击验证)/i.test((e.textContent||'').trim())&&(e.textContent||'').trim().length<40);if(!b)return null;const r=b.getBoundingClientRect();return (r.width>8&&r.height>8)?[r.x+r.width/2,r.y+r.height/2]:null})()""")
    if xy:
        try:
            rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/click',
                data=json.dumps({'x': xy[0], 'y': xy[1]}).encode(),
                headers={'Content-Type': 'application/json'})
            _NO_PROXY_OPENER.open(rq, timeout=30).read()
            log('start-verification button clicked')
        except Exception as e:
            log(f'start-verification click fail: {repr(e)[:50]}')
    # fetch 钩子装一次，整个 tab 生命周期有效
    R.ev("""(()=>{window.__signup=null;const of=window.fetch;window.fetch=async function(...a){const u=String(a[0]||"");const r=await of.apply(this,a);try{if(/auths\/signup/.test(u)){const c=await r.clone().text();window.__signup={st:r.status,body:c.slice(0,200)}}}catch(e){};return r};return 1})()""")

    last_v, last_err = 'NO-WIDGET', None
    ntype = 0
    for att in range(IN_TAB_MAX):
        st = _wait_widget(20 if att == 0 else 12)
        if not _widget_ready(st):
            _force_show_captcha()
            st = _wait_widget(8)
        if not _widget_ready(st):
            R.ev("""(()=>{const b=document.querySelector('#aliyunCaptcha-captcha-body button')||document.querySelector('#aliyunCaptcha-captcha-body');if(b)b.click();return 1})()""")
            st = _wait_widget(8)
        if not _widget_ready(st):
            if att == 0:
                log(_page_ctx('NO-WIDGET'))
                _dbg_push(st, tag='nowidget')
                return False, email, 'NO-WIDGET', None
            log(f'widget dead after {att} attempts')
            return False, email, last_v, last_err
        if 'inpaint' in st['bg']['src'] or not R.ev("""!!document.querySelector('#aliyunCaptcha-puzzle')"""):
            # 阿里新题型「图片还原」（抹黑竖带，static-captcha-sgp 的 inpainted_with_mask）：现有找洞算法不适用
            last_v, last_err = 'NEW-TYPE', None
            ntype += 1
            if ntype == 1:
                _dbg_push(st, tag='newtype')
            if ntype >= 3:
                log(f'new captcha type x{ntype} -> leave this tab')
                return False, email, 'NEW-TYPE', None
            R.ev("""(()=>{const b=document.querySelector('#aliyunCaptcha-btn-refresh')||document.querySelector('#aliyunCaptcha-captcha-body button');if(b)b.click();return 1})()""")
            time.sleep(2.5 + random.random() * 1.5)
            continue
        try:
            hx, x0 = R.hole(st)
        except Exception as e:
            _dbg_push(st)
            return False, email, f'HOLE-ERR {repr(e)[:40]}', None
        target = st['bg']['x'] + (hx - x0) * (st['bg']['w'] / st['bg']['nw'])
        off = target - st['bg']['x']
        if not (6 <= off <= st['bg']['w'] - 6):
            # 洞检测给出越界值（实测出现过 err=-571px）：这种图 CV 失效，拖了也是白拖 → 换题
            log(f'hole out of range ({off:.0f}/{st["bg"]["w"]:.0f}) → refresh challenge')
            _dbg_push(st, hx, x0)
            R.ev("""(()=>{const b=document.querySelector('#aliyunCaptcha-captcha-body button')||document.querySelector('#aliyunCaptcha-refreshIcon')||document.querySelector('.aliyunCaptcha-refresh');if(b)b.click();return 1})()""")
            time.sleep(2.5 + random.random() * 1.5)
            continue
        # dragto3 带软重试：widget-not-ready / 瞬时 500 重等 widget 再拖，不当轮失败
        r = None
        for dtry in range(3):
            rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/dragto3',
                data=json.dumps({'sx': st['slider']['x'] + 20, 'sy': st['slider']['y'] + 20,
                                 'target_x': round(target, 1)}).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                r = json.loads(_NO_PROXY_OPENER.open(rq, timeout=300).read())
                break
            except urllib.error.HTTPError as e:
                body = e.read().decode(errors='replace')[:80]
                if 'widget-not-ready' in body or e.code == 500:
                    time.sleep(1.5)
                    st2 = _wait_widget(8)
                    if st2.get('slider') and st2.get('bg'):
                        st = st2
                        target = st['bg']['x'] + (hx - x0) * (st['bg']['w'] / st['bg']['nw'])
                    continue
                return False, email, f'DRAG-ERR {repr(e)[:40]}', None
            except Exception as e:
                return False, email, f'DRAG-ERR {repr(e)[:40]}', None
        if r is None:
            time.sleep(1.5 + random.random() * 2)
            continue
        v = r.get('verdict')
        last_v, last_err = v or 'NO-VERDICT', r.get('final_err')
        if v not in ('T001', None):
            # F0xx 等：SDK 自动换题，小抖动后继续（不出页面）
            time.sleep(1.5 + random.random() * 2.5)
            continue
        # T001 / NO-VERDICT → 直接尝试提交，__signup 捕获判定
        ver_ok = False
        for _ in range(20):
            tb = R.ev('(document.querySelector("#aliyunCaptcha-captcha-text-box")||{}).className||""')
            txt = R.ev("""(()=>{const t=((document.querySelector("form")||{}).innerText||"");return t.includes("验证通过")||/verified|verification passed/i.test(t)})()""")
            if 'verified' in (tb or '') or txt:
                ver_ok = True
                break
            time.sleep(0.25)
        R.ev("""(()=>{const b=[...document.querySelectorAll("form button")].find(b=>/创建账号|create account|sign\s*up/i.test(b.textContent||''));if(b)b.click();return 1})()""")
        s = 'null'
        for _ in range(20):
            s = R.ev('JSON.stringify(window.__signup||null)')
            if s != 'null':
                break
            time.sleep(0.5)
        try:
            sd = json.loads(s)
            ok = sd.get('st') == 200 and 'success":true' in (sd.get('body') or '')
            if ok:
                return True, email, 'T001-SIGNUP200', r.get('final_err')
            last_v = f'SIGNUP-{s[:50]}'
        except Exception:
            if v == 'T001':
                last_v = 'T001-NO-UI' if not ver_ok else 'SIGNUP-TIMEOUT'
        time.sleep(1.0 + random.random() * 1.5)   # 换题间隙
    return False, email, last_v, last_err


# ---------------------------------------------------------------- activation
def activate(email, pw, proxy_url, alt_proxies=None):
    """内联激活：CloudMail 找 token → verify/finish/oauth（全部走当前代理）。
    返回 (state_dict|None, reason)。"""
    from zreg_min.mailbox import CloudMail
    from zreg_min import http_box
    os.environ['ZREG_HTTP_PROXY_URL'] = proxy_url
    http_box._OP = None  # 换代理后重建 opener

    name = email.split('@')[0]
    m = CloudMail()
    token = None
    dl = time.time() + 90
    while time.time() < dl and not token:
        try:
            mails = m.receive_list(1, size=100)
            for x in mails:
                if (x.get('toEmail') or '').lower() != email.lower():
                    continue
                mm = re.search(r'token=(verify-[a-zA-Z0-9]+)', x.get('text') or '')
                if mm:
                    token = mm.group(1)
                    break
        except Exception:
            pass
        if not token:
            time.sleep(3)
    if not token:
        return None, 'no-mail'

    def hj(method, url, body=None, headers=None, form=None, timeout=30):
        return http_box.http_json(method, url, body=body, headers=headers,
                                  form=form, timeout=timeout)

    s, r, _ = hj('POST', 'https://chat.z.ai/api/v1/auths/verify_email',
                 body={'username': name, 'email': email, 'token': token},
                 headers={'Origin': 'https://chat.z.ai',
                          'Referer': 'https://chat.z.ai/auth/verify_email'})
    if s == 400 and 'invalid' in str(r):
        hj('POST', 'https://chat.z.ai/api/v1/auths/resend_email',
           body={'name': name, 'email': email, 'sso_redirect': None},
           headers={'Origin': 'https://chat.z.ai',
                    'Referer': 'https://chat.z.ai/auth/verify_email'})
        time.sleep(8)
        token = None
        try:
            for x in m.receive_list(1, size=20):
                if (x.get('toEmail') or '').lower() == email.lower():
                    mm = re.search(r'token=(verify-[a-zA-Z0-9]+)', x.get('text') or '')
                    if mm:
                        token = mm.group(1)
                        break
        except Exception:
            pass
        if not token:
            return None, 'resend-no-token'
        s, r, _ = hj('POST', 'https://chat.z.ai/api/v1/auths/verify_email',
                     body={'username': name, 'email': email, 'token': token},
                     headers={'Origin': 'https://chat.z.ai',
                              'Referer': 'https://chat.z.ai/auth/verify_email'})
    if s != 200:
        return None, f'verify-{s}'

    import uuid as _uuid
    # finish_signup 按 IP 限额，且与 signup 共用。免费代理 IP 多人共用，常常一上来就是 429——
    # 实测同一出口重试无用（一个 lane 6 连 429）。验证 token 已在手，直接换出口重发。
    exits = [proxy_url] + [p for p in (alt_proxies or []) if p != proxy_url][:4]
    chat_jwt = None
    for ei, ex in enumerate(exits):
        if ei:
            os.environ['ZREG_HTTP_PROXY_URL'] = ex
            http_box._OP = None
            log(f'  {name}: finish-429 on this exit → retry via {ex}')
        for attempt in range(2 if ei == 0 else 1):
            s2, r2, _ = hj('POST', 'https://chat.z.ai/api/v1/auths/finish_signup',
                           body={'username': name, 'email': email, 'token': token,
                                 'password': pw, 'profile_image_url': '',
                                 'device_id': str(_uuid.uuid4())},
                           headers={'Origin': 'https://chat.z.ai',
                                    'Referer': 'https://chat.z.ai/auth/verify_email'})
            if s2 == 200 and r2.get('success'):
                chat_jwt = (r2.get('user') or {}).get('token')
                break
            if s2 != 429:
                return None, f'finish-{s2}'
            if attempt == 0 and ei == 0:
                time.sleep(30)        # 首个出口给一次瞬时 429 的机会
        if chat_jwt:
            break
    if not chat_jwt:
        return None, 'finish-429'

    oauth = http_box.oauth_http(chat_jwt, log=lambda x: log(f'  {name}: {x}'))
    import uuid as _uuid
    if not oauth or not oauth.get('zcode_jwt'):
        # finish 已成：chat_jwt 必须落盘（signin 要验证码，丢了就捞不回）
        return {
            'email': email, 'password': pw, 'name': name,
            'signup_done': True, 'chat_jwt': chat_jwt, 'oauth_pending': True,
            'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
        }, 'oauth-pending'
    state = {
        'email': email, 'password': pw, 'name': name,
        'signup_done': True, 'chat_jwt': chat_jwt,
        'zcode_jwt': oauth['zcode_jwt'],
        'zai_access_token': oauth.get('zai_access_token', ''),
        'user': oauth.get('user', {}),
        'device_mid': str(_uuid.uuid4()),
        'credential': {
            'oauth:active_provider': 'zai',
            'oauth:zai:access_token': oauth.get('zai_access_token', ''),
            'zcodejwttoken': oauth['zcode_jwt'],
            'oauth:zai:user_info': json.dumps(oauth.get('user', {}), ensure_ascii=False),
        },
        'ts': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    return state, 'ok'


# ---------------------------------------------------------------- start plan claim
ZVER = "3.18.2"
INFRA_PREFIX = ('balance-exit-blocked', 'balance-unreachable', 'EXC', 'NO-WIDGET', 'widget-dead', 'ip-quota')
ZERO_MID = '00000000-0000-0000-0000-000000000000'


def _zh(jwt, mid):
    return {"User-Agent": f"ZCode/{ZVER}", "X-ZCode-App-Version": ZVER,
            "X-Platform": "linux-x64", "X-Client-Language": "en-US",
            "X-Device-Mid": mid or ZERO_MID, "Authorization": f"Bearer {jwt}",
            "Accept": "application/json"}


def plan_active(jwt, mid):
    """(active: True/False/None, http_status)。None=查询失败。
    status 401/403 = 号的 token 死了（真实失败）；405/429/0 = 这个出口被拦（换出口）。"""
    from zreg_min import http_box
    s, r, _ = http_box.http_json(
        "GET", f"https://zcode.z.ai/api/v1/zcode-plan/billing/balance?app_version={ZVER}",
        headers=_zh(jwt, mid), timeout=25)
    if s != 200 or not isinstance(r, dict):
        return None, s
    plans = (r.get('data') or {}).get('plans') or []
    return any('start' in str(p.get('plan_id', '')).lower()
               and str(p.get('status', '')).lower() == 'active' for p in plans), s


def current_plan_id(jwt, mid):
    """当前 claim 活动 plan_id（marketing/touch，每周变，不硬编码）。"""
    from zreg_min import http_box
    s, r, _ = http_box.http_json(
        "GET", "https://zcode.z.ai/api/v1/marketing/touch?seq=0&locale=en-US",
        headers=_zh(jwt, mid), timeout=25)
    try:
        for d in ((r or {}).get('data') or {}).get('deliveries') or []:
            for btn in ((d.get('banner') or {}).get('buttons') or []):
                act = btn.get('action') or {}
                if act.get('type') == 'claim_zcode_plan':
                    pid = (act.get('args') or {}).get('plan_id')
                    if pid:
                        return pid
    except Exception:
        pass
    return 'zcode-v3-start-plan-0817'


def claim_post(jwt, mid, tok, plan_id):
    from zreg_min import http_box
    h = _zh(jwt, mid)
    h.update({"X-Aliyun-Captcha-Verify-Param": tok,
              "X-Aliyun-Captcha-Verify-Region": "cn",
              "Content-Type": "application/json"})
    return http_box.http_json(
        "POST", "https://zcode.z.ai/api/v1/zcode-plan/billing/claim",
        body={"plan_id": plan_id}, headers=h, timeout=30)


def _tab(action, **kw):
    rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/tabs',
                                data=json.dumps({'action': action, **kw}).encode(),
                                headers={'Content-Type': 'application/json'})
    try:
        return json.loads(_NO_PROXY_OPENER.open(rq, timeout=30).read())
    except Exception as e:
        log(f'tab {action} fail: {repr(e)[:60]}')
        return {}


def claim_plan(jwt, mid, proxy_url, max_drag=14, alt_proxies=None):
    """领 Start Plan：本地 zclaim 页起 cn 区滑块 → dragd 拖 → token → POST claim → balance 判定。
    出口被拦（balance 405/429/连不上）时依次换 alt_proxies 重试。
    返回 (active: bool, why: str)。"""
    from zreg_min import http_box
    import random
    R.PORT = DRAGD_PORT          # reg_loop 默认 19223；只有 one_round 会设，补领路径必须自己设
    exits = [proxy_url] + [p for p in (alt_proxies or []) if p != proxy_url][:5]
    last = None
    for ex in exits:
        os.environ['ZREG_HTTP_PROXY_URL'] = ex
        http_box._OP = None
        a, hs = None, 0
        for _ in range(2):
            a, hs = plan_active(jwt, mid)
            if a is not None:
                break
        if a:
            return True, 'already'
        if hs in (401, 403):
            return False, f'http-{hs}'          # token 死了，换出口没用
        if hs != 200:
            last = f'balance-exit-blocked-{hs}'  # 405/429/0：这个出口被拦，换下一个
            continue
        # 出口能通 zcode.z.ai：就在它上面起滑块领计划
        act, why = _claim_plan_on_exit(jwt, mid, ex, max_drag)
        if act or why.startswith(('http-', 'no-plan-after-max-drag')):
            return act, why                     # 领成了 / 号本身的问题：换出口没用
        last = why                               # NO-WIDGET / widget-dead / ip-quota：换出口再试
    return False, last or 'balance-exit-blocked'


def _claim_show_widget():
    """embed 模式下 widget DOM 默认不出、窗口默认隐藏（revive5 配方 ensure_widget）：
    无 DOM → __inst.refresh() 触发；再把 #aliyunCaptcha-window-embed 强制显示。"""
    R.ev("""(()=>{if(document.querySelectorAll('[id^="aliyunCaptcha"]').length===0){try{window.__inst.refresh();}catch(e){}}return 1})()""")
    time.sleep(5)
    R.ev("""(()=>{const w=document.getElementById('aliyunCaptcha-window-embed');if(w){w.style.display='block';w.classList.add('aliyunCaptcha-show');}return 1})()""")
    time.sleep(0.8)


def _shot(st, pad=30, above=50, below=70):
    """题目区域截图（base64 PNG），失败返回空串。"""
    try:
        b = st.get('bg') or {}
        if not b.get('w'):
            r = R.ev("""(()=>{const e=document.querySelector('#aliyunCaptcha-window-popup')||document.querySelector('#aliyunCaptcha-window-embed')||document.querySelector('form');if(!e)return null;const q=e.getBoundingClientRect();return {x:q.x,y:q.y,w:Math.max(q.width,300),h:Math.max(q.height,200)}})()""") or {'x': 300, 'y': 150, 'w': 520, 'h': 420}
            rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/shot',
                data=json.dumps({'x': max(0, r['x'] - 30), 'y': max(0, r['y'] - 10),
                                 'w': min(r['w'] + 60, 760), 'h': min(r.get('h', 300) + 200, 640)}).encode(),
                headers={'Content-Type': 'application/json'})
            return json.loads(_NO_PROXY_OPENER.open(rq, timeout=30).read()).get('data', '')
        rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/shot',
            data=json.dumps({'x': max(0, b.get('x', 0) - pad), 'y': max(0, b.get('y', 0) - above),
                             'w': b.get('w', 300) + 2 * pad, 'h': b.get('w', 300) + above + below}).encode(),
            headers={'Content-Type': 'application/json'})
        return json.loads(_NO_PROXY_OPENER.open(rq, timeout=30).read()).get('data', '')
    except Exception:
        return ''


def _dbg_push(st, hx=None, x0=None, tag='hole'):
    """洞检测失败现场 → collector（kind=dbg）：原图、DOM、区域截图。"""
    try:
        pcu = R.ev("(document.querySelector('#aliyunCaptcha-puzzle')||{src:''}).src") or ''
        push_collector('dbg', f'{tag}-L{LANE_ID or 0}', {
            'bg': (st.get('bg') or {}).get('src', '')[:300000], 'pc': pcu[:300000],
            'hx': None if hx is None else float(hx), 'x0': None if x0 is None else int(x0),
            'bg_rect': st.get('bg'), 'slider': st.get('slider'), 'shot': _shot(st),
            'els': (R.ev("""(()=>[...document.querySelectorAll('[id^=aliyunCaptcha],canvas')].map(e=>{const r=e.getBoundingClientRect();return e.tagName+'#'+e.id+'.'+String(e.className).slice(0,30)+' @'+Math.round(r.x)+','+Math.round(r.y)+' '+Math.round(r.width)+'x'+Math.round(r.height)}))()""") or []),
            'html': (R.ev("""(()=>{const e=document.querySelector('#aliyunCaptcha-window-popup')||document.querySelector('#aliyunCaptcha-window-embed')||document.querySelector('#aliyunCaptcha-captcha-body');return e?e.outerHTML.replace(/src="data:[^"]{200,}"/g,'src="data:…"').slice(0,7000):'no-container'})()""") or '')})
    except Exception as e:
        log(f'dbg push fail {repr(e)[:40]}')


def _claim_diag():
    """滑块不出时的页面现场：aliyun DOM 节点及尺寸/显隐、__inst、refresh() 结果、资源加载明细。"""
    try:
        return R.ev("""(()=>{const q=[...document.querySelectorAll('[id^="aliyunCaptcha"]')];
const ids=q.slice(0,8).map(e=>{const r=e.getBoundingClientRect();return e.id.slice(14,30)+':'+Math.round(r.width)+'x'+Math.round(r.height)+':'+getComputedStyle(e).display});
const res=performance.getEntriesByType('resource').map(e=>e.name.replace(/^https?:\/\//,'').slice(0,44)+'#'+(e.responseStatus||'?')+'/'+Math.round(e.duration));
let rf='';try{if(window.__inst){window.__inst.refresh();rf='refresh-ok'}else{rf='no-inst'}}catch(e){rf='refresh-err:'+String(e&&e.message||e).slice(0,40)}
return JSON.stringify({ids,rf,err:String(window.__err||'').slice(0,40),res:res.slice(-8)})})()""")
    except Exception as e:
        return f'diag-err {repr(e)[:60]}'


def _claim_plan_on_exit(jwt, mid, proxy_url, max_drag):
    from zreg_min import http_box
    import random
    plan_id = current_plan_id(jwt, mid)
    # 页面必须是 about:blank（revive5 配方）：场景 11xygtvd 疑似配了域名限制，http://127.0.0.1 来源下
    # SDK 初始化正常但静默不创建任何 widget DOM（11-24 诊断实证：ids=[] 且 refresh() 无报错）
    from dragd2 import ZCLAIM_PAGE
    tid = (_tab('new', url='about:blank#zclaim').get('targetId'))
    try:
        wait_for(lambda: R.ev('document.readyState==="complete"', 6) is True, 10, 0.5)
        R.ev('document.open();document.write(' + json.dumps(ZCLAIM_PAGE) + ');document.close();1')
        wait_for(lambda: R.ev('document.readyState==="complete" && typeof window.initAliyunCaptcha==="function"', 10) is True, 30, 0.5)
        wait_for(lambda: R.ev('!!window.__inst', 6) is True, 15, 0.5)      # getInstance 回调
        # #go 必须可信点击（CDP 输入）。JS .click() 是 untrusted，SDK embed 模式不触发挑战
        xy = R.ev("""(()=>{const b=document.querySelector('#go');if(!b)return null;const r=b.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2]})()""")
        if xy:
            rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/click',
                data=json.dumps({'x': xy[0], 'y': xy[1]}).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                _NO_PROXY_OPENER.open(rq, timeout=30).read()
            except Exception as e:
                log(f'  go-click fail: {repr(e)[:60]}')
        used = 0
        for att in range(max_drag):
            st = _wait_widget(10)
            if not (st.get('slider') and st.get('bg')):
                _claim_show_widget()
                st = _wait_widget(20)
            if not (st.get('slider') and st.get('bg')):
                err = R.ev('window.__err||""')
                diag = _claim_diag()
                log(f'  claim-diag: {diag}')
                if att == 0:
                    return False, f'NO-WIDGET {str(err)[:20]} | {diag[:230]}'
                return False, 'widget-dead'
            try:
                hx, x0 = R.hole(st)
            except Exception as e:
                return False, f'HOLE-ERR {repr(e)[:40]}'
            target = st['bg']['x'] + (hx - x0) * (st['bg']['w'] / st['bg']['nw'])
            rq = urllib.request.Request(f'http://127.0.0.1:{DRAGD_PORT}/dragto3',
                data=json.dumps({'sx': st['slider']['x'] + 20, 'sy': st['slider']['y'] + 20,
                                 'target_x': round(target, 1)}).encode(),
                headers={'Content-Type': 'application/json'})
            try:
                r = json.loads(_NO_PROXY_OPENER.open(rq, timeout=300).read())
            except Exception:
                time.sleep(1.5)
                continue
            dl = time.time() + 12
            n = 0
            while time.time() < dl:
                n = R.ev('window.__tokens.length') or 0
                if n > used:
                    break
                time.sleep(0.4)
            if n <= used:
                time.sleep(2 + random.random() * 2)   # F0xx：SDK 换题
                _claim_show_widget()
                continue
            tok = R.ev(f'window.__tokens[{used}]')
            used += 1
            s, resp, _ = claim_post(jwt, mid, tok, plan_id)
            log(f'  claim POST -> {s} {str(resp)[:110]} (drag err={r.get("final_err")})')
            if s in (401, 403):
                return False, f'http-{s}'
            if s == 429:
                return False, 'ip-quota'
            dl = time.time() + 16                      # 异步落账，以 balance 为准
            while time.time() < dl:
                a, hs = plan_active(jwt, mid)
                if a:
                    return True, f'claimed att{att + 1}'
                time.sleep(2.5)
            time.sleep(1.5 + random.random())
            _claim_show_widget()                       # 下一个 token：出新题
        return False, 'no-plan-after-max-drag'
    finally:
        if tid:
            _tab('close', id=tid)


# ---------------------------------------------------------------- collector
def push_collector(kind, name, data):
    if not COLLECTOR_URL:
        return False
    try:
        rq = urllib.request.Request(
            COLLECTOR_URL,
            data=json.dumps({'kind': kind, 'name': name, 'data': data}).encode(),
            headers={'Content-Type': 'application/json',
                     'Authorization': f'Bearer {COLLECTOR_TOKEN}'})
        _NO_PROXY_OPENER.open(rq, timeout=20).read()
        return True
    except Exception as e:
        log(f'collector push fail: {repr(e)[:60]}')
        return False


# ---------------------------------------------------------------- main
def _heartbeat(name):
    """每 45s 把共享日志尾巴推给 collector（kind=log），长跑时 VPS 侧可实时看，不必等 job 结束。"""
    import threading
    def _run():
        while True:
            time.sleep(45)
            try:
                lines = open(LOG_F, 'rb').read()[-6000:].decode(errors='replace').splitlines()[-40:]
                push_collector('log', name, {'lines': lines})
            except Exception:
                pass
    threading.Thread(target=_run, daemon=True).start()


def spawn_lanes(n, target, rounds_max):
    """多 lane 管理：Xvfb 共享，每 lane 独立 chrome+dragd+profile+代理分片。"""
    proxies = load_proxies(refresh=False)
    if not proxies:
        log('no proxies available, exit')
        sys.exit(1)
    log(f'lanes={n} target={target} proxies={len(proxies)}')
    ensure_xvfb()                          # 父进程先起公共 Xvfb
    hb = os.environ.get('HB_NAME')
    if hb and COLLECTOR_URL:
        _heartbeat(hb)
    per = [target // n + (1 if k < target % n else 0) for k in range(n)]
    rm = [rounds_max // n + (1 if k < rounds_max % n else 0) if rounds_max else 0
          for k in range(n)]
    procs = []
    for k in range(n):
        lane_f = f'{BASE}/proxies_lane{k}.json'
        json.dump(proxies[k::n], open(lane_f, 'w'))   # 轮转分片，不共 IP 额度
        env = dict(os.environ)
        env['LANE_ID'] = str(k)
        env['CDP_PORT'] = str(9340 + k)
        env['DRAGD_PORT'] = str(19440 + k)
        env['ZREG_PROFILE_DIR'] = f"/tmp/zreg-lane{k}-{os.getpid()}"
        env['LANE_PROXY_FILE'] = lane_f
        cmd = [sys.executable, '-u', os.path.abspath(__file__),
               '--target', str(per[k]), '--rounds-max', str(rm[k] or 10**9)]
        procs.append(subprocess.Popen(cmd, env=env, cwd=BASE))
    rc = 0
    for p in procs:
        rc |= p.wait()
    return rc


def main():
    global TARGET
    ap = argparse.ArgumentParser()
    ap.add_argument('--target', type=int, default=TARGET)
    ap.add_argument('--rounds-max', type=int, default=0)
    ap.add_argument('--lanes', type=int, default=1)
    args = ap.parse_args()
    TARGET = args.target
    missing = [k for k, v in (('ZREG_PW', PW), ('ZREG_EMAIL_DOMAIN', R.EMAIL_DOMAIN),
                              ('CLOUDMAIL_EMAIL', os.environ.get('CLOUDMAIL_EMAIL')),
                              ('CLOUDMAIL_PASSWORD', os.environ.get('CLOUDMAIL_PASSWORD'))) if not v]
    if missing:
        log(f'missing required secrets/env: {missing}')
        sys.exit(2)

    if args.lanes > 1 and not LANE_ID:
        rc = spawn_lanes(args.lanes, TARGET, args.rounds_max)
        sys.exit(rc)

    st = load_state()
    proxies = load_proxies(refresh=False)
    if not proxies:
        log('no proxies available, exit')
        sys.exit(1)
    idx = st.get('proxy_idx', 0) % len(proxies)

    log(f'=== actions start: {st["ok"]}/{TARGET}, {len(proxies)} proxies, idx={idx} ===')
    if not start_stack(proxies[idx]['url']):
        idx = (idx + 1) % len(proxies)
        if not start_stack(proxies[idx]['url']):
            sys.exit(1)

    rounds_done = 0
    while st['ok'] < TARGET:
        if args.rounds_max and rounds_done >= args.rounds_max:
            log(f'rounds-max {args.rounds_max} reached')
            break
        st['rounds'] += 1
        rounds_done += 1
        try:
            ok, email, verdict, err = one_round(st['rounds'])
        except Exception as e:
            ok, email, verdict, err = False, '-', f'EXC {repr(e)[:60]}', None

        if ok:
            st['ok'] += 1
            st['f_streak'] = 0
            st['err_streak'] = 0
            st['nw_streak'] = 0
            st['signups_this_proxy'] += 1
            with open(ACCT_F, 'a') as f:
                f.write(json.dumps({'t': time.time(), 'email': email, 'pw': PW,
                                    'verdict': verdict, 'err': err}) + '\n')
            push_collector('acct', email.split('@')[0],
                           {'email': email, 'pw': PW, 'verdict': verdict})
            log(f'ROUND {st["rounds"]}: #{st["ok"]}/{TARGET} {email} err={err}')
            # 内联激活（同代理）
            state, why = activate(email, PW, proxies[idx]['url'],
                                   [p['url'] for p in proxies if p['url'] != proxies[idx]['url']])
            if state:
                os.makedirs(f'{BASE}/zreg_state_out', exist_ok=True)
                os.makedirs(f'{BASE}/cred_out', exist_ok=True)
                nm = email.split('@')[0]
                json.dump(state, open(f'{BASE}/zreg_state_out/{nm}.json', 'w'),
                          ensure_ascii=False, indent=2)
                push_collector('zreg', nm, state)
                if state.get('credential'):
                    json.dump(state['credential'],
                              open(f'{BASE}/cred_out/{nm}.json', 'w'))
                    push_collector('cred', nm, state['credential'])
                if state.get('credential') and os.environ.get('ZREG_DO_CLAIM'):   # 新号 ~1h 自动开通，默认不领
                    act, why = claim_plan(state['zcode_jwt'], state.get('device_mid'), proxies[idx]['url'],
                                         alt_proxies=[p['url'] for p in proxies if p['url'] != proxies[idx]['url']])
                    push_collector('plan', nm, {'active': act, 'why': why,
                                                 'retry': (not act) and any(why.startswith(k) for k in INFRA_PREFIX)})
                    log(f'  {nm}: PLAN {"ACTIVE ✓" if act else "NOT claimed"} ({why})')
                if state.get('oauth_pending'):
                    log(f'  {nm}: FINISHED, oauth pending (chat_jwt saved)')
                else:
                    log(f'  {nm}: ACTIVATED ✓')
            else:
                st.setdefault('pending_act', []).append(email)
                log(f'  {email}: activation pending ({why})')
        else:
            log(f'ROUND {st["rounds"]}: fail {verdict} err={err} ({st["ok"]}/{TARGET})')
            v = str(verdict)
            quota = ('SIGNUP-{"st":405' in v or 'SIGNUP-{"st":429' in v)
            if v.startswith('F0'):
                st['f_streak'] += 1
            elif v == 'NO-WIDGET':
                st['nw_streak'] = (st.get('nw_streak', 0) + 1)
            elif v.startswith(('EXC', 'DRAG-ERR', 'NO-VERDICT')) or 'Timeout' in v:
                st['err_streak'] += 1
            rotate_now = (v == 'PAGE-BLANK' or quota or st['f_streak'] >= F_WALL
                          or st.get('nw_streak', 0) >= 2 or st['err_streak'] >= 2
                          or st['signups_this_proxy'] >= 16)
            if rotate_now:
                log(f'rotate proxy ({v} quota={quota} f={st["f_streak"]} nw={st.get("nw_streak", 0)} '
                    f'err={st["err_streak"]} signups={st["signups_this_proxy"]})')
                # 页面/基础设施烂 → 记入 bad，run 内不再回头；quota/F 墙只是额度问题不记
                bad = st.setdefault('bad_proxies', [])
                if v in ('PAGE-BLANK', 'NO-WIDGET') or (v.startswith('EXC') and st['err_streak'] >= 2):
                    if proxies[idx]['url'] not in bad:
                        bad.append(proxies[idx]['url'])
                for _ in range(len(proxies)):
                    idx = (idx + 1) % len(proxies)
                    if proxies[idx]['url'] not in bad:
                        break
                else:
                    log('all proxies marked bad → refetch pool')
                    proxies = load_proxies(refresh=True)
                    bad.clear()
                    idx = 0
                st['proxy_idx'] = idx
                st['f_streak'] = 0
                st['nw_streak'] = 0
                st['err_streak'] = 0
                st['signups_this_proxy'] = 0
                start_stack(proxies[idx]['url'])
            elif st['err_streak'] >= ERR_WALL:
                log('err streak → restart stack')
                start_stack(proxies[idx]['url'])
                st['err_streak'] = 0
            elif not v.startswith(('EXC', 'DRAG-ERR', 'NO-VERDICT', 'NO-WIDGET', 'PAGE-BLANK')):
                st['err_streak'] = max(0, st.get('err_streak', 0) - 1)
        save_state(st)
        time.sleep(0.3)

    log(f'=== done: {st["ok"]}/{TARGET} (pending_act={len(st.get("pending_act", []))}) ===')
    save_state(st)
    _kill_stack()


if __name__ == '__main__':
    try:
        main()
    finally:
        _kill_stack()
