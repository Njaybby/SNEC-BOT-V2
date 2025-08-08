#!/usr/bin/env python3
import os
import asyncio
from aiohttp import ClientSession, TCPConnector

# Optional imports: these packages may not be installed in all environments.
# We attempt to import them and provide helpful error messages if missing.
try:
    from telegram import Update
    from telegram.constants import ParseMode
    from telegram.ext import (
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
    )
except ImportError as te:
    Update = None  # type: ignore
    ParseMode = None  # type: ignore
    ApplicationBuilder = None  # type: ignore
    CommandHandler = None  # type: ignore
    ContextTypes = None  # type: ignore
    telegram_import_error = te
else:
    telegram_import_error = None

try:
    from dotenv import load_dotenv
except ImportError:
    # Fallback: define a basic load_dotenv if python-dotenv is unavailable.
    def load_dotenv(path: str = ".env", **kwargs):  # type: ignore
        """
        Minimal implementation of load_dotenv.

        Reads key=value pairs from a .env file and injects them into
        os.environ if they are not already defined. This function is a
        simplified fallback when python-dotenv is not available.

        Args:
            path (str): Path to the .env file. Defaults to '.env'.
        """
        if not path:
            path = ".env"
        try:
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        key, value = line.split("=", 1)
                        key = key.strip()
                        value = value.strip().strip('"').strip("'")
                        # Set only if not already in environment
                        if key not in os.environ:
                            os.environ[key] = value
        except FileNotFoundError:
            # Silently ignore if .env file doesn't exist
            pass

# ─── Load config ──────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
RPC_URL = os.getenv("REDBELLY_RPC")
SNEC_ADDR = os.getenv("SNEC_TOKEN")
WRBNT_ADDR = os.getenv("WRBNT_TOKEN")
PAIR_ADDR = os.getenv("PAIR_ADDR")
BUY_THRESHOLD_USD = 2.0  # alert on price > $2

"""
This script implements a simple Telegram bot that fetches the price of the
$SNEC token in USD using an on-chain reserve ratio from a Redbelly pair
and an off-chain USD price for WRBNT from CoinGecko. It then exposes a
`/price_snec` command and a background task to notify when the price
crosses a predefined USD threshold. The code has been adjusted to handle
missing environment variables more gracefully, and reorganized for clarity.

To run this script, you need to define the following environment variables:

* TELEGRAM_TOKEN: The API token for your Telegram bot.
* CHAT_ID: The ID of the chat/channel where alerts will be sent.
* REDBELLY_RPC: The JSON-RPC URL for the Redbelly node.
* SNEC_TOKEN: The contract address for the SNEC token (will be lowercased).
* WRBNT_TOKEN: The contract address for the WRBNT token (will be lowercased).
* PAIR_ADDR: The contract address for the pair (LP token) that holds
  SNEC and WRBNT reserves.

Ensure these are set in a `.env` file or the system environment before running.
"""

# Validate required configuration upfront to avoid runtime errors later. If any
# of these are missing, raise a descriptive exception.
def validate_env_var(var_name: str, value: str):
    if value is None or value == "":
        raise EnvironmentError(
            f"Missing required environment variable '{var_name}'. "
            "Please set it in your environment or .env file."
        )


for name, value in [
    ("TELEGRAM_TOKEN", TOKEN),
    ("CHAT_ID", CHAT_ID),
    ("REDBELLY_RPC", RPC_URL),
    ("SNEC_TOKEN", SNEC_ADDR),
    ("WRBNT_TOKEN", WRBNT_ADDR),
    ("PAIR_ADDR", PAIR_ADDR),
]:
    validate_env_var(name, value)

# Convert addresses to lowercase once after validation. This avoids calling
# `.lower()` on None which would raise an exception.
SNEC_ADDR = SNEC_ADDR.lower()
WRBNT_ADDR = WRBNT_ADDR.lower()
CHAT_ID = int(CHAT_ID)


