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

MAX_RESULTS = 5
OWNER_ID    = 7525750969
whitelist   = set()

# ─── Auth ─────────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    return user_id == OWNER_ID or user_id in whitelist

# ─── Formatters ───────────────────────────────────────────────────────────────

def fmt_compact(n):
    if n is None: return "N/A"
    n = float(n)
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:     return f"{n/1_000_000:.2f}M"
    if n >= 1_000:         return f"{n/1_000:.2f}K"
    if n == 0:             return "N/A"
    return f"{n:.2f}"

def age_str(ts):
    if not ts: return "Unknown"
    delta         = datetime.now(timezone.utc) - datetime.fromtimestamp(ts, tz=timezone.utc)
    total_seconds = int(delta.total_seconds())
    years   = total_seconds // (365 * 24 * 3600); total_seconds %= (365 * 24 * 3600)
    months  = total_seconds // (30 * 24 * 3600);  total_seconds %= (30 * 24 * 3600)
    days    = total_seconds // (24 * 3600);        total_seconds %= (24 * 3600)
    hours   = total_seconds // 3600;               total_seconds %= 3600
    minutes = total_seconds // 60
    parts = []
    if years:   parts.append(f"{years}y")
    if months:  parts.append(f"{months}mo")
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts) + " ago"

# ─── DexScreener ──────────────────────────────────────────────────────────────

def dexscreener_search(name: str):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/search?q={name}", timeout=10)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        nl = name.lower().strip()
        return [
            p for p in pairs
            if p.get("chainId","").lower() == "ethereum"
            and (
                p.get("baseToken",{}).get("symbol","").lower() == nl
                or p.get("baseToken",{}).get("name","").lower() == nl
            )
        ]
    except Exception as e:
        logger.error(f"DexScreener: {e}")
        return []

# ─── GeckoTerminal ────────────────────────────────────────────────────────────

def geckoterminal_search(name: str):
    try:
        r = requests.get(
            "https://api.geckoterminal.com/api/v2/search/pools",
            params={"query": name, "network": "eth", "page": 1},
            headers={"Accept": "application/json;version=20230302"},
            timeout=10,
        )
        r.raise_for_status()
        pools = r.json().get("data") or []
        nl      = name.lower().strip()
        results = []
        for pool in pools:
            attrs      = pool.get("attributes", {})
            sym        = attrs.get("base_token_symbol", "").lower()
            pool_name  = attrs.get("name", "").lower()
            liq_usd    = float(attrs.get("reserve_in_usd") or 0)
            if liq_usd < 500: continue
            if sym != nl and pool_name.split(" / ")[0].strip() != nl: continue
            token_addr = (pool.get("relationships", {})
                             .get("base_token", {})
                             .get("data", {})
                             .get("id", "")
                             .replace("eth_", ""))
            pool_addr  = attrs.get("address", "")
            vol        = attrs.get("volume_usd", {}) or {}
            results.append({
                "chainId":       "ethereum",
                "pairAddress":   pool_addr,
                "baseToken":     {"address": token_addr, "symbol": sym.upper(), "name": pool_name.split(" / ")[0].strip().title()},
                "quoteToken":    {"symbol": "WETH"},
                "dexId":         attrs.get("dex_id", "unknown"),
                "priceUsd":      str(attrs.get("base_token_price_usd") or 0),
                "fdv":           attrs.get("fdv_usd"),
                "liquidity":     {"usd": liq_usd},
                "volume":        {"m5": vol.get("m5"), "h1": vol.get("h1"), "h6": vol.get("h6"), "h24": vol.get("h24")},
                "txns":          {"h24": {"buys": 0, "sells": 0}},
                "priceChange":   {},
                "pairCreatedAt": None,
            })
        return results
    except Exception as e:
        logger.error(f"GeckoTerminal: {e}")
        return []

# ─── Etherscan ────────────────────────────────────────────────────────────────

