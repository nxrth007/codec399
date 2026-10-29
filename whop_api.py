"""Whop REST client: turn "N ad uploads for this email" or "Gold for this email" into a checkout link.

The Discord bot calls `ads_checkout` / `gold_checkout`.  Whop's checkout needs a priced variant
(what the dashboard calls a plan), so the client keeps one hidden variant per ad count under your
product ("3 ad uploads", $75) and one for Gold ($1,500 every 30 days), creating each the first time it
is needed and remembering the ids in whop_variants.json next to the accounts file.  It then creates
a checkout configuration for that variant carrying metadata {"email", "ads"} or {"email", "gold":
true}; Whop copies that metadata onto the payment (and, for Gold, the membership) and sends it back
in `payment.succeeded`, which whop.py turns into credits on the matching account.

Verified against the live API on 2026-10-29:
    POST /api/v1/variants                 {"account_id", "product_id", "billing_type": "one_time", "initial_price", ...}
    POST /api/v1/checkout_configurations  {"mode": "payment", "variant_id", "metadata", "redirect_url"} -> purchase_url

Environment:
    WHOP_API_KEY        Account API key from the Whop dashboard (Developer > API keys)
    WHOP_COMPANY_ID     Your business id, biz_xxxxxxxx (GET /accounts/me, or the dashboard URL)
    WHOP_PRODUCT_ID     The product the variants hang off, prod_xxxxxxxx
    WHOP_GOLD_PLAN_ID   Optional: an existing $1,500/month plan, plan_xxxxxxxx, used instead of creating one
    WHOP_API_URL        Default https://api.whop.com/api/v1
    CODEC399_PUBLIC_URL Where the customer lands after paying, e.g. https://codec.example.com
    CODEC399_PRICE_PER_AD   Dollars per ad upload, default 25
    CODEC399_GOLD_PRICE     Dollars per month for Gold, default 1500
"""
import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CODEC399_JOBS_DIR") or os.path.join(BASE, "jobs")
VARIANTS_FILE = os.path.join(os.path.dirname(os.environ.get("CODEC399_USERS_FILE") or "") or DATA_DIR,
                             "whop_variants.json")
API_URL = os.environ.get("WHOP_API_URL", "https://api.whop.com/api/v1").rstrip("/")
API_KEY = os.environ.get("WHOP_API_KEY", "")
COMPANY_ID = os.environ.get("WHOP_COMPANY_ID", "")
PRODUCT_ID = os.environ.get("WHOP_PRODUCT_ID", "")
GOLD_PLAN_ID = os.environ.get("WHOP_GOLD_PLAN_ID", "")
PUBLIC_URL = os.environ.get("CODEC399_PUBLIC_URL", "").rstrip("/")
PRICE_PER_AD = float(os.environ.get("CODEC399_PRICE_PER_AD", "25"))
GOLD_PRICE = float(os.environ.get("CODEC399_GOLD_PRICE", "1500"))
GOLD_DAYS = 30
MAX_ADS = 200
TIMEOUT = 20
_lock = threading.Lock()


class WhopError(RuntimeError):
    pass


def configured():
    """Names of the settings still missing; empty when a checkout can be created."""
    missing = [name for name, val in (("WHOP_API_KEY", API_KEY), ("WHOP_COMPANY_ID", COMPANY_ID)) if not val]
    if not PRODUCT_ID and not GOLD_PLAN_ID:
        missing.append("WHOP_PRODUCT_ID")
    return missing


def _check_email(email):
    email = (email or "").strip().lower()
    if "@" not in email or " " in email:
        raise ValueError("Enter the email you registered with.")
    return email


def _redirect():
    return (PUBLIC_URL + "/dashboard?paid=1") if PUBLIC_URL else None


# ---------------------------------------------------------------- request bodies (pure, tested)

def ads_variant_body(ads):
    """A hidden one-time variant for `ads` uploads at PRICE_PER_AD each."""
    ads = int(ads)
    if not 1 <= ads <= MAX_ADS:
        raise ValueError(f"Choose between 1 and {MAX_ADS} ads.")
    return {
        "account_id": COMPANY_ID,
        "product_id": PRODUCT_ID,
        "currency": "usd",
        "billing_type": "one_time",
        "release_method": "buy_now",
        "visibility": "hidden",
        "initial_price": round(ads * PRICE_PER_AD, 2),
        "title": f"{ads} ad upload{'s' if ads != 1 else ''}",
        "internal_notes": f"codec399 bot: {ads} x ${PRICE_PER_AD:g}",
        "metadata": {"codec399_ads": ads},
    }


