import os
import asyncio
import requests
import logging
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
ETHERSCAN_API_KEY  = os.getenv("ETHERSCAN_API_KEY",  "YOUR_ETHERSCAN_KEY")

MAX_RESULTS = 10
OWNER_ID    = 7525750969
whitelist   = set()

def is_allowed(uid): return uid == OWNER_ID or uid in whitelist

def fmt(n):
    if n is None: return "N/A"
    n = float(n)
    if n >= 1e9:  return f"{n/1e9:.2f}B"
    if n >= 1e6:  return f"{n/1e6:.2f}M"
    if n >= 1e3:  return f"{n/1e3:.2f}K"
    if n == 0:    return "N/A"
    return f"{n:.2f}"

def age(ts):
    if not ts: return "Unknown"
    s = int((datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds())
    y = s//(365*86400); s %= 365*86400
    mo= s//(30*86400);  s %= 30*86400
    d = s//86400;       s %= 86400
    h = s//3600;        s %= 3600
    m = s//60
    p = []
    if y:  p.append(f"{y}y")
    if mo: p.append(f"{mo}mo")
    if d:  p.append(f"{d}d")
    if h:  p.append(f"{h}h")
    p.append(f"{m}m")
    return " ".join(p) + " ago"

def dex_name(dex_id):
    d = dex_id.lower()
    if "v4" in d and "uniswap" in d: return "Uniswap V4"
    if "v3" in d and "uniswap" in d: return "Uniswap V3"
    if d == "uniswap" or ("v2" in d and "uniswap" in d): return "Uniswap V2"
    if "sushiswap" in d:  return "SushiSwap"
    if "pancakeswap" in d: return "PancakeSwap"
    return d.replace("-"," ").replace("_"," ").title()

def dex_matches_filter(dex_id, filt):
    d = dex_id.lower()
    if filt == "v2": return d == "uniswap" or "v2" in d
    if filt == "v3": return "v3" in d
    if filt == "v4": return "v4" in d
    return True

# ─── Search ───────────────────────────────────────────────────────────────────

def search_dexscreener(name):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={name}", timeout=10)
        r.raise_for_status()
        nl = name.lower().strip()
        return [p for p in (r.json().get("pairs") or [])
                if p.get("chainId","").lower() == "ethereum"
                and (p.get("baseToken",{}).get("symbol","").lower() == nl
                     or p.get("baseToken",{}).get("name","").lower() == nl)]
    except Exception as e:
        logger.error(f"DexScreener: {e}")
        return []

def search_geckoterminal(name):
    try:
        nl = name.lower().strip()
        all_pools = []
        for page in [1, 2]:
            try:
                r = requests.get("https://api.geckoterminal.com/api/v2/search/pools",
                    params={"query": name, "network": "eth", "page": page},
                    headers={"Accept": "application/json;version=20230302"}, timeout=10)
                r.raise_for_status()
                all_pools.extend(r.json().get("data") or [])
            except:
                pass
        out = []
        for pool in all_pools:
            a = pool.get("attributes", {})
            sym = a.get("base_token_symbol", "").lower()
            pn  = a.get("name", "").lower()
            liq = float(a.get("reserve_in_usd") or 0)
            if liq < 400: continue
            if sym != nl and pn.split(" / ")[0].strip() != nl: continue
            ta = pool.get("relationships",{}).get("base_token",{}).get("data",{}).get("id","").replace("eth_","")
            vol = a.get("volume_usd",{}) or {}
            out.append({
                "chainId":"ethereum","pairAddress":a.get("address",""),
                "baseToken":{"address":ta,"symbol":sym.upper(),"name":pn.split(" / ")[0].strip().title()},
                "quoteToken":{"symbol":"WETH"},"dexId":a.get("dex_id","unknown"),
                "priceUsd":str(a.get("base_token_price_usd") or 0),"fdv":a.get("fdv_usd"),
                "liquidity":{"usd":liq},
                "volume":{"m5":vol.get("m5"),"h1":vol.get("h1"),"h6":vol.get("h6"),"h24":vol.get("h24")},
                "txns":{"h24":{"buys":0,"sells":0}},"priceChange":{},"pairCreatedAt":None,
            })
        return out
    except Exception as e:
        logger.error(f"GeckoTerminal: {e}")
        return []

# ─── Data fetchers ────────────────────────────────────────────────────────────

def get_timestamp(addr, pair_created_ms):
    if pair_created_ms:
        return int(pair_created_ms) // 1000
    try:
        r = requests.get("https://api.etherscan.io/api", params={
            "module":"contract","action":"getcontractcreation",
            "contractaddresses":addr,"apikey":ETHERSCAN_API_KEY}, timeout=8)
        d = r.json()
        if d.get("status")=="1" and d.get("result"):
            res = d["result"][0]
            bn = res.get("blockNumber")
            if not bn:
                txh = res.get("txHash")
                if txh:
                    r2 = requests.get("https://api.etherscan.io/api", params={
                        "module":"proxy","action":"eth_getTransactionByHash",
                        "txhash":txh,"apikey":ETHERSCAN_API_KEY}, timeout=8)
                    bn = int(r2.json().get("result",{}).get("blockNumber","0x0"),16)
            else:
                bn = int(bn)
            if bn:
                r3 = requests.get("https://api.etherscan.io/api", params={
                    "module":"block","action":"getblockreward",
                    "blockno":bn,"apikey":ETHERSCAN_API_KEY}, timeout=8)
                ts = r3.json().get("result",{}).get("timeStamp")
                if ts: return int(ts)
        # Fallback: first tx
        r4 = requests.get("https://api.etherscan.io/api", params={
            "module":"account","action":"txlist","address":addr,
            "startblock":0,"endblock":99999999,"page":1,"offset":1,
            "sort":"asc","apikey":ETHERSCAN_API_KEY}, timeout=8)
        txs = r4.json().get("result") or []
        if isinstance(txs, list) and txs:
            ts = txs[0].get("timeStamp")
            if ts: return int(ts)
    except Exception as e:
        logger.error(f"Etherscan ts {addr}: {e}")
    return None

def get_socials(addr):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{addr}", timeout=6)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        return pairs[0].get("info") or {} if pairs else {}
    except:
        return {}

def get_ath(pair):
    try:
        pa = pair.get("pairAddress","")
        cp = float(pair.get("priceUsd") or 0)
        fdv = float(pair.get("fdv") or 0)
        if not pa or not cp or not fdv: return "N/A"
        r = requests.get(f"https://api.geckoterminal.com/api/v2/networks/eth/pools/{pa}/ohlcv/day",
            params={"limit":1000,"currency":"usd"},
            headers={"Accept":"application/json;version=20230302"}, timeout=8)
        r.raise_for_status()
        ohlcv = r.json().get("data",{}).get("attributes",{}).get("ohlcv_list",[])
        if not ohlcv: return "N/A"
        ath_p = max(e[2] for e in ohlcv)
        return fmt((ath_p/cp)*fdv) if ath_p and cp > 0 else "N/A"
    except:
        return "N/A"

def get_tax(addr):
    try:
        r = requests.get(f"https://api.honeypot.is/v2/IsHoneypot?address={addr}", timeout=6)
        r.raise_for_status()
        d = r.json()
        if d.get("honeypotResult",{}).get("isHoneypot",False): return "HONEYPOT"
        s = d.get("simulationResult",{})
        bt, st = s.get("buyTax"), s.get("sellTax")
        if bt is None and st is None: return "N/A"
        bs = f"{bt:.1f}%" if bt is not None else "N/A"
        ss = f"{st:.1f}%" if st is not None else "N/A"
        be = "🟢" if (bt or 0)<=5 else "🟡" if (bt or 0)<=10 else "🔴"
        se = "🟢" if (st or 0)<=5 else "🟡" if (st or 0)<=10 else "🔴"
        return f"{be} Buy: {bs} | {se} Sell: {ss}"
    except:
        return "N/A"

# ─── Core ─────────────────────────────────────────────────────────────────────

def fetch_one(addr, pair):
    ts   = get_timestamp(addr, pair.get("pairCreatedAt"))
    info = get_socials(addr)
    ath  = get_ath(pair)
    tax  = get_tax(addr)
    return addr, pair, ts, info, ath, tax

def find_tokens(name, dex_filter=None):
    with ThreadPoolExecutor(max_workers=2) as p:
        f1 = p.submit(search_dexscreener, name)
        f2 = p.submit(search_geckoterminal, name)
        ds = f1.result()
        gt = f2.result()

    # Filter by dex FIRST
    if dex_filter:
        ds = [p for p in ds if dex_matches_filter(p.get("dexId",""), dex_filter)]
        gt = [p for p in gt if dex_matches_filter(p.get("dexId",""), dex_filter)]

    # Run extra searches with different query variations to catch missed tokens
    nl = name.lower().strip()
    for extra_q in [f"{name} token", f"{name} coin"]:
        try:
            r2 = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={extra_q}", timeout=10)
            r2.raise_for_status()
            extra = [p for p in (r2.json().get("pairs") or [])
                     if p.get("chainId","").lower() == "ethereum"
                     and (p.get("baseToken",{}).get("symbol","").lower() == nl
                          or nl in p.get("baseToken",{}).get("name","").lower())]
            ds.extend(extra)
        except:
            pass

    if not ds and not gt:
        return None, f"No tokens found with the name *{name}* on ETH."

    # Deduplicate: prefer DexScreener
    tmap = {}
    for p in ds:
        a = p.get("baseToken",{}).get("address","").lower()
        if not a: continue
        l = float((p.get("liquidity") or {}).get("usd") or 0)
        if a not in tmap or l > float((tmap[a].get("liquidity") or {}).get("usd") or 0):
            tmap[a] = p
    for p in gt:
        a = p.get("baseToken",{}).get("address","").lower()
        if not a or a in tmap: continue
        tmap[a] = p

    liquid = {a:p for a,p in tmap.items() if float((p.get("liquidity") or {}).get("usd") or 0) >= 400}

    if not liquid:
        return None, f"Found *{name}* on ETH but none have active LP (liquidity > $400)."

    total = len(liquid)
    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = {pool.submit(fetch_one, a, p): a for a,p in liquid.items()}
        for f in as_completed(futs):
            try: results.append(f.result())
            except Exception as e: logger.error(f"Worker: {e}")

    results.sort(key=lambda x: x[2] if x[2] else float("inf"))
    return {"results": results[:MAX_RESULTS], "total": total}, None

# ─── Message ──────────────────────────────────────────────────────────────────

def build_msg(pair, ts, info, ath, tax):
    b     = pair.get("baseToken",{})
    name  = b.get("name","Unknown")
    sym   = b.get("symbol","?")
    addr  = b.get("address","")
    dex   = dex_name(pair.get("dexId","Unknown"))
    pa    = pair.get("pairAddress", addr)
    mc    = pair.get("fdv")
    liq   = pair.get("liquidity") or {}
    lq    = liq.get("quote")
    qs    = (pair.get("quoteToken") or {}).get("symbol","")
    lp    = f"{float(lq):.2f} {qs.upper()}" if lq and qs.upper() in ("WETH","ETH") else f"${fmt(liq.get('usd'))}"
    tx    = (pair.get("txns") or {}).get("h24") or {}
    vol   = pair.get("volume") or {}
    sp = []
    for s in (info.get("socials") or []):
        t = (s.get("type") or "").lower(); u = s.get("url","")
        if not u: continue
        if t == "twitter":    sp.append(f'🐦 [X]({u})')
        elif t == "telegram": sp.append(f'✈️ [Telegram]({u})')
        else: sp.append(f'🔗 [{t.title()}]({u})')
    for w in (info.get("websites") or []):
        u = w.get("url","")
        if u: sp.append(f'🌐 [{(w.get("label") or "Website").title()}]({u})')
    soc = " | ".join(sp) if sp else "No socials available"
    tl = "🚨 *HONEYPOT — DO NOT BUY*" if tax == "HONEYPOT" else f"💸 Tax: {tax}"
    return (
        f"✅ *{name}* ({sym}) ⏳ {age(ts)}  📡\n"
        f"`{addr}`\n\n"
        f"💰 MC: {fmt(mc)} | 🚀 ATH MC: {ath} | 🏦 LP: {lp} | 🏷️ {dex}\n"
        f"📊 Tx 24h: {tx.get('buys',0)}B/{tx.get('sells',0)}S | 🔊 Vol 5m: {fmt(vol.get('m5'))}\n"
        f"{tl}\n\n"
        f"Socials: {soc}\n\n"
        f"🔗 [DexT](https://www.dextools.io/app/en/ether/pair-explorer/{pa}) • "
        f"[DexS](https://dexscreener.com/ethereum/{pa}) • "
        f"[CA](https://etherscan.io/address/{addr}) • "
        f"[MAE](https://t.me/maestro?start={addr}) • "
        f"[MAE Pro](https://t.me/maestropro?start={addr}) • "
        f"[Banana](https://t.me/BananaGunSniper_bot?start=snp_{addr}) • "
        f"[SGM](https://t.me/Sigma_buyBot?start={addr}) • "
        f"[MevX](https://t.me/MevxTradingBot?start={addr})"
    )

# ─── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized.\nContact the admin @glokenn to get access.")
        return
    await update.message.reply_text(
        "👋 *OG Token Finder — ETH*\n\n"
        "Type `/eth <name>` to find the oldest tokens with active LP.\n"
        "Add `v2` `v3` or `v4` to filter by DEX.\n\n"
        "*Examples:*\n`/eth pepe`\n`/eth pepe v2`\n`/eth shiba v3`",
        parse_mode=ParseMode.MARKDOWN)

async def eth_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized.\nContact the admin @glokenn to get access.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/eth <token name>`\nExample: `/eth pepe`", parse_mode=ParseMode.MARKDOWN)
        return
    args = list(context.args)
    dex_filter = None
    if args[-1].lower() in ("v2","v3","v4"):
        dex_filter = args.pop().lower()
    query = " ".join(args).strip()
    if not query:
        await update.message.reply_text("⚠️ Usage: `/eth <token name>`\nExample: `/eth pepe`", parse_mode=ParseMode.MARKDOWN)
        return
    ftxt = f" ({dex_name('uniswap-'+dex_filter)} only)" if dex_filter else ""
    loading = await update.message.reply_text(f"🔍 Searching ETH for *{query}*{ftxt}...", parse_mode=ParseMode.MARKDOWN)
    try:
        data, err = await asyncio.get_event_loop().run_in_executor(None, find_tokens, query, dex_filter)
        if err:
            await loading.edit_text(err, parse_mode=ParseMode.MARKDOWN)
            return
        res = data["results"]; tot = data["total"]
        blocks = [build_msg(pair, ts, info, ath, tax) for _, pair, ts, info, ath, tax in res]
        sep = "\n➖➖➖➖➖➖➖➖➖➖\n"
        await loading.edit_text(sep.join(blocks) + f"\n\n📊 Showing {len(res)}/{tot} results",
            parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"eth_cmd: {e}")
        await loading.edit_text("⚠️ Something went wrong. Please try again.")

async def allow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    if not context.args:
        await update.message.reply_text("Usage: `/allow <user_id>`", parse_mode=ParseMode.MARKDOWN); return
    try:
        uid = int(context.args[0]); whitelist.add(uid)
        await update.message.reply_text(f"✅ User `{uid}` added.", parse_mode=ParseMode.MARKDOWN)
    except: await update.message.reply_text("⚠️ Invalid user ID.")

async def remove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    if not context.args:
        await update.message.reply_text("Usage: `/remove <user_id>`", parse_mode=ParseMode.MARKDOWN); return
    try:
        uid = int(context.args[0]); whitelist.discard(uid)
        await update.message.reply_text(f"✅ User `{uid}` removed.", parse_mode=ParseMode.MARKDOWN)
    except: await update.message.reply_text("⚠️ Invalid user ID.")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    if not whitelist:
        await update.message.reply_text("📋 Whitelist is empty. Only you can use the bot."); return
    await update.message.reply_text(f"📋 *Whitelisted:*\n" + "\n".join(f"`{u}`" for u in whitelist), parse_mode=ParseMode.MARKDOWN)

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Set TELEGRAM_BOT_TOKEN!")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("eth",    eth_cmd))
    app.add_handler(CommandHandler("allow",  allow_cmd))
    app.add_handler(CommandHandler("remove", remove_cmd))
    app.add_handler(CommandHandler("users",  users_cmd))
    logger.info("🚀 OG Token Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