def get_creation_timestamp(address: str):
    try:
        # Method 1: getcontractcreation
        r = requests.get("https://api.etherscan.io/api", params={
            "module":"contract","action":"getcontractcreation",
            "contractaddresses": address,"apikey": ETHERSCAN_API_KEY,
        }, timeout=8)
        data = r.json()
        bn  = None
        if data.get("status") == "1" and data.get("result"):
            res = data["result"][0]
            bn  = res.get("blockNumber")
            if not bn:
                txh = res.get("txHash")
                if txh:
                    r2  = requests.get("https://api.etherscan.io/api", params={
                        "module":"proxy","action":"eth_getTransactionByHash",
                        "txhash": txh,"apikey": ETHERSCAN_API_KEY,
                    }, timeout=8)
                    bn = int(r2.json().get("result",{}).get("blockNumber","0x0"), 16)
            else:
                bn = int(bn)

        # Method 2: fallback — get first tx from account tx list
        if not bn:
            r3 = requests.get("https://api.etherscan.io/api", params={
                "module":"account","action":"txlist",
                "address": address,"startblock":0,"endblock":99999999,
                "page":1,"offset":1,"sort":"asc",
                "apikey": ETHERSCAN_API_KEY,
            }, timeout=8)
            txs = r3.json().get("result") or []
            if isinstance(txs, list) and txs:
                bn = int(txs[0].get("blockNumber", 0))
                ts = txs[0].get("timeStamp")
                if ts:
                    return int(ts)

        if not bn: return None

        r4 = requests.get("https://api.etherscan.io/api", params={
            "module":"block","action":"getblockreward",
            "blockno": bn,"apikey": ETHERSCAN_API_KEY,
        }, timeout=8)
        ts = r4.json().get("result",{}).get("timeStamp")
        return int(ts) if ts else None
    except Exception as e:
        logger.error(f"Etherscan {address}: {e}")
        return None

def get_timestamp(addr: str, pair_created_at_ms):
    if pair_created_at_ms:
        return int(pair_created_at_ms) // 1000
    return get_creation_timestamp(addr)

# ─── Socials ──────────────────────────────────────────────────────────────────

def get_token_info(address: str):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{address}", timeout=6)
        r.raise_for_status()
        pairs = r.json().get("pairs") or []
        return pairs[0].get("info") or {} if pairs else {}
    except:
        return {}

# ─── ATH MC ───────────────────────────────────────────────────────────────────

def get_ath_mc(pair: dict) -> str:
    try:
        pair_addr     = pair.get("pairAddress","")
        current_price = float(pair.get("priceUsd") or 0)
        fdv           = float(pair.get("fdv") or 0)
        if not pair_addr or not current_price or not fdv: return "N/A"
        r = requests.get(
            f"https://api.geckoterminal.com/api/v2/networks/eth/pools/{pair_addr}/ohlcv/day",
            params={"limit":1000,"currency":"usd"},
            headers={"Accept":"application/json;version=20230302"},
            timeout=8,
        )
        r.raise_for_status()
        ohlcv = r.json().get("data",{}).get("attributes",{}).get("ohlcv_list",[])
        if not ohlcv: return "N/A"
        ath_price = max(entry[2] for entry in ohlcv)
        return fmt_compact((ath_price / current_price) * fdv) if ath_price and current_price > 0 else "N/A"
    except:
        return "N/A"

# ─── Tax + LP from honeypot.is ───────────────────────────────────────────────

def get_tax_and_lp(address: str, dex_id: str) -> tuple:
    """Returns (tax_str, lp_str) from a single honeypot.is call."""
    dex = dex_id.lower()

    # V3/V4 pools have no burnable LP
    if "v4" in dex:
        lp_status = "🔵 V4 Pool"
    elif "v3" in dex:
        lp_status = "🟣 V3 Pool"
    else:
        lp_status = "N/A"

    tax_str = "N/A"
    try:
        r = requests.get(f"https://api.honeypot.is/v2/IsHoneypot?address={address}", timeout=6)
        r.raise_for_status()
        data = r.json()

        # Tax
        if data.get("honeypotResult",{}).get("isHoneypot", False):
            tax_str = "HONEYPOT"
        else:
            sim      = data.get("simulationResult", {})
            buy_tax  = sim.get("buyTax")
            sell_tax = sim.get("sellTax")
            if buy_tax is not None or sell_tax is not None:
                buy_str  = f"{buy_tax:.1f}%"  if buy_tax  is not None else "N/A"
                sell_str = f"{sell_tax:.1f}%" if sell_tax is not None else "N/A"
                buy_e    = "🟢" if (buy_tax  or 0) <= 5 else "🟡" if (buy_tax  or 0) <= 10 else "🔴"
                sell_e   = "🟢" if (sell_tax or 0) <= 5 else "🟡" if (sell_tax or 0) <= 10 else "🔴"
                tax_str  = f"{buy_e} Buy: {buy_str} | {sell_e} Sell: {sell_str}"

        # LP burn from lpHolders (honeypot.is)
        if lp_status == "N/A":
            lp_holders = data.get("lpHolders") or []
            dead_addrs = {
                "0x000000000000000000000000000000000000dead",
                "0x0000000000000000000000000000000000000000",
            }
            burned = any(h.get("address","").lower() in dead_addrs for h in lp_holders)
            locked = any(h.get("isLocked", False) for h in lp_holders)
            if burned:
                lp_status = "🔥 Burned"
            elif locked:
                lp_status = "🔒 Locked"
            elif lp_holders:
                lp_status = "🔓 Not Burned"

    except Exception as e:
        logger.error(f"honeypot.is {address}: {e}")

    return tax_str, lp_status

