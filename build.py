#!/usr/bin/env python3
"""
Launchscope: independent ratings for new token launches.
Grades every fresh launch (ETH, Base, BSC, Solana) in its first hours, then
tracks what happened to it. The "rug rate by grade" table is the proof the
grades work.

Sources (all free, no key):
  GeckoTerminal  new pools + pool re-checks (liquidity, FDV, volume, flows)
  GoPlus         contract security (honeypot, mint, owner, taxes, holders)
  Solana RPC     holder concentration for Solana mints

Usage: python3 build.py      -> data.json, terminal.html, snapshots/
"""
import json, os, time, math, glob, datetime, urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP = os.path.join(HERE, "snapshots")
GT = "https://api.geckoterminal.com/api/v2"
GP = "https://api.gopluslabs.io/api/v1"
SOL_RPC = "https://api.mainnet-beta.solana.com"
NETS = {"eth": 1, "base": 8453, "bsc": 56, "solana": None}   # GT id -> GoPlus chain id
NET_LABEL = {"eth": "Ethereum", "base": "Base", "bsc": "BNB Chain", "solana": "Solana"}
PER_NET = 15                       # newest launches rated per network per run
SKIP_SYMBOLS = {"WETH", "ETH", "SOL", "WSOL", "USDC", "USDT", "WBNB", "BNB", "DAI", "USD1", "CBBTC", "WBTC"}
DEAD = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}
RUG_DROP = 0.90                    # liquidity down >= 90% vs rating time = rugged
MIN_AGE_H_OUTCOME = 24             # judge outcomes only after 24h

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "launchscope/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def _post(url, payload, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "launchscope/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def gt_get(path, tries=3):
    for i in range(tries):
        try:
            d = _get(GT + path); time.sleep(2.2); return d   # stay under 30 calls/min
        except urllib.error.HTTPError as e:
            if e.code == 429: time.sleep(20); continue
            return None
        except Exception:
            time.sleep(3)
    return None

def f(x, d=0.0):
    try: return float(x)
    except (TypeError, ValueError): return d

# ---------- 1. FETCH NEW LAUNCHES ----------
def fetch_launches():
    out = []
    for net in NETS:
        d = gt_get(f"/networks/{net}/new_pools?page=1&include=base_token")
        if not d: print(f"  ! {net}: no data"); continue
        toks = {t["id"]: t["attributes"] for t in d.get("included", []) if t.get("type") == "token"}
        seen = set(); n = 0
        for p in d.get("data", []):
            a = p["attributes"]; bt = p["relationships"]["base_token"]["data"]["id"]
            addr = bt.split("_", 1)[1]
            ta = toks.get(bt, {})
            sym = (ta.get("symbol") or a.get("name", "?").split("/")[0]).strip()
            if sym.upper() in SKIP_SYMBOLS or addr in seen: continue
            seen.add(addr)
            tx = D(D(a.get("transactions")).get("h24"))
            out.append({
                "net": net, "chain": NET_LABEL[net], "token": addr, "pool": a["address"],
                "symbol": sym[:16], "name": (ta.get("name") or a.get("name", ""))[:40],
                "created": a.get("pool_created_at"),
                "liq": f(a.get("reserve_in_usd")), "fdv": f(a.get("fdv_usd")),
                "mcap": f(a.get("market_cap_usd")),
                "vol24": f(D(a.get("volume_usd")).get("h24")),
                "buys": int(tx.get("buys") or 0), "sells": int(tx.get("sells") or 0),
                "chg1h": f(D(a.get("price_change_percentage")).get("h1")),
            })
            n += 1
            if n >= PER_NET: break
        print(f"  {net}: {n} launches")
    return out

# ---------- 2. SECURITY X-RAY ----------
def goplus_evm(chain_id, addr):
    try:
        d = _get(f"{GP}/token_security/{chain_id}?contract_addresses={addr}")
        time.sleep(2.1)
        r = d.get("result") if isinstance(d, dict) else None
        if not isinstance(r, dict) or not r: return None
        v = r.get(addr.lower()) or list(r.values())[0]
        return v if isinstance(v, dict) else None
    except Exception:
        time.sleep(2.1); return None

def goplus_sol(addr):
    try:
        d = _get(f"{GP}/solana/token_security?contract_addresses={addr}")
        time.sleep(2.1)
        r = d.get("result") if isinstance(d, dict) else None
        if not isinstance(r, dict) or not r: return None
        v = r.get(addr) or list(r.values())[0]
        return v if isinstance(v, dict) else None
    except Exception:
        time.sleep(2.1); return None

