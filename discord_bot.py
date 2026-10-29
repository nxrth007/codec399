"""codec399 Discord bot: pricing in every ticket, Whop checkout links, and balance lookups.

What it does
    * When the ticket bot opens a new ticket channel (a channel created under the ticket category,
      or whose name starts with the ticket prefix), this bot posts the pricing panel there: $25 per
      ad upload, $1,500 a month for Gold (unlimited), with two buttons.
    * "Buy ad uploads" opens a form asking for the registered email and how many ads.  "Gold" asks
      for the email.  The bot checks the email against codec399 (`/api/bot/account`); a person who
      has not registered is told to register first, because the webhook credits the account by
      that email.  Then it asks Whop for a checkout link (whop_api.py) and replies with it, visible
      only to the person who clicked.
    * `/buy` does the same from anywhere, `/balance EMAIL` shows uploads left, and `/pricing`
      (staff) posts the panel in the current channel.

Environment
    DISCORD_BOT_TOKEN             the bot's token (Discord developer portal > Bot > Reset Token)
    DISCORD_GUILD_ID              your server id; commands register instantly there instead of in ~an hour
    DISCORD_TICKET_CATEGORY_ID    category the ticket bot creates channels in (optional)
    DISCORD_TICKET_PREFIX         ticket channel name prefix, default "ticket-"
    CODEC399_PUBLIC_URL           https://codec.example.com (register link, redirect after paying)
    CODEC399_INTERNAL_URL         where to reach the app for account checks, default the public URL
    CODEC399_BOT_TOKEN            shared secret that lets the bot call /api/bot/account
    plus the WHOP_* variables listed in whop_api.py

Run:  python discord_bot.py   (the service unit in deploy/ does this on the server).
"""
import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

import discord
from discord import app_commands

import whop_api

log = logging.getLogger("codec399.bot")

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GUILD_ID = int(os.environ.get("DISCORD_GUILD_ID") or 0)
TICKET_CATEGORY_ID = int(os.environ.get("DISCORD_TICKET_CATEGORY_ID") or 0)
TICKET_PREFIX = os.environ.get("DISCORD_TICKET_PREFIX", "ticket-").lower()
PUBLIC_URL = os.environ.get("CODEC399_PUBLIC_URL", "").rstrip("/")
INTERNAL_URL = (os.environ.get("CODEC399_INTERNAL_URL") or PUBLIC_URL).rstrip("/")
BOT_TOKEN = os.environ.get("CODEC399_BOT_TOKEN", "")
PRICE_PER_AD = whop_api.PRICE_PER_AD
GOLD_PRICE = whop_api.GOLD_PRICE
GOLD_COLOUR = 0xD4AF37
PANEL_COLOUR = 0x5865F2


def dollars(n):
    return f"${n:,.0f}" if float(n).is_integer() else f"${n:,.2f}"


def register_url():
    return f"{PUBLIC_URL}/register" if PUBLIC_URL else "the codec399 sign-in page"


# ---------------------------------------------------------------- codec399 account lookup

def lookup_account(email):
    """Ask codec399 about an email.  Returns the account dict, None if unknown, or raises RuntimeError."""
    if not (INTERNAL_URL and BOT_TOKEN):
        raise RuntimeError("CODEC399_INTERNAL_URL / CODEC399_BOT_TOKEN are not set.")
    url = f"{INTERNAL_URL}/api/bot/account?" + urllib.parse.urlencode({"email": email})
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise RuntimeError(f"codec399 answered {exc.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Could not reach codec399: {exc}") from None


def account_problem(account):
    """A sentence explaining why this account cannot be credited yet, or None when it can."""
    if account is None:
        return (f"That email is not registered. Register first at {register_url()} with the same "
                "email, then come back and click again. Payment is matched to the account by email, "
                "so paying before registering means the credits have nowhere to go.")
    if account.get("status") == "revoked":
        return "That account has been revoked. Please ask a staff member in this ticket before paying."
    if account.get("role") == "admin":
        return "Admin accounts do not need credits."
    return None


