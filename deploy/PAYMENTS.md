# Selling uploads through Discord and Whop

How a person goes from "joined the Discord" to "can upload the videos they paid for", and every
value you have to fill in to make it work.

## The flow

```
member opens a ticket ──► codec399 bot posts the pricing panel in the ticket
                            │  $25 per ad  ·  $1,500 / month Gold (unlimited)
                            │  "Register first at https://<site>/register"
                            ▼
member clicks Buy ad uploads ──► form: registered email + how many ads
                            │
                            ├─ bot asks codec399: is that email registered?   (GET /api/bot/account)
                            │     no  → "Register first, then click again"
                            │     yes → bot asks Whop for a checkout        (POST /api/v1/checkout_configurations)
                            │             price = ads × $25, metadata {"email", "ads"}
                            ▼
member pays on Whop ──► Whop sends payment.succeeded to https://<site>/api/whop/webhook
                            │  signature checked, duplicates dropped
                            ▼
codec399 approves the account if it was still pending and adds the credits
                            │  one credit per upload; Gold = unlimited for 31 days per payment
                            ▼
member signs in, Upload is enabled for exactly the number of ads paid for
(staff see "✅ Paid: 4 ad uploads for buyer@example.com" in the notify channel)
```

The email is the key. The bot refuses to hand out a link for an email that is not registered, and
the webhook credits whichever account carries the email in the checkout's metadata. If someone pays
with metadata that matches no account (only possible by sharing a link), the webhook logs it,
announces it in the notify channel, and an admin credits the account by hand from the Approval tab
once they register.

## 1. Whop: product, API key and business id

Your values (looked up with your API key on 2026-10-29; none of these is a secret):

| Setting | Value |
|---|---|
| `CODEC399_PUBLIC_URL` | `https://ghostrus.xyz` (VPS srv1739797, 2.25.183.193) |
| `DISCORD_GUILD_ID` | `1565025145188388914` (GhostRUs) |
| `DISCORD_TICKET_CATEGORY_ID` | unset: the server has no ticket category yet, so tickets are matched by the `ticket-` name prefix |
| `WHOP_COMPANY_ID` | `biz_q8U4sOdwLgmqk5` (Ghost Marketing) |
| `WHOP_PRODUCT_ID` | `prod_obWrOW3bzzOBD` (the product currently titled "Silver - 1 Ad") |
| `WHOP_API_KEY` | the key in your `whop api-key.txt` file (never commit it; it goes in `/etc/codec399.env` only) |