def sol_top_holders(mint):
    """Top-10 share of supply excluding the single largest account (usually the pool / bonding curve)."""
    try:
        sup = _post(SOL_RPC, {"jsonrpc": "2.0", "id": 1, "method": "getTokenSupply", "params": [mint]})
        total = f(sup["result"]["value"]["uiAmount"])
        time.sleep(0.6)
        big = _post(SOL_RPC, {"jsonrpc": "2.0", "id": 1, "method": "getTokenLargestAccounts", "params": [mint]})
        time.sleep(0.6)
        amts = sorted((f(x.get("uiAmount")) for x in big["result"]["value"]), reverse=True)
        if not total or len(amts) < 2: return None
        return round(sum(amts[1:11]) / total * 100, 1)
    except Exception:
        return None

def is1(r, k): return str(r.get(k, "0")) == "1"

# ---------- 3. SCORING ----------
def score_evm(L, r):
    flags, hard = [], []
    if r is None:
        return 0, ["No security data (contract unverifiable)"], ["unverifiable"], {}
    for k, msg in [("is_honeypot", "Honeypot: you cannot sell"), ("cannot_buy", "Buying blocked"),
                   ("cannot_sell_all", "Cannot sell full balance"), ("hidden_owner", "Hidden owner"),
                   ("can_take_back_ownership", "Owner can reclaim ownership"), ("selfdestruct", "Self-destruct in code"),
                   ("owner_change_balance", "Owner can change balances"), ("honeypot_with_same_creator", "Creator made a honeypot before")]:
        if is1(r, k): hard.append(msg)
    bt, st = f(r.get("buy_tax")), f(r.get("sell_tax"))
    if bt > 0.10 or st > 0.10: hard.append(f"Tax {bt*100:.0f}% buy / {st*100:.0f}% sell")
    # contract safety /40
    c = 40
    for k, pen, msg in [("is_open_source", 0, ""), ("is_proxy", 10, "Upgradeable proxy: code can change"),
                        ("is_mintable", 10, "Mintable supply"), ("transfer_pausable", 8, "Transfers can be paused"),
                        ("is_blacklisted", 5, "Blacklist function"), ("slippage_modifiable", 8, "Owner can change tax"),
                        ("personal_slippage_modifiable", 5, "Per-wallet tax control"), ("external_call", 4, "External calls"),
                        ("trading_cooldown", 3, "Trading cooldown")]:
        if k == "is_open_source":
            if not is1(r, k): c -= 15; flags.append("Code not verified")
        elif is1(r, k): c -= pen; flags.append(msg)
    owner = (r.get("owner_address") or "").lower()
    if owner and owner not in DEAD: c -= 5; flags.append("Ownership not renounced")
    c = max(c, 0)
    # holders /25
    top = 0.0
    for h in [h for h in (r.get("holders") or []) if isinstance(h, dict)][:15]:
        a = (h.get("address") or "").lower()
        if a in DEAD or str(h.get("is_locked")) == "1" or str(h.get("is_contract")) == "1": continue
        top += f(h.get("percent")) * 100
    top = min(top, 100)
    hl = r.get("holders") or []
    if not hl:
        top = None; hs = 10; flags.append("Holder list not available yet")
    else:
        hs = 25 if top < 20 else max(0, 25 * (80 - top) / 60)
        if top >= 80: hard.append(f"Top wallets hold {top:.0f}% (dump risk)")
        elif top >= 50: flags.append(f"Top wallets hold {top:.0f}%")
    cp = f(r.get("creator_percent")) * 100
    if cp >= 50: hard.append(f"Creator holds {cp:.0f}% of supply (dump risk)")
    elif cp >= 20: hs -= 10; flags.append(f"Creator holds {cp:.0f}%")
    elif cp >= 10: hs -= 5; flags.append(f"Creator holds {cp:.0f}%")
    hs = max(hs, 0)
    locked = any(str(x.get("is_locked")) == "1" for x in (r.get("lp_holders") or []) if isinstance(x, dict))
    return c, flags, hard, {"top_pct": (round(top, 1) if top is not None else None), "creator_pct": round(cp, 1), "lp_locked": locked,
                            "holders": int(f(r.get("holder_count"))), "hs": hs}