def balance_line(account):
    if account.get("gold"):
        return "Gold: unlimited uploads."
    n = int(account.get("credits") or 0)
    return f"{n} upload{'s' if n != 1 else ''} left."


# ---------------------------------------------------------------- the pricing panel

def pricing_embed():
    e = discord.Embed(
        title="codec399 pricing",
        description=(
            f"**{dollars(PRICE_PER_AD)} per ad** (one upload each). Pick how many you need.\n"
            f"**{dollars(GOLD_PRICE)} / month for Gold**: unlimited uploads.\n\n"
            f"**Before you pay, register** at {register_url()} with the email you will give here. "
            "Your payment is matched to that email: the account is approved and credited automatically "
            "the moment Whop confirms it, and you can upload exactly what you paid for."
        ),
        colour=PANEL_COLOUR,
    )
    e.set_footer(text="Buttons reply only to you. Links are valid for one checkout.")
    return e


class PricingView(discord.ui.View):
    """Persistent: custom_ids let the buttons keep working after a bot restart."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label=f"Buy ad uploads ({dollars(PRICE_PER_AD)} each)", style=discord.ButtonStyle.primary,
                       custom_id="codec399:buy_ads", emoji="🎬")
    async def buy_ads(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(BuyAdsModal())

    @discord.ui.button(label=f"Gold: unlimited ({dollars(GOLD_PRICE)}/mo)", style=discord.ButtonStyle.success,
                       custom_id="codec399:buy_gold", emoji="⭐")
    async def buy_gold(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(GoldModal())


# ---------------------------------------------------------------- checkout flow

async def deliver_checkout(interaction, email, ads=None, gold=False):
    """Validate, check the account, create the Whop checkout and answer the interaction (ephemeral)."""
    await interaction.response.defer(ephemeral=True, thinking=True)
    email = (email or "").strip().lower()
    if ads is not None:
        try:
            ads = int(str(ads).strip())
        except ValueError:
            ads = 0
        if not 1 <= ads <= whop_api.MAX_ADS:
            await interaction.followup.send(f"❌ Enter a whole number of ads from 1 to {whop_api.MAX_ADS}.", ephemeral=True)
            return
    if "@" not in email or " " in email:
        await interaction.followup.send("❌ Enter the email address you registered with.", ephemeral=True)
        return

    try:
        account = await asyncio.to_thread(lookup_account, email)
    except RuntimeError as exc:
        log.warning("account lookup failed: %s", exc)
        await interaction.followup.send("❌ I could not check that email with codec399 right now. "
                                        "Please try again in a minute or ask a staff member.", ephemeral=True)
        return
    problem = account_problem(account)
    if problem:
        await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
        return

    try:
        if gold:
            url, amount = await asyncio.to_thread(whop_api.gold_checkout, email, interaction.user.id)
        else:
            url, amount = await asyncio.to_thread(whop_api.ads_checkout, email, ads, interaction.user.id)
    except (whop_api.WhopError, ValueError) as exc:
        log.error("checkout failed for %s: %s", email, exc)
        await interaction.followup.send("❌ Whop could not create the checkout. A staff member has been notified; "
                                        "please try again shortly.", ephemeral=True)
        await notify_staff(interaction, f"Checkout failed for {email} ({interaction.user.mention}): {exc}")
        return

    if gold:
        title, what = "Gold checkout", f"Gold, unlimited uploads, {dollars(amount)} per month."
    else:
        title, what = "Ad upload checkout", f"{ads} ad upload{'s' if ads != 1 else ''} at {dollars(PRICE_PER_AD)} each: **{dollars(amount)}**."
    e = discord.Embed(title=title, colour=GOLD_COLOUR if gold else PANEL_COLOUR,
                      description=f"{what}\n\n**[Pay on Whop]({url})**\n\n"
                                  f"Account: `{email}` ({'pending approval, ' if account.get('status') == 'pending' else ''}{balance_line(account)})\n"
                                  "Once the payment goes through your account is credited automatically, "
                                  "usually within a few seconds. Use `/balance` to check.")
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label="Pay on Whop", url=url, style=discord.ButtonStyle.link))
    await interaction.followup.send(embed=e, view=view, ephemeral=True)
    await notify_staff(interaction, f"{interaction.user.mention} requested a checkout for `{email}`: {what}")


async def notify_staff(interaction, text):
    """A short note in the ticket channel so staff can follow the conversation."""
    channel = interaction.channel
    if channel is None or not is_ticket_channel(channel):
        return
    try:
        await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        pass


class BuyAdsModal(discord.ui.Modal, title="Buy ad uploads"):
    email = discord.ui.TextInput(label="Email you registered with", placeholder="you@example.com", max_length=120)
    ads = discord.ui.TextInput(label=f"How many ads? ({dollars(PRICE_PER_AD)} each)", placeholder="4", max_length=3)

    async def on_submit(self, interaction: discord.Interaction):
        await deliver_checkout(interaction, self.email.value, ads=self.ads.value)


class GoldModal(discord.ui.Modal, title="Gold: unlimited uploads"):
    email = discord.ui.TextInput(label="Email you registered with", placeholder="you@example.com", max_length=120)

    async def on_submit(self, interaction: discord.Interaction):
        await deliver_checkout(interaction, self.email.value, gold=True)


# ---------------------------------------------------------------- tickets

def is_ticket_channel(channel):
    if not isinstance(channel, discord.TextChannel):
        return False
    if TICKET_CATEGORY_ID and channel.category_id == TICKET_CATEGORY_ID:
        return True
    return bool(TICKET_PREFIX) and channel.name.lower().startswith(TICKET_PREFIX)


class Bot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(PricingView())
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self):
        log.info("signed in as %s; whop %s", self.user, "configured" if not whop_api.configured()
                 else "missing " + ", ".join(whop_api.configured()))

    async def on_guild_channel_create(self, channel):
        if not is_ticket_channel(channel):
            return
        # Let the ticket bot post its own opening message first.
        await asyncio.sleep(3)
        try:
            await channel.send(embed=pricing_embed(), view=PricingView())
        except discord.HTTPException as exc:
            log.warning("could not post pricing in %s: %s", channel, exc)


bot = Bot()


@bot.tree.command(name="buy", description="Get a Whop checkout link for ad uploads")
@app_commands.describe(email="The email you registered with on codec399", ads=f"How many ads ({dollars(PRICE_PER_AD)} each)")
async def buy(interaction: discord.Interaction, email: str, ads: app_commands.Range[int, 1, whop_api.MAX_ADS]):
    await deliver_checkout(interaction, email, ads=ads)


@bot.tree.command(name="gold", description=f"Get a Whop checkout link for Gold: unlimited uploads, {dollars(GOLD_PRICE)}/month")
@app_commands.describe(email="The email you registered with on codec399")
async def gold(interaction: discord.Interaction, email: str):
    await deliver_checkout(interaction, email, gold=True)


@bot.tree.command(name="balance", description="How many uploads an account has left")
@app_commands.describe(email="The email you registered with on codec399")
async def balance(interaction: discord.Interaction, email: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        account = await asyncio.to_thread(lookup_account, email.strip().lower())
    except RuntimeError:
        await interaction.followup.send("❌ Could not reach codec399 right now.", ephemeral=True)
        return
    if account is None:
        await interaction.followup.send(f"That email is not registered. Register at {register_url()}.", ephemeral=True)
        return
    status = "" if account.get("status") == "active" else f" Account status: {account.get('status')}."
    await interaction.followup.send(f"`{account['email']}`: {balance_line(account)}{status}", ephemeral=True)


@bot.tree.command(name="pricing", description="Post the pricing panel in this channel (staff)")
@app_commands.default_permissions(manage_channels=True)
async def pricing(interaction: discord.Interaction):
    await interaction.response.send_message(embed=pricing_embed(), view=PricingView())


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if not TOKEN:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    missing = whop_api.configured()
    if missing:
        log.warning("Whop is not configured (%s); checkout buttons will fail until it is.", ", ".join(missing))
    if not BOT_TOKEN:
        log.warning("CODEC399_BOT_TOKEN is not set; account checks will fail.")
    bot.run(TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