async def rpc(session: ClientSession, method: str, params: list):
    """
    Make a JSON-RPC call to the specified Redbelly RPC endpoint.

    Args:
        session (ClientSession): The aiohttp session used for the HTTP request.
        method (str): The JSON-RPC method name.
        params (list): The parameters for the RPC call.

    Returns:
        dict: The result from the JSON-RPC call.

    Raises:
        RuntimeError: If the JSON-RPC response contains an error.
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    async with session.post(RPC_URL, json=payload) as resp:
        resp.raise_for_status()
        data = await resp.json()
        # JSON-RPC responses may include an 'error' field only when an error occurs.
        # If the field exists but is None or falsy, treat it as no error.
        err = data.get("error")
        if err:
            raise RuntimeError(err)
        return data.get("result")


async def fetch_price_usd() -> float:
    """
    Fetch the price of $SNEC in USD.

    This function uses on-chain reserves from the Redbelly pair contract and
    off-chain pricing from CoinGecko to compute the USD price. The ratio of
    reserves r1/r0 yields the price in WRBNT. That is multiplied by the
    current WRBNT-to-USD price obtained from CoinGecko to derive the SNEC
    price in USD.

    Returns:
        float: The estimated SNEC price in USD.
    """
    # Acquire the reserves via JSON-RPC. Use a single session for these calls.
    conn = TCPConnector(ssl=False)
    async with ClientSession(connector=conn) as sess:
        # `eth_call` for getReserves in an Uniswap V2-like pair. Data is
        # function selector 0x0902f1ac and returns (reserve0, reserve1)
        raw = await rpc(
            sess,
            "eth_call",
            [{"to": PAIR_ADDR, "data": "0x0902f1ac"}, "latest"],
        )
        # raw is a hex string like 0x000000... reserve0 reserve1 etc.
        # Skip "0x" then parse as big integers. Each reserve is 32 bytes (64 hex chars).
        r0 = int(raw[2:66], 16)
        r1 = int(raw[66:130], 16)
        # Determine which token is token0 in the pair.
        t0 = await rpc(
            sess,
            "eth_call",
            [{"to": PAIR_ADDR, "data": "0x0dfe1681"}, "latest"],
        )
        token0 = "0x" + t0[-40:]

    # Determine reserves order: if token0 matches SNEC, assign accordingly.
    # When token0 == SNEC, r0 holds the SNEC reserves; else r1 holds them.
    if token0.lower() == SNEC_ADDR:
        snec_res, wrbnt_res = r0, r1
    else:
        snec_res, wrbnt_res = r1, r0

    # Calculate the pair price of WRBNT relative to SNEC
    if snec_res == 0:
        raise ZeroDivisionError("SNEC reserve is zero in the pair; cannot compute price.")
    price_wrbnt = wrbnt_res / snec_res

    # Fetch the WRBNT price in USD from CoinGecko. Use a separate session to
    # avoid mixing endpoints in the same session. If the token name changes,
    # adjust the `ids` parameter accordingly.
    async with ClientSession() as http:
        cg_resp = await http.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "redbelly-network-token", "vs_currencies": "usd"},
        )
        cg_resp.raise_for_status()
        cg = await cg_resp.json()
        usd = cg["redbelly-network-token"]["usd"]

    return price_wrbnt * usd


from typing import Any

async def price_snec(update: Any, ctx: Any) -> None:
    """
    Telegram command handler for `/price_snec`.

    Responds to the user with the current price of $SNEC, estimated market cap
    given a total supply of 69 billion tokens, and includes a signature line.

    Args:
        update (Update): Telegram update that triggered this handler.
        ctx (ContextTypes.DEFAULT_TYPE): The context from the handler.
    """
    try:
        usd_price = await fetch_price_usd()
        market_cap = usd_price * 69e9
        text = (
            "Slithery lil Snec 🐍\n\n"
            f"🐍 $SNEC Price: ~${usd_price:.4f} USD\n"
            f"💰 Market Cap: ~${market_cap:,.0f} USD\n"
            f"🌐 Supply: 69 000 000 000 SNEC\n\n"
            "✨ Powered by @njaybby"
        )
        # Use ParseMode.HTML if available; else fallback to plain HTML string.
        parse_mode = ParseMode.HTML if ParseMode is not None else "HTML"
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode=parse_mode,
        )
    except Exception as ex:
        await ctx.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❗ Error fetching price: {ex}",
        )


async def buy_alert_loop(bot: Any) -> None:
    """
    Background loop that periodically checks the price of $SNEC and sends a
    notification to the configured chat ID when the price crosses the
    `BUY_THRESHOLD_USD` threshold.

    Args:
        bot: The bot instance used to send messages.
    """
    global last_alerted
    # Wait a bit before the first check to allow the bot to finish starting
    await asyncio.sleep(10)
    while True:
        try:
            usd_price = await fetch_price_usd()
            if usd_price > BUY_THRESHOLD_USD and usd_price != last_alerted:
                msg = (
                    f"🚨 Slithery Snec just slithered past "
                    f"${BUY_THRESHOLD_USD:.2f}! Current: ~${usd_price:.4f} USD 🐍"
                )
                await bot.send_message(chat_id=CHAT_ID, text=msg)
                last_alerted = usd_price
        except Exception as ex:
            print(f"[buy-alert error] {ex}")
        # Sleep before next check
        await asyncio.sleep(60)


async def main() -> None:
    """
    Asynchronous entry point for the Snec Telegram bot.

    Builds the application, registers handlers, starts the background alert
    loop, starts the bot, and then blocks indefinitely until the process
    receives a termination signal. Using an asynchronous entry point avoids
    nested event loop issues.
    """
    # If telegram could not be imported, warn the user and exit.
    if telegram_import_error is not None:
        print(
            "Missing required dependency 'python-telegram-bot':\n"
            f"{telegram_import_error}\n"
            "Please install the 'python-telegram-bot' package to run the bot."
        )
        return

    # Build the application with the provided bot token
    app = ApplicationBuilder().token(TOKEN).build()
    # Register the /price_snec command handler
    app.add_handler(CommandHandler("price_snec", price_snec))

    # Initialize and start the application
    await app.initialize()
    # Start the background alert loop
    alert_task = asyncio.create_task(buy_alert_loop(app.bot))
    await app.start()
    # Start polling updates via the underlying updater. Without this call,
    # the application will not receive updates from Telegram.
    try:
        await app.updater.start_polling()
    except AttributeError:
        # If the Updater is not available (e.g. removed in newer PTB versions),
        # run_polling() should be used instead. However, run_polling() cannot be
        # awaited inside an already running event loop. In that case, users
        # should install the job-queue extra and use the synchronous
        # run_polling() call in a synchronous main().
        raise RuntimeError(
            "Unable to start polling: Updater not available. Your installed "
            "version of python-telegram-bot may not support this pattern."
        )
    print("🤖 Snec Bot 2.0 started -- use /price_snec")
    # The bot is now polling for updates internally. We block forever to
    # keep the program alive. Use an endless future to wait for shutdown.
    try:
        await asyncio.Event().wait()
    finally:
        # Shutdown sequence on cancellation or exit
        # Cancel the alert task
        if not alert_task.done():
            alert_task.cancel()
            try:
                await alert_task
            except asyncio.CancelledError:
                pass
        # Stop polling and the bot gracefully
        try:
            await app.updater.stop()
        except Exception:
            pass
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    # Run the asynchronous main function using asyncio.run(). Catch KeyboardInterrupt
    # to allow graceful termination.
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot shutdown requested. Exiting...")