def score_sol(L, r, top):
    flags, hard = [], []
    if r is None:
        return 0, ["No security data (mint unverifiable)"], ["unverifiable"], {}
    st = lambda k: str(D(r.get(k)).get("status", "0")) == "1"
    if st("freezable"): hard.append("Freeze authority: your tokens can be frozen")
    if st("balance_mutable_authority"): hard.append("Authority can change balances")
    if str(r.get("non_transferable", "0")) == "1": hard.append("Non-transferable token")
    c = 40
    if st("mintable"): c -= 15; flags.append("Mint authority live")
    if st("closable"): c -= 5; flags.append("Accounts closable")
    if st("metadata_mutable"): c -= 3; flags.append("Metadata mutable")
    if r.get("transfer_fee"): c -= 5; flags.append("Transfer fee")
    if st("transfer_hook"): c -= 5; flags.append("Transfer hook")
    c = max(c, 0)
    if top is None:
        hs = 10; flags.append("Holder data unavailable")
    else:
        hs = 25 if top < 20 else max(0, 25 * (80 - top) / 60)
        if top >= 80: hard.append(f"Top wallets hold {top:.0f}% (dump risk)")
        elif top >= 50: flags.append(f"Top wallets hold {top:.0f}%")
    return c, flags, hard, {"top_pct": top, "creator_pct": None, "lp_locked": None, "holders": None, "hs": hs}

def market_score(L, ext):
    flags = []; m = 0
    liq = L["liq"]
    m += 0 if liq < 5_000 else 12 if liq >= 250_000 else 12 * math.log10(liq / 5_000) / math.log10(50)
    if liq < 10_000: flags.append(f"Thin liquidity ${liq:,.0f}")
    ratio = liq / L["fdv"] if L["fdv"] > 0 else 0
    m += 6 if ratio >= .10 else 4 if ratio >= .05 else 2 if ratio >= .02 else 0
    if 0 < ratio < .02: flags.append(f"Liquidity only {ratio*100:.1f}% of FDV")
    b, s = L["buys"], L["sells"]
    if s == 0 and b >= 20: flags.append("Buys but zero sells (sell-block pattern)")
    elif s and 0.5 <= b / s <= 3: m += 4
    if L["liq"] > 0 and 0 < L["vol24"] / L["liq"] <= 20: m += 3
    # track record /10
    age_h = hours_since(L["created"])
    t = 2 if age_h < 1 else 5 if age_h < 24 else 8
    if ext.get("lp_locked"): t += 2
    return min(m, 25), min(t, 10), flags, age_h

def hours_since(iso):
    try:
        t = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (datetime.datetime.now(datetime.timezone.utc) - t).total_seconds() / 3600
    except Exception:
        return 0

ORDER = "ABCDF"
def grade(score, hard, L=None):
    if hard: return "F"
    g = "A" if score >= 80 else "B" if score >= 65 else "C" if score >= 50 else "D" if score >= 35 else "F"
    if L:  # you cannot exit a thin pool: liquidity caps the grade
        tp = L.get("top_pct")
        if tp is not None and tp >= 50: g = max(g, "D", key=ORDER.index)
        cap = "D" if L["liq"] < 10_000 else "C" if L["liq"] < 50_000 else "B" if (L["fdv"] > 0 and L["liq"] / L["fdv"] < .02) else "A"
        g = max(g, cap, key=ORDER.index)
    return g

def D(x):
    return x if isinstance(x, dict) else {}

def rate(L):
    if L["net"] == "solana":
        r = goplus_sol(L["token"]); top = sol_top_holders(L["token"])
        c, fl, hard, ext = score_sol(L, r, top)
    else:
        r = goplus_evm(NETS[L["net"]], L["token"])
        c, fl, hard, ext = score_evm(L, r)
    m, t, mfl, age = market_score(L, ext)
    pending = "unverifiable" in hard
    if L["liq"] <= 0 and not pending: hard.append("No liquidity: cannot exit")
    score = 0 if pending else round(c + ext.get("hs", 0) + m + t)
    if pending:
        g = "NR"; hard = []; fl = ["Not yet indexed by the security scanner: re-rated next run"]
    else:
        L["top_pct"] = ext.get("top_pct")
        g = grade(score, hard, L)
        if g == "F" and not hard:
            hard = [f"Low overall score {score}/100: " + ", ".join((fl + mfl)[:2])]
    L.update({"score": score, "grade": g, "hard": hard, "flags": fl + mfl,
              "safety": round(c), "holder_score": round(ext.get("hs", 0)), "market": round(m), "track": round(t),
              "top_pct": ext.get("top_pct"), "creator_pct": ext.get("creator_pct"),
              "lp_locked": ext.get("lp_locked"), "holders": ext.get("holders"), "age_h": round(age, 1)})
    return L

