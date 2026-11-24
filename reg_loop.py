#!/usr/bin/env python3
"""注册流水线单轮：新题 → 洞检测 → dragto(g=0.99) → verdict。可连跑 N 轮看通过率。"""
import io
import json
import sys
import time
import urllib.request

import numpy as np
from PIL import Image

PORT = 19223
import os as _os
PW = _os.environ.get('ZREG_PW', '')                      # 账号统一密码：secrets
EMAIL_DOMAIN = _os.environ.get('ZREG_EMAIL_DOMAIN', '')  # 收信域名：secrets
# 显式无代理 opener：免疫任何 http_proxy/https_proxy env（CDP 本地请求不该被劫持）
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def ev(expr, timeout=25):
    rq = urllib.request.Request(f'http://127.0.0.1:{PORT}/eval',
        data=json.dumps({'expr': expr}).encode(), headers={'Content-Type': 'application/json'})
    return json.loads(OP.open(rq, timeout=timeout).read())['value']


def state():
    return json.loads(ev("""(()=>JSON.stringify({
      slider:(s=>s&&s.getBoundingClientRect().width>5?{x:s.getBoundingClientRect().x,y:s.getBoundingClientRect().y,w:s.getBoundingClientRect().width}:null)(document.querySelector('#aliyunCaptcha-sliding-slider')),
      bg:(b=>b&&b.naturalWidth>50?{x:b.getBoundingClientRect().x,w:b.getBoundingClientRect().width,nw:b.naturalWidth,src:b.src}:null)(document.querySelector('#aliyunCaptcha-img'))
    }))()"""))


def hole(st):
    """洞检测：blur-NCC 主估计（亚像素抛物线插值）+ 亮区法对照。
    两个估计差 >6px 说明某一方法失效，取 blur-NCC。"""
    op = urllib.request.build_opener()
    op.addheaders = [('User-Agent', 'Mozilla/5.0'), ('Referer', 'https://chat.z.ai/')]
    bgu = st['bg']['src']
    if bgu.startswith('data:'):
        import base64 as _b64
        bg = Image.open(io.BytesIO(_b64.b64decode(bgu.split(',', 1)[1])))
    else:
        bg = Image.open(io.BytesIO(op.open(bgu, timeout=20).read()))
    pcu = ev("(document.querySelector('#aliyunCaptcha-puzzle')||{src:''}).src") or (bgu.rsplit('/', 1)[0] + '/shadow.png')
    if pcu.startswith('data:'):
        import base64 as _b64
        pc = Image.open(io.BytesIO(_b64.b64decode(pcu.split(',', 1)[1])))
    else:
        pc = Image.open(io.BytesIO(op.open(pcu, timeout=20).read()))
    rgb = np.asarray(bg.convert('RGB'), dtype=np.float64)
    pca = np.asarray(pc.convert('RGBA'), dtype=np.float64)
    m = pca[:, :, 3] > 128
    ys, xs = np.where(m)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    sub = m[y0:y1, x0:x1]
    ph, pw = sub.shape

    def blur(x, k=2):
        for _ in range(k):
            for axis in (0, 1):
                x = (np.roll(x, 1, axis) + x + np.roll(x, -1, axis)) / 3
        return x

    g = blur(rgb.mean(axis=2))
    p = blur(pca[y0:y1, x0:x1, :3].mean(axis=2))
    p_ = p[sub] - p[sub].mean()
    pn = np.sqrt((p_ ** 2).sum())
    scores = {}
    for ox in range(0, rgb.shape[1] - pw):
        w = g[y0:y0 + ph, ox:ox + pw][sub]
        w_ = w - w.mean()
        scores[ox] = (w_ * p_).sum() / (np.sqrt((w_ ** 2).sum()) * pn + 1e-9)
    bx = max(scores, key=scores.get)
    l_, c_, r_ = scores.get(bx - 1, scores[bx]), scores[bx], scores.get(bx + 1, scores[bx])
    den = (l_ - 2 * c_ + r_)
    delta = 0.5 * (l_ - r_) / den if abs(den) > 1e-9 else 0.0
    delta = max(-0.5, min(0.5, delta))
    return bx + delta, x0


