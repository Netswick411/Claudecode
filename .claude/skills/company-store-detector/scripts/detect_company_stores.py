#!/usr/bin/env python3
"""Deterministic company-store detector.

For each company (Company Name + bare-domain Website) decide whether it operates a
*separate* employee-facing online store (almost always Shopify, often login/password
gated) selling branded merch/swag.

DESIGN PRINCIPLES
-----------------
* Deterministic only. Pure HTTP + DNS + string/JSON checks. NO LLM/AI calls per row.
* A company store is a SEPARATE store URL: a store-style subdomain
  (store./shop./swag./merch./gear.company.com), a path (company.com/store), or a
  *.myshopify.com site. The company's main marketing site does NOT count, even if it
  happens to be built on Shopify.
* Login/password-gating on a *store* URL is a STRONG positive (employee-only store).
* When unsure, mark No.
* Concurrency, timeouts, and incremental writes (crash-safe; resumable).

SIGNAL CHANNELS
---------------
DNS (works even when the store is password-gated, and in restricted-egress envs):
  * A store-style subdomain on the company's own domain whose CNAME points to
    *.myshopify.com  -> dedicated Shopify store (STRONG).
  * ...whose A record is in Shopify's edge block 23.227.38.0/24 (STRONG).
  * ...whose CNAME points to a known swag/merch fulfillment platform (STRONG).
HTTP (richer, but requires open outbound egress to the target host):
  * Store URL /products.json returns valid Shopify product JSON (cart-enabled).
  * Store URL /cart.js returns Shopify cart JSON.
  * Shopify response headers (x-shopid, x-shopify-stage, x-sorting-hat-shopid, ...).
  * Shopify body markers (cdn.shopify.com, /cdn/shop/, Shopify.theme, myshopify.com).
  * Shopify password/login page on a store URL (STRONG: employee-only store).

NOTE: a guessed <label>.myshopify.com is NEVER a DNS-only positive, because
*.myshopify.com is a wildcard that resolves for every label whether or not a shop
exists -- it requires HTTP confirmation.

Restricted-egress environments: if HTTP requests come back as proxy denials
(403 + x-deny-reason, or connection refused/blocked), the script records that HTTP
is unavailable and relies on DNS signals alone. Run --check-egress to see which
channels are live before a big run.
"""

import argparse
import csv
import ipaddress
import json
import os
import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")
try:
    import dns.resolver
except ImportError:
    sys.exit("Missing dependency: pip install dnspython")

# --------------------------------------------------------------------------- config
STORE_SUBS = ["store", "shop", "swag", "merch", "gear", "teamstore", "companystore"]
STORE_PATHS = ["store", "shop", "swag", "merch", "gear", "company-store"]

# Shopify's well-known edge network (store.x.com A records land here when on Shopify).
SHOPIFY_NETS = [ipaddress.ip_network("23.227.38.0/24")]

# CNAME-target substrings that denote a dedicated branded-merch / swag storefront.
SWAG_PLATFORM_HINTS = [
    "myshopify.com",          # Shopify (handled explicitly too)
    "brilliantmade.com",      # corporate swag platform
    "swag.com", "swagup",     # swag platforms
    "printful.com", "printify.com",
    "customink", "bonfire", "threadless", "spreadshop", "spreadshirt",
    "fourthwall", "teespring", "spri.ng", "gemnote", "printfection",
    "merchology", "mythreadshop", "shopvida",
]

DEFAULT_TIMEOUT = 8
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*"}

SHOPIFY_HEADER_KEYS = {
    "x-shopid", "x-shopify-stage", "x-sorting-hat-shopid", "x-shardid",
    "x-sorting-hat-podid", "x-storefront-renderer-rendered",
}
SHOPIFY_BODY_MARKS = [
    "cdn.shopify.com", "/cdn/shop/", "shopify.theme", "myshopify.com",
    "shopify-section", "window.shopify", "x-shopify",
]

_resolver = dns.resolver.Resolver()
_resolver.lifetime = 6.0
_resolver.timeout = 4.0

# track whether HTTP egress looks blocked (proxy allowlist / sandbox)
_http_blocked = threading.Event()


# --------------------------------------------------------------------------- helpers
def norm_domain(website):
    """Bare registrable host from a Website cell (strip scheme/path/www)."""
    if not website:
        return ""
    w = website.strip().lower()
    for pre in ("https://", "http://"):
        if w.startswith(pre):
            w = w[len(pre):]
    w = w.split("/")[0].split("?")[0].split("#")[0].strip()
    if w.startswith("www."):
        w = w[4:]
    return w.strip(". ")


def dns_cname_chain(host):
    """Follow CNAMEs; return list of lowercased CNAME targets (no trailing dot)."""
    chain, cur, seen = [], host, set()
    for _ in range(6):
        if cur in seen:
            break
        seen.add(cur)
        try:
            ans = _resolver.resolve(cur, "CNAME")
        except Exception:
            break
        tgt = str(ans[0].target).rstrip(".").lower()
        chain.append(tgt)
        cur = tgt
    return chain


