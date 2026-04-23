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

DEAD = {"0x000000000000000000000000000000000000dead", "0x0000000000000000000000000000000000000000"}

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

# ─── Etherscan ────────────────────────────────────────────────────────────────

def etherscan(params: dict):
    try:
        params["apikey"] = ETHERSCAN_API_KEY
        r = requests.get("https://api.etherscan.io/api", params=params, timeout=8)
        return r.json()
    except:
        return {}

def get_creation_timestamp(address: str):
    try:
        data = etherscan({"module":"contract","action":"getcontractcreation","contractaddresses":address})
        if data.get("status") != "1" or not data.get("result"): return None
        res = data["result"][0]
        bn  = res.get("blockNumber")
        if not bn:
            txh = res.get("txHash")
            if not txh: return None
            r2  = etherscan({"module":"proxy","action":"eth_getTransactionByHash","txhash":txh})
            bn  = int(r2.get("result",{}).get("blockNumber","0x0"), 16)
        else:
            bn = int(bn)
        if not bn: return None
        r3 = etherscan({"module":"block","action":"getblockreward","blockno":bn})
        ts = r3.get("result",{}).get("timeStamp")
        return int(ts) if ts else None
    except Exception as e:
        logger.error(f"Etherscan ts {address}: {e}")
        return None

def get_timestamp(addr: str, pair_created_at_ms):
    if pair_created_at_ms:
        return int(pair_created_at_ms) // 1000
    return get_creation_timestamp(addr)

# ─── LP Burned + CA Renounced ─────────────────────────────────────────────────

def get_lp_burned(pair_addr: str, dex_id: str) -> str:
    if "v3" in dex_id.lower():
        return "V3"
    if not pair_addr:
        return "N/A"
    try:
        dead_bal = int(etherscan({
            "module":"account","action":"tokenbalance",
            "contractaddress": pair_addr,
            "address": "0x000000000000000000000000000000000000dead",
        }).get("result") or 0)
        return "🔥 Burned" if dead_bal > 0 else "❌ Not Burned"
    except:
        return "N/A"

def get_ca_renounced(token_addr: str) -> str:
    try:
        result = etherscan({
            "module":"proxy","action":"eth_call",
            "to": token_addr,"data":"0x8da5cb5b","tag":"latest",
        }).get("result","")
        if result and result != "0x" and len(result) >= 42:
            owner = "0x" + result[-40:]
            return "✅ Renounced" if owner.lower() in DEAD else "⚠️ Not Renounced"
        return "N/A"
    except:
        return "N/A"

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

# ─── Tax ──────────────────────────────────────────────────────────────────────

def get_tax_info(address: str) -> str:
    try:
        r = requests.get(f"https://api.honeypot.is/v2/IsHoneypot?address={address}", timeout=6)
        r.raise_for_status()
        data = r.json()
        if data.get("honeypotResult",{}).get("isHoneypot",False): return "HONEYPOT"
        sim      = data.get("simulationResult",{})
        buy_tax  = sim.get("buyTax")
        sell_tax = sim.get("sellTax")
        if buy_tax is None and sell_tax is None: return "N/A"
        buy_str  = f"{buy_tax:.1f}%"  if buy_tax  is not None else "N/A"
        sell_str = f"{sell_tax:.1f}%" if sell_tax is not None else "N/A"
        buy_e    = "🟢" if (buy_tax  or 0) <= 5 else "🟡" if (buy_tax  or 0) <= 10 else "🔴"
        sell_e   = "🟢" if (sell_tax or 0) <= 5 else "🟡" if (sell_tax or 0) <= 10 else "🔴"
        return f"{buy_e} Buy: {buy_str} | {sell_e} Sell: {sell_str}"
    except:
        return "N/A"

# ─── Core Logic ───────────────────────────────────────────────────────────────

def fetch_token_data(addr: str, pair: dict):
    ts         = get_timestamp(addr, pair.get("pairCreatedAt"))
    info       = get_token_info(addr)
    ath        = get_ath_mc(pair)
    tax        = get_tax_info(addr)
    pair_addr  = pair.get("pairAddress","")
    dex_id     = pair.get("dexId","")
    lp_burned  = get_lp_burned(pair_addr, dex_id)
    renounced  = get_ca_renounced(addr)
    return addr, pair, ts, info, ath, tax, lp_burned, renounced

def find_og_tokens_eth(name: str):
    pairs = dexscreener_search(name)
    if not pairs:
        return None, f"No tokens found with the name *{name}* on ETH."

    token_map = {}
    for p in pairs:
        addr = p.get("baseToken",{}).get("address","").lower()
        liq  = float((p.get("liquidity") or {}).get("usd") or 0)
        if addr not in token_map or liq > float((token_map[addr].get("liquidity") or {}).get("usd") or 0):
            token_map[addr] = p

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

def build_token_block(pair, ts, info, ath, tax, lp_burned, renounced) -> str:
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
        f"{honeypot_line}\n"
        f"🔥 LP: {lp_burned} | 📄 CA: {renounced}\n\n"
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
        "Type `/eth <name>` to find the oldest tokens with active LP.\n\n"
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
        blocks  = [build_token_block(pair, ts, info, ath, tax, lp_burned, renounced)
                   for _, pair, ts, info, ath, tax, lp_burned, renounced in results]
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