# ---------- 4. OUTCOME TRACKER (the proof) ----------
def outcomes(now_tokens):
    """Re-check every pool rated >= 24h ago. Rugged = liquidity down >= 90% (or pool gone)."""
    rated = {}
    for fp in sorted(glob.glob(os.path.join(SNAP, "launch_*.ndjson"))):
        for line in open(fp, encoding="utf-8"):
            r = json.loads(line)
            key = (r["net"], r["pool"])
            if key not in rated: rated[key] = r          # keep FIRST rating (no hindsight)
    due = [r for r in rated.values() if hours_since(r["rated_at"]) >= MIN_AGE_H_OUTCOME]
    status_path = os.path.join(SNAP, "outcomes.json")
    known = json.load(open(status_path)) if os.path.exists(status_path) else {}
    todo = [r for r in due if f"{r['net']}:{r['pool']}" not in known]
    by_net = {}
    for r in todo: by_net.setdefault(r["net"], []).append(r)
    for net, rows in by_net.items():
        for i in range(0, len(rows), 30):
            chunk = rows[i:i + 30]
            d = gt_get(f"/networks/{net}/pools/multi/{','.join(x['pool'] for x in chunk)}")
            live = {p["attributes"]["address"]: f(p["attributes"].get("reserve_in_usd")) for p in (d or {}).get("data", [])}
            for x in chunk:
                now = live.get(x["pool"])
                base = x["liq"] or 0
                rug = (now is None and d is not None) or (base > 0 and now is not None and now <= base * (1 - RUG_DROP))
                known[f"{net}:{x['pool']}"] = {"grade": x["grade"], "liq0": base, "liq_now": now,
                                               "rugged": bool(rug), "checked": now_iso(), "symbol": x["symbol"]}
    json.dump(known, open(status_path, "w"), indent=1)
    table = {}
    for v in known.values():
        g = table.setdefault(v["grade"], {"n": 0, "rug": 0})
        g["n"] += 1; g["rug"] += v["rugged"]
    return {g: {"n": t["n"], "rug": t["rug"], "rate": round(t["rug"] / t["n"] * 100, 1) if t["n"] else None}
            for g, t in sorted(table.items())}, len(known)

def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

def main():
    os.makedirs(SNAP, exist_ok=True)
    print("-> fetching new launches")
    L = fetch_launches()
    print(f"-> rating {len(L)} launches (security X-ray)")
    rated = []
    for x in L:
        try: rated.append(rate(x))
        except Exception as e:
            x.update({"score": 0, "grade": "NR", "hard": [], "flags": [f"Rating error, retried next run"], "safety": 0,
                      "holder_score": 0, "market": 0, "track": 0, "top_pct": None, "creator_pct": None,
                      "lp_locked": None, "holders": None, "age_h": round(hours_since(x.get("created")), 1)})
            rated.append(x)
    stamp = now_iso()
    for r in rated: r["rated_at"] = stamp
    day = stamp[:13].replace(" ", "_").replace(":", "")
    with open(os.path.join(SNAP, f"launch_{day}.ndjson"), "w", encoding="utf-8") as fh:
        for r in rated:
            if r["grade"] != "NR": fh.write(json.dumps(r) + "\n")
    print("-> checking outcomes of past ratings")
    proof, n_tracked = outcomes(rated)
    grades = {g: sum(1 for r in rated if r["grade"] == g) for g in ["A","B","C","D","F","NR"]}
    ds = {"generated": stamp, "launches": sorted(rated, key=lambda r: -r["score"]),
          "grades": grades, "proof": proof, "n_tracked": n_tracked,
          "n_snapshots": len(glob.glob(os.path.join(SNAP, "launch_*.ndjson"))),
          "networks": sorted({r["chain"] for r in rated})}
    json.dump(ds, open(os.path.join(HERE, "data.json"), "w"), indent=1)
    tpl = open(os.path.join(HERE, "template.html"), encoding="utf-8").read()
    html = tpl.replace("/*__DATA__*/", "window.DATA = " + json.dumps(ds) + ";")
    for fn in ("terminal.html", "index.html"):
        open(os.path.join(HERE, fn), "w", encoding="utf-8").write(html)
    print(f"-> {len(rated)} rated · grades {grades} · tracked {n_tracked} · proof {proof}")
    print("done", stamp)

if __name__ == "__main__":
    main()
