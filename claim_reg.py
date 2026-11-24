#!/usr/bin/env python3
"""claim_reg.py — 批量补领 Start Plan（zcode-plan/billing/claim）。

collector 派发「无 active start plan」的号 → 本地 zclaim 页起 cn 区滑块 → dragd 拖 →
token → POST claim → balance 判定 → 回报 collector。

用法: python3 claim_reg.py [--lanes 4] [--batch 4]
每批（默认 4 个，配额 ~5/IP）后换代理出口；多 lane 共享 Xvfb，各自 chrome/dragd/profile/代理分片。
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
os.chdir(BASE)

import actions_reg as A  # noqa: E402


def pull(n):
    if not A.COLLECTOR_URL:
        A.log('no COLLECTOR_URL, cannot pull')
        return []
    url = A.COLLECTOR_URL.rstrip('/') + f'/pull?n={n}'
    try:
        rq = urllib.request.Request(url, headers={'Authorization': f'Bearer {A.COLLECTOR_TOKEN}'})
        d = json.loads(urllib.request.urlopen(rq, timeout=30).read())
        return d.get('batch') or []
    except Exception as e:
        A.log(f'pull fail: {repr(e)[:80]}')
        return []


INFRA = ('balance-exit-blocked', 'balance-unreachable', 'EXC', 'NO-WIDGET', 'widget-dead', 'ip-quota')


def zcode_ok(px):
    """出口预检：zcode.z.ai 不能回 405（WAF 拦数据中心/代理 IP）。无鉴权 GET 即可判断。"""
    import urllib.request as u
    try:
        op = u.build_opener(u.ProxyHandler({'http': px['url'], 'https': px['url']}))
        rq = u.Request('https://zcode.z.ai/api/v1/client/configs', headers={'User-Agent': 'ZCode/3.18.2'})
        with op.open(rq, timeout=12) as r:
            r.read(50)
            return r.status != 405
    except u.HTTPError as e:
        return e.code != 405
    except Exception:
        return False


def lane_main(batch, max_batches=0):
    proxies = A.load_proxies()
    if not proxies:
        A.log('no proxies, exit')
        return 1
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(16) as ex:
        keep = [p for p, ok in zip(proxies, ex.map(zcode_ok, proxies)) if ok]
    A.log(f'zcode.z.ai-reachable exits: {len(keep)}/{len(proxies)}')
    proxies = keep or proxies
    idx, total, idle, batches = 0, 0, 0, 0
    if not A.start_stack(proxies[idx]['url']):
        return 1
    while True:
        # 出口不可用（dragd/chrome 没起来）→ 换一个，别在死栈上领
        for _ in range(min(8, len(proxies))):
            if A.stack_healthy():
                break
            idx = (idx + 1) % len(proxies)
            A.start_stack(proxies[idx]['url'])
        tasks = pull(batch)
        if not tasks:
            idle += 1
            if idle >= 2:
                A.log('queue empty → done')
                break
            continue
        idle = 0
        batches += 1
        if max_batches and batches > max_batches:
            A.log(f'max-batches {max_batches} reached → done')
            break
        for acc in tasks:
            try:
                act, why = A.claim_plan(acc['jwt'], acc['mid'], proxies[idx]['url'],
                                        alt_proxies=[p['url'] for p in proxies if p['url'] != proxies[idx]['url']])
            except Exception as e:
                d = A._procs.get('dragd'); ch = A._procs.get('chrome')
                import subprocess as sp
                try:
                    ss = sp.run(['ss', '-tln'], capture_output=True, text=True, timeout=5).stdout
                    lst = [l.split()[3] for l in ss.splitlines() if f':{A.DRAGD_PORT} ' in l or f':{A.CDP_PORT} ' in l]
                except Exception:
                    lst = ['ss-fail']
                try:
                    dtail = open(f'{A.BASE}/dragd.log', 'rb').read()[-300:].decode(errors='replace').replace('\n', ' | ')
                except Exception:
                    dtail = 'no-dragd.log'
                act, why = False, (f'EXC {repr(e)[:50]} | dragd.poll={d and d.poll()} chrome.poll={ch and ch.poll()} '
                                   f'listen={lst} dragd.log={dtail}')
            # 出口/基础设施问题不是号的错：只释放租约让它换个出口重试，不计失败
            infra = (not act) and any(why.startswith(k) for k in INFRA)
            A.push_collector('plan', acc['name'], {'active': bool(act), 'why': why, 'retry': infra})
            total += 1 if act else 0
            A.log(f"{acc['name']}: PLAN {'ACTIVE ✓' if act else '✗'} ({why}) [{total} claimed]")
            if infra:
                break
        idx = (idx + 1) % len(proxies)          # 每批后换出口（~5 claim/IP）
        A.start_stack(proxies[idx]['url'])
    A.log(f'=== lane done: {total} claimed ===')
    A._kill_stack()
    return 0


def spawn_lanes(n, batch, max_batches=0):
    proxies = A.load_proxies(refresh=True)
    if not proxies:
        A.log('no proxies available, exit')
        return 1
    A.log(f'claim lanes={n} batch={batch} proxies={len(proxies)}')
    A.ensure_xvfb()                       # 父进程先起公共 Xvfb，lane 之间不再互相杀
    procs = []
    for k in range(n):
        lane_f = f'{BASE}/proxies_lane{k}.json'
        json.dump(proxies[k::n], open(lane_f, 'w'))
        env = dict(os.environ)
        env.update({'LANE_ID': str(k), 'CDP_PORT': str(9340 + k), 'DRAGD_PORT': str(19440 + k),
                    'ZREG_PROFILE_DIR': f'/tmp/zclaim-lane{k}-{os.getpid()}',
                    'LANE_PROXY_FILE': lane_f})
        procs.append(subprocess.Popen(
            [sys.executable, '-u', os.path.abspath(__file__), '--batch', str(batch), '--max-batches', str(max_batches), '--lane'],
            env=env, cwd=BASE))
    rc = 0
    for p in procs:
        rc |= p.wait()
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--lanes', type=int, default=4)
    ap.add_argument('--batch', type=int, default=4)
    ap.add_argument('--lane', action='store_true', help='internal: run as lane worker')
    ap.add_argument('--max-batches', type=int, default=0, help='debug: stop after N batches')
    a = ap.parse_args()
    if a.lane:
        sys.exit(lane_main(a.batch, a.max_batches))
    sys.exit(spawn_lanes(max(1, a.lanes), a.batch, a.max_batches))


if __name__ == '__main__':
    main()