def open_challenge(email=None, name=None):
    time.sleep(1.2)
    if email is None:
        email = 'u' + format(int(time.time() * 1000), 'x') + '@' + EMAIL_DOMAIN
    if name is None:
        name = 'u' + format(int(time.time() * 1000), 'x')
    js = """(()=>{const set=(sel,v)=>{const el=document.querySelector(sel);if(!el)return 0;el.focus();const d=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;d.call(el,v);el.dispatchEvent(new Event('input',{bubbles:true}));return 1};
      set('form input[autocomplete="name"]','__NAME__');set('form input[name="email"]','__EMAIL__');set('form input[name="new-password"]',__PW__);return 1})()"""
    ev(js.replace('__NAME__', name).replace('__EMAIL__', email).replace('__PW__', PW))
    return email
    time.sleep(0.5)
    ev("""(()=>{const b=[...document.querySelectorAll("form button")].find(b=>/创建账号/.test(b.textContent||''));if(b)b.click();return 1})()""")
    time.sleep(3.5)


def one(trial):
    ev('location.href = "https://chat.z.ai/auth?redirect_uri=fuckzaicn"; "r"')
    time.sleep(6.5)
    # 登录页 → 点“注册”切到注册表单
    ev("""(()=>{const b=[...document.querySelectorAll("a,button")].find(b=>b.textContent.trim()==="注册");if(b)b.click();return 1})()""")
    time.sleep(1.5)
    open_challenge()
    st = state()
    if not st.get('slider'):
        ev("""(()=>{const b=document.querySelector('#aliyunCaptcha-captcha-body button')||document.querySelector('#aliyunCaptcha-captcha-body');if(b)b.click();return 1})()""")
        time.sleep(3)
        st = state()
    if not (st.get('slider') and st.get('bg')):
        print(f'[{trial}] no widget'); return False
    try:
        hx, x0 = hole(st)
    except Exception as e:
        print(f'[{trial}] hole err {repr(e)[:60]}'); return False
    target = st['bg']['x'] + (hx - x0) * (st['bg']['w'] / st['bg']['nw'])
    rq = urllib.request.Request(f'http://127.0.0.1:{PORT}/dragto3', data=json.dumps({
        'sx': st['slider']['x'] + 20, 'sy': st['slider']['y'] + 20,
        'target_x': round(target, 1)}).encode(), headers={'Content-Type': 'application/json'})
    try:
        r = json.loads(OP.open(rq, timeout=300).read())
    except urllib.error.HTTPError as e:
        print(f'[{trial}] dragto 500 {e.read().decode()[:120]}'); return False
    print(f"[{trial}] hole={hx} verdict={r.get('verdict')} final={r.get('final_piece')} need={r.get('need')} err={r.get('final_err')}", flush=True)
    if r.get('verdict') != 'T001':
        return False
    # 验证通过 → 点创建账号 → 捕获 signup 响应
    ev("""(()=>{window.__signup=null;const of=window.fetch;window.fetch=async function(...a){const u=String(a[0]||"");const r=await of.apply(this,a);try{if(/auths\\/signup/.test(u)){const c=await r.clone().text();window.__signup={st:r.status,body:c.slice(0,200)}}}catch(e){};return r};return 1})()""")
    ev("""(()=>{const b=[...document.querySelectorAll("form button")].find(b=>/创建账号/.test(b.textContent||""));if(b)b.click();return 1})()""")
    for _ in range(12):
        time.sleep(1)
        s = ev('JSON.stringify(window.__signup||null)')
        if s != 'null':
            break
    print(f'  signup={s}', flush=True)
    try:
        sd = json.loads(s)
        return sd.get('st') == 200 and 'success":true' in (sd.get('body') or '')
    except Exception:
        return False


if __name__ == '__main__':
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    ok = 0
    for i in range(n):
        if one(i):
            ok += 1
    print(f'RESULT {ok}/{n}')