def gold_variant_body():
    """A hidden renewal variant: GOLD_PRICE now and every GOLD_DAYS days."""
    return {
        "account_id": COMPANY_ID,
        "product_id": PRODUCT_ID,
        "currency": "usd",
        "billing_type": "renewal",
        "billing_period": GOLD_DAYS,
        "release_method": "buy_now",
        "visibility": "hidden",
        "initial_price": GOLD_PRICE,
        "renewal_price": GOLD_PRICE,
        "title": "Gold: unlimited uploads",
        "internal_notes": f"codec399 bot: Gold ${GOLD_PRICE:g} every {GOLD_DAYS} days",
        "metadata": {"codec399_gold": True},
    }


def checkout_body(variant_id, email, ads=0, gold=False, discord_id=None):
    """The checkout configuration that carries the order to the webhook."""
    meta = {"email": _check_email(email)}
    if gold:
        meta["gold"] = True
    else:
        meta["ads"] = int(ads)
    if discord_id:
        meta["discord_id"] = str(discord_id)
    body = {"mode": "payment", "variant_id": variant_id, "metadata": meta}
    if _redirect():
        body["redirect_url"] = _redirect()
    return body


# ---------------------------------------------------------------- HTTP

def _request(method, path, body=None, params=None):
    if configured():
        raise WhopError("Whop is not configured: set " + ", ".join(configured()) + ".")
    url = f"{API_URL}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json",
                 "Accept": "application/json", "User-Agent": "codec399-bot/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:500]
        raise WhopError(f"Whop answered {exc.code} for {method} {path}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise WhopError(f"Could not reach Whop: {exc}") from None


# ---------------------------------------------------------------- variants, cached

def _load_cache():
    try:
        with open(VARIANTS_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, ValueError):
        return {}


def _save_cache(cache):
    os.makedirs(os.path.dirname(VARIANTS_FILE) or ".", exist_ok=True)
    tmp = VARIANTS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cache, fh, indent=2, sort_keys=True)
    os.replace(tmp, VARIANTS_FILE)


def _matches(variant, key, ads=None):
    """A variant made by this client, still hidden-or-visible (not archived) and at today's price."""
    meta = variant.get("metadata") or {}
    if key == "gold":
        return bool(meta.get("codec399_gold")) and float(variant.get("renewal_price") or 0) == GOLD_PRICE
    return meta.get("codec399_ads") == ads and float(variant.get("initial_price") or 0) == round(ads * PRICE_PER_AD, 2)


def _find_existing(key, ads=None):
    """Scan the product's variants for one we created earlier (the cache file may have been lost)."""
    params = {"account_id": COMPANY_ID, "product_id": PRODUCT_ID, "first": 100}
    for _page in range(10):
        resp = _request("GET", "variants", params=params)
        for v in resp.get("data") or []:
            if _matches(v, key, ads):
                return v["id"]
        info = resp.get("page_info") or {}
        if not info.get("has_next_page") or not info.get("end_cursor"):
            return None
        params["after"] = info["end_cursor"]
    return None


def variant_for(key, ads=None):
    """Return the variant id for `ads` uploads (key "ads") or for Gold (key "gold"), creating it once."""
    if key == "gold" and GOLD_PLAN_ID:
        return GOLD_PLAN_ID
    cache_key = "gold" if key == "gold" else f"ads:{int(ads)}"
    price = GOLD_PRICE if key == "gold" else round(int(ads) * PRICE_PER_AD, 2)
    with _lock:
        cache = _load_cache()
        hit = cache.get(cache_key)
        if hit and hit.get("price") == price:
            return hit["id"]
        vid = _find_existing(key, ads)
        if not vid:
            body = gold_variant_body() if key == "gold" else ads_variant_body(ads)
            resp = _request("POST", "variants", body)
            vid = resp.get("id")
            if not vid:
                raise WhopError(f"Whop created no variant: {json.dumps(resp)[:300]}")
        cache[cache_key] = {"id": vid, "price": price}
        _save_cache(cache)
        return vid


def _purchase_url(resp):
    url = resp.get("purchase_url") or ""
    if not url:
        raise WhopError(f"Whop returned no purchase_url: {json.dumps(resp)[:300]}")
    if url.startswith("/"):
        url = "https://whop.com" + url
    return url


def ads_checkout(email, ads, discord_id=None):
    """Create the checkout for `ads` uploads; returns (purchase_url, dollars)."""
    ads = int(ads)
    email = _check_email(email)
    if not 1 <= ads <= MAX_ADS:
        raise ValueError(f"Choose between 1 and {MAX_ADS} ads.")
    vid = variant_for("ads", ads)
    resp = _request("POST", "checkout_configurations", checkout_body(vid, email, ads=ads, discord_id=discord_id))
    return _purchase_url(resp), round(ads * PRICE_PER_AD, 2)


def gold_checkout(email, discord_id=None):
    email = _check_email(email)
    vid = variant_for("gold")
    resp = _request("POST", "checkout_configurations", checkout_body(vid, email, gold=True, discord_id=discord_id))
    return _purchase_url(resp), GOLD_PRICE