# ─── Core Logic ───────────────────────────────────────────────────────────────

def fetch_token_data(addr: str, pair: dict):
    ts          = get_timestamp(addr, pair.get("pairCreatedAt"))
    info        = get_token_info(addr)
    ath         = get_ath_mc(pair)
    tax, lp_burn = get_tax_and_lp(addr, pair.get("dexId",""))
    return addr, pair, ts, info, ath, tax, lp_burn

def find_og_tokens_eth(name: str):
    # Search both sources in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(dexscreener_search, name)
        f2 = pool.submit(geckoterminal_search, name)
        ds_pairs = f1.result()
        gt_pairs = f2.result()

    all_pairs = ds_pairs + gt_pairs

    if not all_pairs:
        return None, f"No tokens found with the name *{name}* on ETH."

    # Group by token address — deduplicate same CA
    # Prefer DexScreener results (they have pairCreatedAt timestamp)
    # Only use GeckoTerminal if token not found in DexScreener at all
    token_map = {}
    for p in ds_pairs:
        addr = p.get("baseToken",{}).get("address","").lower()
        if not addr: continue
        liq  = float((p.get("liquidity") or {}).get("usd") or 0)
        if addr not in token_map or liq > float((token_map[addr].get("liquidity") or {}).get("usd") or 0):
            token_map[addr] = p
    for p in gt_pairs:
        addr = p.get("baseToken",{}).get("address","").lower()
        if not addr or addr in token_map: continue  # skip if already found by DexScreener
        token_map[addr] = p

    # Filter: must have LP > $500
    liquid = {a: p for a, p in token_map.items()
              if float((p.get("liquidity") or {}).get("usd") or 0) >= 500}

    if not liquid:
        return None, f"Found *{name}* on ETH but none have active LP (liquidity > $500)."

    total = len(liquid)
    results = []
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(fetch_token_data, addr, pair): addr for addr, pair in liquid.items()}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                logger.error(f"Worker error: {e}")

    results.sort(key=lambda x: x[2] if x[2] else float("inf"))
    return {"results": results[:MAX_RESULTS], "total": total}, None

# ─── Message Builder ──────────────────────────────────────────────────────────

