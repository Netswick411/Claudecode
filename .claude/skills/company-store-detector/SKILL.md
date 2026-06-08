---
name: company-store-detector
description: >-
  Detect whether companies operate an internal employee-facing "company store" — a
  separate, cart-enabled online shop (almost always Shopify, often password/login-gated)
  that sells branded merch/swag to staff. Use when given a CSV of companies + websites
  and asked to flag which ones run a dedicated store (store./shop./swag. subdomain,
  /store path, or *.myshopify.com). Deterministic HTTP + DNS only — no per-row LLM calls.
---

# Company Store Detector

Deterministically classify each company in a CSV as operating an employee-facing
company store (Yes/No), with the store URL and the signal that decided it.

## What counts as a company store

A **separate** store URL that is NOT the main marketing site:

- a store-style subdomain — `store.company.com`, `shop.company.com`, `swag.company.com`,
  `merch.company.com`, `gear.company.com`
- a store path — `company.com/store`, `/shop`, `/swag`
- a `*.myshopify.com` site

It must be a dedicated, cart-enabled storefront. **False-positive rule:** the company's
main website merely being built on Shopify does **not** count. Login/password-gating on a
*store* URL is a **strong** positive (employee-only store). When unsure → **No**.

## How it decides (deterministic signals)

**DNS** (reliable; works even when the store is password-gated):
- store-subdomain CNAME → `*.myshopify.com`  → dedicated Shopify store (strong)
- store-subdomain A record in Shopify's edge block `23.227.38.0/24` (strong)
- store-subdomain CNAME → a known swag/merch platform (brilliantmade, swag.com, printful, …)

**HTTP** (richer; needs open outbound egress to the target host):
- store URL `/products.json` returns Shopify product JSON (cart-enabled) — definitive
- store URL `/cart.js` returns Shopify cart JSON
- Shopify response headers (`x-shopid`, `x-shopify-stage`, `x-sorting-hat-shopid`, …)
- Shopify body markers (`cdn.shopify.com`, `/cdn/shop/`, `Shopify.theme`, `myshopify.com`)
- Shopify password/login page on a store URL → employee-only store (strong)

A guessed `<label>.myshopify.com` is **never** a DNS-only positive (`*.myshopify.com` is a
wildcard that resolves for every label) — it requires HTTP confirmation via `/products.json`.

## Usage

```bash
pip install requests dnspython

# 1) Check which channels are live in this environment (HTTP may be allowlist-blocked):
python3 scripts/detect_company_stores.py --check-egress

# 2) Spot-check the first 50 rows:
python3 scripts/detect_company_stores.py "Account Export-Jun 08.csv" -n 50 -o sample_50.csv

# 3) Full run (~60 concurrent workers, incremental crash-safe writes):
python3 scripts/detect_company_stores.py "Account Export-Jun 08.csv" -o accounts_with_stores.csv
```

Input CSV needs a `Company Name` and a `Website` column (bare domains like `fieldcore.com`
are fine). Output preserves all original columns and appends:
`Company Store?` (Yes/No), `Store URL`, `Signal`.

## Environment note (restricted egress)

In sandboxes with an HTTP allowlist (proxy returns `403 x-deny-reason: host_not_allowed`),
the HTTP channel is unavailable. The script auto-detects this, falls back to **DNS-only**
signals, and labels the run accordingly. DNS-only still reliably catches the most common
employee-store pattern (a `store.`/`shop.`/`swag.` subdomain pointed at Shopify or a swag
platform). For full HTTP coverage (path-based stores, guessed myshopify shops, body/header
markers), run in an environment with open outbound egress.
