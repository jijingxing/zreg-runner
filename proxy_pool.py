#!/usr/bin/env python3
"""proxy_pool.py — 免費代理池：抓取 + 驗活（能通 chat.z.ai 才留）。
用法: python3 proxy_pool.py [--n 25] [--out proxies.json]
輸出: [{url, latency_ms, ip}, ...] 按 latency 排序。
"""
import argparse
import json
import re
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SOURCES = [
    "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
    "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
    "https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list.txt",
    "https://raw.githubusercontent.com/mmpx12/proxy-list/master/https.txt",
    "https://raw.githubusercontent.com/TheSpeedX/SOCKS-List/master/http.txt",
    "https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt",
    "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt",
    "https://api.proxyscrape.com/v2/?request=getproxies&protocol=http&timeout=10000&country=all&ssl=all&anonymity=all",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/http/http.txt",
    "https://raw.githubusercontent.com/officialputuid/KangProxy/KangProxy/https/https.txt",
    "https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/generated/http_proxies.txt",
]
# clarketm 格式: ip:port 后面带元数据
PAT = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3}:\d{2,5})")
TEST_URL = "https://chat.z.ai/api/version"
AUTH_URL = "https://chat.z.ai/auth?redirect_uri=fuckzaicn"
TIMEOUT = 12


def fetch_list():
    out = set()
    for src in SOURCES:
        try:
            req = urllib.request.Request(src, headers={"User-Agent": "curl/8.0"})
            txt = urllib.request.urlopen(req, timeout=15).read().decode(errors="replace")
            out.update(PAT.findall(txt))
        except Exception as e:
            print(f"  src fail {src.split('/')[2]}: {repr(e)[:50]}")
    return sorted(out)


def check(proxy):
    """两段验活：① api/version 通；② auth 页 HTML 完整且首个 JS chunk 可达（SPA 可载性）。
    只过 ① 的代理一半是 NO-WIDGET 制造者。"""
    url = f"http://{proxy}"
    try:
        t0 = time.time()
        op = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": url, "https": url}))
        req = urllib.request.Request(TEST_URL, headers={"User-Agent": "curl/8.0"})
        with op.open(req, timeout=TIMEOUT) as r:
            body = r.read(200)
            if r.status != 200 or b"version" not in body and b"{" not in body:
                return None
        rq2 = urllib.request.Request(AUTH_URL, headers={"User-Agent": "curl/8.0"})
        html = op.open(rq2, timeout=TIMEOUT).read(60000).decode(errors="replace")
        if len(html) < 2000:
            return None
        m = (re.search(r'src="(/_next/static/[^"]+\.js)"', html)
             or re.search(r'src="(/[^"]+\.js)"', html))
        if m:
            rq3 = urllib.request.Request("https://chat.z.ai" + m.group(1), method="HEAD",
                                         headers={"User-Agent": "curl/8.0"})
            op.open(rq3, timeout=TIMEOUT).read(10)
        return {"url": url, "latency_ms": int((time.time() - t0) * 1000), "ip": proxy}
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=25)
    ap.add_argument("--out", default="proxies.json")
    ap.add_argument("--max-fetch", type=int, default=600)
    args = ap.parse_args()

    t0 = time.time()
    cand = fetch_list()
    print(f"fetched {len(cand)} candidates in {time.time()-t0:.1f}s")
    cand = cand[: args.max_fetch] if args.max_fetch > 0 else cand

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=80) as ex:
        results = [r for r in ex.map(check, cand) if r]
    results.sort(key=lambda x: x["latency_ms"])
    print(f"alive: {len(results)}/{len(cand)} in {time.time()-t0:.1f}s")
    for r in results[: args.n]:
        print(f"  {r['ip']:>22}  {r['latency_ms']}ms")
    json.dump(results[: args.n], open(args.out, "w"), indent=1)
    print(f"-> {args.out} ({min(len(results), args.n)} proxies)")


if __name__ == "__main__":
    main()