def dns_a_records(host):
    try:
        ans = _resolver.resolve(host, "A")
        return [str(r) for r in ans]
    except Exception:
        return []


def ip_in_shopify(ip):
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in SHOPIFY_NETS)


def http_get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True):
    """GET with proxy-block detection. Returns (resp, status_str).

    status_str in {'ok', 'blocked', 'error'}. 'blocked' => sandbox/proxy egress denial.
    """
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout,
                         allow_redirects=allow_redirects)
    except requests.exceptions.RequestException as e:
        msg = str(e).lower()
        if any(k in msg for k in ("not allowed", "forbidden by", "proxy", "tunnel")):
            _http_blocked.set()
            return None, "blocked"
        return None, "error"
    if r.status_code == 403 and r.headers.get("x-deny-reason"):
        _http_blocked.set()
        return None, "blocked"
    return r, "ok"


def shopify_http_signals(base_url):
    """Probe a candidate store base URL over HTTP. Returns (is_store, signal) or None.

    Distinguishes a real cart-enabled / password-gated Shopify storefront.
    """
    # 1) products.json -> definitive cart-enabled Shopify storefront
    r, st = http_get(base_url.rstrip("/") + "/products.json", allow_redirects=True)
    if st == "blocked":
        return None
    if r is not None and r.status_code == 200:
        ctype = r.headers.get("content-type", "")
        if "json" in ctype:
            try:
                data = r.json()
                if isinstance(data, dict) and "products" in data:
                    return True, "products.json (cart-enabled Shopify)"
            except ValueError:
                pass

    # 2) cart.js -> Shopify cart endpoint
    r, st = http_get(base_url.rstrip("/") + "/cart.js")
    if r is not None and r.status_code == 200 and "json" in r.headers.get("content-type", ""):
        try:
            data = r.json()
            if isinstance(data, dict) and ("token" in data or "items" in data):
                return True, "cart.js (Shopify cart endpoint)"
        except ValueError:
            pass

    # 3) homepage: headers / body markers / password page
    r, st = http_get(base_url, allow_redirects=True)
    if r is None:
        return None
    hdr_hit = SHOPIFY_HEADER_KEYS.intersection({k.lower() for k in r.headers})
    body = (r.text or "").lower()
    body_hit = [m for m in SHOPIFY_BODY_MARKS if m in body]
    is_password = ("/password" in r.url.lower()) or (
        r.status_code in (401, 403) and (hdr_hit or body_hit)
    ) or ("password" in body and "shopify" in body and "<form" in body)

    if is_password and (hdr_hit or body_hit):
        return True, "Shopify password/login page (employee-only store)"
    if hdr_hit:
        return True, "Shopify headers: " + ",".join(sorted(hdr_hit))
    if len(body_hit) >= 2:
        return True, "Shopify body markers: " + ",".join(body_hit[:3])
    return None


# --------------------------------------------------------------------------- per-company
def detect(company, website):
    """Return dict: store (bool), url (str), signal (str)."""
    domain = norm_domain(website)
    if not domain or "." not in domain:
        return {"store": False, "url": "", "signal": "no/invalid domain"}

    candidates = [f"{sub}.{domain}" for sub in STORE_SUBS]

    # ---- DNS pass (reliable; works for password-gated stores & restricted egress) ----
    for host in candidates:
        chain = dns_cname_chain(host)
        # CNAME -> myshopify
        for tgt in chain:
            if tgt.endswith(".myshopify.com") or tgt == "myshopify.com":
                return {"store": True, "url": f"https://{host}",
                        "signal": f"DNS CNAME {host} -> {tgt} (dedicated Shopify store)"}
        # CNAME -> known swag/merch platform
        for tgt in chain:
            for hint in SWAG_PLATFORM_HINTS:
                if hint != "myshopify.com" and hint in tgt:
                    return {"store": True, "url": f"https://{host}",
                            "signal": f"DNS CNAME {host} -> {tgt} (swag/merch platform)"}
        # A record in Shopify edge block
        if not chain:
            ips = dns_a_records(host)
        else:
            ips = dns_a_records(host)
        if any(ip_in_shopify(ip) for ip in ips):
            return {"store": True, "url": f"https://{host}",
                    "signal": f"DNS A {host} -> {next(ip for ip in ips if ip_in_shopify(ip))} (Shopify 23.227.38.0/24)"}

    # ---- HTTP pass (only meaningful with open egress) ----
    # Subdomain stores that resolve but DNS wasn't conclusive (e.g. non-Shopify CDN).
    for host in candidates:
        if not (dns_a_records(host) or dns_cname_chain(host)):
            continue  # subdomain doesn't exist; skip HTTP
        for scheme in ("https://", "http://"):
            res = shopify_http_signals(scheme + host)
            if res:
                return {"store": True, "url": scheme + host, "signal": "HTTP " + res[1]}
            if _http_blocked.is_set():
                break
        if _http_blocked.is_set():
            break

    # Path-based stores (HTTP only). Skip entirely when egress is blocked.
    if not _http_blocked.is_set():
        for path in STORE_PATHS:
            base = f"https://{domain}/{path}"
            res = shopify_http_signals(base)
            if res:
                # products.json/cart.js at the path is a strong separate-store signal
                return {"store": True, "url": base, "signal": "HTTP path " + res[1]}

    # ---- guessed myshopify shop: needs HTTP confirmation (wildcard DNS is not proof) ----
    if not _http_blocked.is_set():
        label = domain.split(".")[0]
        guess = f"https://{label}.myshopify.com"
        res = shopify_http_signals(guess)
        if res and "products.json" in res[1]:
            return {"store": True, "url": guess, "signal": "HTTP " + res[1]}

    return {"store": False, "url": "", "signal": "no separate store detected"}