def build_token_block(pair, ts, info, ath, tax, lp_burn="N/A") -> str:
    base      = pair.get("baseToken",{})
    name      = base.get("name","Unknown")
    symbol    = base.get("symbol","?")
    addr      = base.get("address","")
    dex       = pair.get("dexId","Unknown").replace("-"," ").title()
    pair_addr = pair.get("pairAddress", addr)
    mc        = pair.get("fdv")
    liq       = pair.get("liquidity") or {}
    liq_quote = liq.get("quote")
    quote_sym = (pair.get("quoteToken") or {}).get("symbol","")
    lp_str    = (f"{float(liq_quote):.2f} {quote_sym.upper()}"
                 if liq_quote and quote_sym.upper() in ("WETH","ETH")
                 else f"${fmt_compact(liq.get('usd'))}")
    txns  = (pair.get("txns") or {}).get("h24") or {}
    vol   = pair.get("volume") or {}

    socials_raw  = info.get("socials") or []
    websites_raw = info.get("websites") or []
    social_parts = []
    for s in socials_raw:
        t = (s.get("type") or "").lower(); url = s.get("url","")
        if not url: continue
        if t == "twitter":    social_parts.append(f'🐦 [X]({url})')
        elif t == "telegram": social_parts.append(f'✈️ [Telegram]({url})')
        else: social_parts.append(f'🔗 [{t.title()}]({url})')
    for w in websites_raw:
        url = w.get("url","")
        if url: social_parts.append(f'🌐 [{(w.get("label") or "Website").title()}]({url})')
    socials_line = " | ".join(social_parts) if social_parts else "No socials available"

    honeypot_line = "🚨 *HONEYPOT — DO NOT BUY*" if tax == "HONEYPOT" else f"💸 Tax: {tax}"

    return (
        f"✅ *{name}* ({symbol}) ⏳ {age_str(ts)}  📡\n"
        f"`{addr}`\n\n"
        f"💰 MC: {fmt_compact(mc)} | 🚀 ATH MC: {ath} | 🏦 LP: {lp_str} | 🏷️ {dex}\n"
        f"📊 Tx 24h: {txns.get('buys',0)}B/{txns.get('sells',0)}S | 🔊 Vol 5m: {fmt_compact(vol.get('m5'))}\n"
        f"{honeypot_line}\n\n"
        f"Socials: {socials_line}\n\n"
        f"🔗 [DexT](https://www.dextools.io/app/en/ether/pair-explorer/{pair_addr}) • "
        f"[DexS](https://dexscreener.com/ethereum/{pair_addr}) • "
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
        "Type `/eth <n>` to find the oldest tokens with active LP.\n\n"
        "*Examples:*\n`/eth pepe`\n`/eth shiba`\n`/eth wojak`",
        parse_mode=ParseMode.MARKDOWN,
    )

async def eth_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ You are not authorized.\nContact the admin @glokenn to get access.")
        return
    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/eth <token name>`\nExample: `/eth pepe`", parse_mode=ParseMode.MARKDOWN)
        return
    query   = " ".join(context.args).strip()
    loading = await update.message.reply_text(f"🔍 Searching ETH for *{query}*...", parse_mode=ParseMode.MARKDOWN)
    try:
        data, error = await asyncio.get_event_loop().run_in_executor(None, find_og_tokens_eth, query)
        if error:
            await loading.edit_text(error, parse_mode=ParseMode.MARKDOWN)
            return
        results = data["results"]
        total   = data["total"]
        blocks  = [build_token_block(pair, ts, info, ath, tax, lp_burn)
                   for _, pair, ts, info, ath, tax, lp_burn in results]
        sep    = "\n➖➖➖➖➖➖➖➖➖➖\n"
        footer = f"\n\n📊 Showing {len(results)}/{total} results"
        await loading.edit_text(sep.join(blocks) + footer, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        logger.error(f"eth_command error: {e}")
        await loading.edit_text("⚠️ Something went wrong. Please try again.")

async def allow_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: `/allow <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        uid = int(context.args[0])
        whitelist.add(uid)
        await update.message.reply_text(f"✅ User `{uid}` added.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user ID.")

async def remove_user(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not context.args:
        await update.message.reply_text("Usage: `/remove <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    try:
        uid = int(context.args[0])
        whitelist.discard(uid)
        await update.message.reply_text(f"✅ User `{uid}` removed.", parse_mode=ParseMode.MARKDOWN)
    except ValueError:
        await update.message.reply_text("⚠️ Invalid user ID.")

async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    if not whitelist:
        await update.message.reply_text("📋 Whitelist is empty. Only you can use the bot.")
        return
    users = "\n".join([f"`{uid}`" for uid in whitelist])
    await update.message.reply_text(f"📋 *Whitelisted users:*\n{users}", parse_mode=ParseMode.MARKDOWN)

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    if TELEGRAM_BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        raise ValueError("Set TELEGRAM_BOT_TOKEN environment variable!")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("eth",    eth_command))
    app.add_handler(CommandHandler("allow",  allow_user))
    app.add_handler(CommandHandler("remove", remove_user))
    app.add_handler(CommandHandler("users",  list_users))
    logger.info("🚀 OG Token Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