The bot sells everything under that one product. It keeps one **hidden** plan per ad count
("3 ad uploads" at $75, "1 ad upload" at $25, and so on) and one for Gold ("Gold: unlimited
uploads", $1,500 every 30 days), created through the API the first time each is needed and reused
after that (ids are remembered in `/var/lib/codec399/whop_variants.json`, and found again by their
metadata if that file is lost). The dry run already created three of them on your account; you can
see them under the product's plans, and they are harmless. Buyers never browse these: each checkout
link is made for one person and carries their email.

Two things worth doing in the dashboard: rename the product from "Silver - 1 Ad" to something
general such as "Ghost Ad Uploads" (the title shows on the checkout page), and leave the original
$25 plan you made as it is or archive it; the bot does not use it.

If you would rather control the Gold plan yourself, create a $1,500 / 30-day renewal plan on the
product and put its `plan_...` id in `WHOP_GOLD_PLAN_ID`; the bot then uses that one.

## 2. Whop: the webhook

**Dashboard → Developer → Webhooks → Create webhook.** The form has these fields; fill them in
exactly like this:

| Field | Value |
|---|---|
| Name (optional) | `Ghost Whop Webhooks` or anything you like |
| Endpoint URL | `https://ghostrus.xyz/api/whop/webhook` |
| Sub-account events | **off** |
| Enabled | **on** |
| Events | tick **`payment.succeeded`** (this is the one that credits accounts). Also tick **`membership.updated`** (not `member.updated`) so a Gold subscription that lapses or is cancelled ends Gold on the site. Leave **All** unticked; nothing else is needed. |

Click **Save**. The webhooks table then shows a **Secret** column with a value starting `ws_`.
Copy it as is (no trimming, no base64) into `CODEC399_WHOP_WEBHOOK_SECRET`. Until that variable
is set the endpoint answers 503 and nothing is credited.

Whop signs every delivery with that secret (Standard Webhooks: `webhook-id`, `webhook-timestamp`,
`webhook-signature` headers over the raw body) and `whop.py` rejects anything that does not verify,
is older than five minutes, or was already delivered. Every delivery, good or bad, is appended to
`/var/lib/codec399/whop_events.log`.

To check the wiring before a real sale, buy **1 ad** ($25) with a test account and refund it
from the Whop dashboard afterwards; the refund does not remove the
credit, so take it back with `manage.py credits EMAIL -1` or from the Approval tab. A delivery
without your metadata (Whop's "send test event", if offered) is logged as "No order details in
metadata; nothing credited", which proves the URL and the signature are right.

## 3. Discord: the bot

The application already exists: **codec399 / application id `1565240562515443772`** at
<https://discord.com/developers/applications/1565240562515443772>.

1. **Bot** tab → **Reset Token** → copy the token straight into your key file (it is shown once).
   It becomes `DISCORD_BOT_TOKEN` on the server. The three privileged intents (Presence, Server
   Members, Message Content) stay **off**; the bot does not need them.
2. Add the bot to the server with this link (scopes `bot` + `applications.commands`; permissions
   View Channels, Send Messages, Embed Links, Read Message History = `84992`):

   <https://discord.com/oauth2/authorize?client_id=1565240562515443772&scope=bot+applications.commands&permissions=84992>

   Pick the Ghost server and click **Authorize**.
3. In Discord, **User Settings → Advanced → Developer Mode** on. Right-click the server icon →
   **Copy Server ID** → `DISCORD_GUILD_ID` (slash commands then appear immediately instead of in
   about an hour). Right-click the category your ticket bot creates tickets in → **Copy Channel ID**
   → `DISCORD_TICKET_CATEGORY_ID`. If tickets are not under one category, leave that unset and set
   `DISCORD_TICKET_PREFIX` to whatever the ticket channel names start with (default `ticket-`).
4. Make sure the ticket bot's channels are visible to this bot's role (ticket bots usually create
   private channels: add the codec399 bot's role to the ticket bot's "support roles" setting, or to
   the category's permissions). If the bot cannot see the channel it cannot post the panel.
5. Optional but useful: in a staff-only channel, **Edit Channel → Integrations → Webhooks → New
   Webhook → Copy Webhook URL** → `DISCORD_NOTIFY_WEBHOOK`. Every payment (and every one that
   could not be matched to an account) is announced there.

What the bot offers once running:

| Where | What |
|---|---|
| Every new ticket | the pricing panel with **Buy ad uploads** and **Gold** buttons |
| `/buy email ads` | a checkout link for N ads, visible only to the person |
| `/gold email` | a checkout link for Gold |
| `/balance email` | uploads left / Gold, and the account status |
| `/pricing` | posts the panel in the current channel (needs Manage Channels) |

Replies to the buttons and commands are ephemeral (only the person who clicked sees them); the bot
also drops a one-line note in the ticket so staff can follow ("… requested a checkout for … 4 ad
uploads at $25 each: $100").

## 4. The server

Add to `/etc/codec399.env` (every line is listed, commented out, in `codec399.env.example`):

```
CODEC399_PUBLIC_URL=https://ghostrus.xyz
CODEC399_WHOP_WEBHOOK_SECRET=ws_...
WHOP_API_KEY=...
WHOP_COMPANY_ID=biz_...
WHOP_PRODUCT_ID=prod_...
DISCORD_BOT_TOKEN=...
DISCORD_GUILD_ID=...
DISCORD_TICKET_CATEGORY_ID=...
CODEC399_BOT_TOKEN=<output of: openssl rand -hex 32>
CODEC399_INTERNAL_URL=http://127.0.0.1:5399
DISCORD_NOTIFY_WEBHOOK=https://discord.com/api/webhooks/...
```

Then re-run the installer, which installs the bot's requirements and starts the
`codec399-bot` service whenever `DISCORD_BOT_TOKEN` is set:

```bash
cd /root/codec399 && git pull && bash deploy/install.sh ghostrus.xyz
journalctl -u codec399-bot -f
```

The bot logs `signed in as codec399#1234; whop configured` when everything is in place, or names
the variables that are still missing.

## 5. Try it end to end

1. Register a test account on the site (it sits in the Approval tab as pending).
2. Open a ticket in Discord; the panel appears a few seconds after the ticket bot's message.
3. Click **Buy ad uploads**, enter the test email and `1`; you get a $25 Whop link.
4. Pay it (or, on the sandbox, use Whop's test card). Within seconds the notify channel shows
   "✅ Paid: 1 ad upload …", the account is active, and `/balance` reports 1 upload left.
5. Sign in to the site: Upload is enabled, and after one video it shows "no uploads left" with the
   Discord link.

## Changing prices

`CODEC399_PRICE_PER_AD` and `CODEC399_GOLD_PRICE` in the env file, then
`systemctl restart codec399-bot`. The panel, the forms and the checkout amounts all read from
them. Credits already bought are unaffected.