# --------------------------------------------------------------------------- driver
def check_egress():
    print("Checking outbound channels...")
    r, st = http_get("https://store.hashicorp.com/", timeout=8)
    print(f"  HTTP to arbitrary host: {st}"
          + ("  (egress BLOCKED -> DNS-only mode)" if st == "blocked" else ""))
    try:
        chain = dns_cname_chain("store.hashicorp.com")
        print(f"  DNS CNAME lookup: {'ok' if chain else 'no-answer'} {chain}")
    except Exception as e:
        print(f"  DNS: error {e}")


def run(in_path, out_path, limit=None, workers=60):
    with open(in_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        rows = list(reader)
    if limit:
        rows = rows[:limit]

    name_col = "Company Name" if "Company Name" in fieldnames else fieldnames[0]
    site_col = "Website" if "Website" in fieldnames else None
    if site_col is None:
        for c in fieldnames:
            if "web" in c.lower() or "domain" in c.lower() or "url" in c.lower():
                site_col = c
                break
    out_fields = fieldnames + ["Company Store?", "Store URL", "Signal"]

    results = [None] * len(rows)
    done = 0
    lock = threading.Lock()

    def work(i, row):
        return i, detect(row.get(name_col, ""), row.get(site_col, "") if site_col else "")

    # incremental write: open output, write header, flush each completed row in order
    write_idx = 0
    with open(out_path, "w", newline="", encoding="utf-8") as out_f:
        writer = csv.DictWriter(out_f, fieldnames=out_fields)
        writer.writeheader()
        out_f.flush()
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(work, i, r) for i, r in enumerate(rows)]
            for fut in as_completed(futs):
                i, res = fut.result()
                results[i] = res
                with lock:
                    done += 1
                    # flush any contiguous completed prefix to keep file crash-safe & ordered
                    while write_idx < len(rows) and results[write_idx] is not None:
                        rr = dict(rows[write_idx])
                        rd = results[write_idx]
                        rr["Company Store?"] = "Yes" if rd["store"] else "No"
                        rr["Store URL"] = rd["url"]
                        rr["Signal"] = rd["signal"]
                        writer.writerow(rr)
                        write_idx += 1
                    out_f.flush()
                    if done % 25 == 0 or done == len(rows):
                        yes = sum(1 for r in results if r and r["store"])
                        sys.stderr.write(f"\r  {done}/{len(rows)} processed, {yes} stores"
                                         + ("  [HTTP blocked -> DNS-only]" if _http_blocked.is_set() else "")
                                         + "   ")
                        sys.stderr.flush()
    sys.stderr.write("\n")
    yes = sum(1 for r in results if r and r["store"])
    print(f"Done. {yes}/{len(rows)} companies flagged with a company store.")
    if _http_blocked.is_set():
        print("NOTE: HTTP egress was blocked in this environment; results are DNS-only "
              "(store-subdomain -> Shopify/swag). Path-based and guessed-myshopify stores "
              "could not be probed. Re-run with open egress for full HTTP coverage.")
    print(f"Output: {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Detect employee-facing company stores.")
    ap.add_argument("input", nargs="?", help="input CSV")
    ap.add_argument("-o", "--output", help="output CSV")
    ap.add_argument("-n", "--limit", type=int, default=None, help="only first N rows")
    ap.add_argument("-w", "--workers", type=int, default=60)
    ap.add_argument("--check-egress", action="store_true",
                    help="probe HTTP/DNS channels and exit")
    args = ap.parse_args()

    if args.check_egress:
        check_egress()
        return
    if not args.input:
        ap.error("input CSV required")
    out = args.output or (os.path.splitext(args.input)[0] + "_with_stores.csv")
    run(args.input, out, limit=args.limit, workers=args.workers)


if __name__ == "__main__":
    main()
