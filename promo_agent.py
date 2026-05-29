"""
LaunchFlow Promotion Agent
Runs as a background daemon thread inside the LaunchFlow server.
Posts to Pinterest, Reddit, Bluesky, Threads, and Telegram on a rotating schedule.

Env vars needed (set in Render dashboard):
  ANTHROPIC_API_KEY          — for AI content generation
  PINTEREST_ACCESS_TOKEN     — Pinterest API v5 token
  PINTEREST_BOARD_ID         — board to post pins to
  LAUNCHFLOW_PROMO_IMAGE     — default promo image URL for Pinterest
  REDDIT_CLIENT_ID           — Reddit app client ID
  REDDIT_CLIENT_SECRET       — Reddit app client secret
  REDDIT_USERNAME            — Reddit account username
  REDDIT_PASSWORD            — Reddit account password
  BLUESKY_HANDLE             — e.g. yourname.bsky.social
  BLUESKY_APP_PASSWORD       — Bluesky app password (not login password)
  THREADS_ACCESS_TOKEN       — Meta Threads API access token
  THREADS_USER_ID            — Threads user ID
  TELEGRAM_BOT_TOKEN         — Telegram bot token
  TELEGRAM_CHANNEL_ID        — channel to post in (e.g. @yourchannel or -100xxx)
"""

import os
import time
import logging
import threading
import requests

logger = logging.getLogger("launchflow.promo")

# ── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
SERVICE_NAME        = "LaunchFlow"
SERVICE_URL         = os.getenv("BASE_URL", "https://launchflow.onrender.com").rstrip("/")
SERVICE_DESCRIPTION = (
    "LaunchFlow is an AI-powered platform where anyone can build a dropshipping store "
    "in minutes — no inventory, no upfront cost. It generates your entire store with AI, "
    "handles payments via Stripe Connect, and includes profit calculators, supplier tracking, "
    "AI product copy, and discount codes."
)

PINTEREST_TOKEN     = os.getenv("PINTEREST_ACCESS_TOKEN", "")
PINTEREST_BOARD_ID  = os.getenv("PINTEREST_BOARD_ID", "")
PINTEREST_IMAGE     = os.getenv("LAUNCHFLOW_PROMO_IMAGE", "")

REDDIT_CLIENT_ID     = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
REDDIT_USERNAME      = os.getenv("REDDIT_USERNAME", "")
REDDIT_PASSWORD      = os.getenv("REDDIT_PASSWORD", "")
REDDIT_SUBREDDITS    = [
    "dropshipping", "ecommerce", "entrepreneur",
    "sidehustle", "passive_income", "smallbusiness",
]

BLUESKY_HANDLE   = os.getenv("BLUESKY_HANDLE", "")
BLUESKY_PASSWORD = os.getenv("BLUESKY_APP_PASSWORD", "")

THREADS_TOKEN   = os.getenv("THREADS_ACCESS_TOKEN", "")
THREADS_USER_ID = os.getenv("THREADS_USER_ID", "")

TELEGRAM_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "")

# Post once every 4 hours. Each platform has a cycle_every multiplier
# so they don't all fire every single run.
CYCLE_SECONDS = 4 * 3600

PLATFORM_FREQUENCY = {
    # platform: post every Nth cycle
    "bluesky":   1,   # every run  (~6x/day)
    "threads":   2,   # every 2nd  (~3x/day)
    "telegram":  2,   # every 2nd  (~3x/day)
    "pinterest": 3,   # every 3rd  (~2x/day)
    "reddit":    6,   # every 6th  (~1x/day)
}

# ── Content angles ───────────────────────────────────────────────────────────
ANGLES = [
    {
        "hook": "I built a full dropshipping store in under 10 minutes using AI",
        "angle": "AI builds your entire store automatically — products, copy, design",
    },
    {
        "hook": "How to sell products online without buying any inventory",
        "angle": "zero upfront cost dropshipping model — pay only after you get paid",
    },
    {
        "hook": "This tool finds winning products AND writes your ad copy for you",
        "angle": "all-in-one AI product research and copywriting for dropshippers",
    },
    {
        "hook": "The only dropshipping platform that pays sellers automatically via Stripe",
        "angle": "Stripe Connect seller payouts — automated, instant, trustworthy",
    },
    {
        "hook": "Profit calculator built in — know your margins before you list anything",
        "angle": "profit calculator, supplier tracker, and discount codes in one place",
    },
    {
        "hook": "Why most dropshippers fail (and what the smart ones do differently)",
        "angle": "AI removes the guesswork — product research, copy, and pricing all automated",
    },
    {
        "hook": "Side hustle idea: build an AI dropshipping store this weekend",
        "angle": "low barrier, AI-powered, start generating income fast",
    },
    {
        "hook": "This is what a modern dropshipping platform actually looks like in 2025",
        "angle": "AI-first store builder vs outdated Shopify-style setups",
    },
]

# ── State ────────────────────────────────────────────────────────────────────
_angle_index  = 0
_cycle_count  = 0
_reddit_index = 0


def _next_angle():
    global _angle_index
    angle = ANGLES[_angle_index % len(ANGLES)]
    _angle_index += 1
    return angle


def _next_subreddit():
    global _reddit_index
    sub = REDDIT_SUBREDDITS[_reddit_index % len(REDDIT_SUBREDDITS)]
    _reddit_index += 1
    return sub


# ── Content generation ───────────────────────────────────────────────────────
def _generate(platform: str, angle: dict) -> str:
    if not ANTHROPIC_API_KEY:
        return _fallback(platform, angle)
    try:
        import anthropic
        instructions = {
            "bluesky": (
                "Write a Bluesky post (max 290 chars). Punchy and conversational, "
                "like a real person sharing a discovery. End with the URL. No hashtag spam."
            ),
            "threads": (
                "Write a Threads post (max 450 chars). Casual, engaging, relatable. "
                "Could be a tip, a story opener, or an observation. End with the link."
            ),
            "telegram": (
                "Write a Telegram channel message (2-3 sentences). Informative, "
                "direct, with a clear call to action and the link at the end."
            ),
            "pinterest": (
                "Write a Pinterest pin description (100-200 chars). "
                "Keyword-rich for SEO, benefit-focused, no clickbait. "
                "Naturally include 2-3 relevant keywords."
            ),
        }
        prompt = (
            f"You are writing promotional content for {SERVICE_NAME}.\n\n"
            f"About: {SERVICE_DESCRIPTION}\n"
            f"URL: {SERVICE_URL}\n"
            f"Hook to riff on: {angle['hook']}\n"
            f"Angle: {angle['angle']}\n\n"
            f"Platform instructions: {instructions.get(platform, 'Write a short social post.')}\n\n"
            "Return ONLY the post text, nothing else. Do not add quotes around it."
        )
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        logger.warning("AI generation failed (%s), using fallback: %s", platform, e)
        return _fallback(platform, angle)


def _generate_reddit(angle: dict) -> tuple[str, str]:
    """Returns (title, body)."""
    if not ANTHROPIC_API_KEY:
        return (angle["hook"], f"{angle['angle']}\n\nCheck it out: {SERVICE_URL}")
    try:
        import anthropic
        prompt = (
            f"You are writing a Reddit post for r/dropshipping or r/entrepreneur about {SERVICE_NAME}.\n\n"
            f"About: {SERVICE_DESCRIPTION}\n"
            f"URL: {SERVICE_URL}\n"
            f"Hook: {angle['hook']}\n"
            f"Angle: {angle['angle']}\n\n"
            "Write an authentic, non-spammy Reddit post. Share a genuine insight or story. "
            "Mention the tool naturally — don't start with the tool name. "
            "Format:\nTITLE: <title under 200 chars>\nBODY: <2-3 paragraph body that reads like a real person sharing advice>\n\n"
            "The body should end naturally with the URL as a resource, not a hard sell."
        )
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        title, body = angle["hook"], text
        if "TITLE:" in text and "BODY:" in text:
            parts = text.split("BODY:", 1)
            title = parts[0].replace("TITLE:", "").strip()
            body = parts[1].strip()
        return title, body
    except Exception as e:
        logger.warning("Reddit AI generation failed: %s", e)
        return (angle["hook"], f"{angle['angle']}\n\n{SERVICE_URL}")


def _fallback(platform: str, angle: dict) -> str:
    return f"{angle['hook']} — {SERVICE_URL}"


# ── Platform posting ─────────────────────────────────────────────────────────
def _post_bluesky(text: str) -> bool:
    if not (BLUESKY_HANDLE and BLUESKY_PASSWORD):
        return False
    try:
        from atproto import Client
        client = Client()
        client.login(BLUESKY_HANDLE, BLUESKY_PASSWORD)
        client.send_post(text=text[:300])
        logger.info("Bluesky: posted")
        return True
    except Exception as e:
        logger.error("Bluesky post failed: %s", e)
        return False


def _post_threads(text: str) -> bool:
    if not (THREADS_TOKEN and THREADS_USER_ID):
        return False
    try:
        base = f"https://graph.threads.net/v1.0/{THREADS_USER_ID}"
        r = requests.post(f"{base}/threads", params={
            "media_type": "TEXT",
            "text": text[:500],
            "access_token": THREADS_TOKEN,
        }, timeout=15)
        container_id = r.json().get("id")
        if not container_id:
            logger.error("Threads: no container_id — %s", r.text)
            return False
        requests.post(f"{base}/threads_publish", params={
            "creation_id": container_id,
            "access_token": THREADS_TOKEN,
        }, timeout=15)
        logger.info("Threads: posted")
        return True
    except Exception as e:
        logger.error("Threads post failed: %s", e)
        return False


def _post_telegram(text: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHANNEL):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHANNEL, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
        if r.ok:
            logger.info("Telegram: posted")
            return True
        logger.error("Telegram error: %s", r.text)
        return False
    except Exception as e:
        logger.error("Telegram post failed: %s", e)
        return False


def _post_pinterest(text: str, title: str) -> bool:
    if not (PINTEREST_TOKEN and PINTEREST_BOARD_ID and PINTEREST_IMAGE):
        return False
    try:
        r = requests.post(
            "https://api.pinterest.com/v5/pins",
            headers={"Authorization": f"Bearer {PINTEREST_TOKEN}"},
            json={
                "link": SERVICE_URL,
                "title": title[:100],
                "description": text[:500],
                "board_id": PINTEREST_BOARD_ID,
                "media_source": {
                    "source_type": "image_url",
                    "url": PINTEREST_IMAGE,
                },
            },
            timeout=20,
        )
        if r.ok:
            logger.info("Pinterest: pin created")
            return True
        logger.error("Pinterest error: %s", r.text)
        return False
    except Exception as e:
        logger.error("Pinterest post failed: %s", e)
        return False


def _post_reddit(title: str, body: str, subreddit: str) -> bool:
    if not all([REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD]):
        return False
    try:
        import praw
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            username=REDDIT_USERNAME,
            password=REDDIT_PASSWORD,
            user_agent=f"{SERVICE_NAME} Promo Agent v1.0",
        )
        reddit.subreddit(subreddit).submit(title=title, selftext=body)
        logger.info("Reddit: posted to r/%s", subreddit)
        return True
    except Exception as e:
        logger.error("Reddit post failed (r/%s): %s", subreddit, e)
        return False


# ── Cycle ────────────────────────────────────────────────────────────────────
def run_cycle():
    global _cycle_count
    _cycle_count += 1
    angle = _next_angle()
    logger.info("LaunchFlow promo cycle #%d | angle: %s", _cycle_count, angle["hook"][:60])

    def should_post(platform: str) -> bool:
        freq = PLATFORM_FREQUENCY.get(platform, 1)
        return _cycle_count % freq == 0

    if should_post("bluesky"):
        content = _generate("bluesky", angle)
        _post_bluesky(content)

    if should_post("threads"):
        content = _generate("threads", angle)
        _post_threads(content)

    if should_post("telegram"):
        content = _generate("telegram", angle)
        _post_telegram(content)

    if should_post("pinterest"):
        content = _generate("pinterest", angle)
        _post_pinterest(content, title=angle["hook"])

    if should_post("reddit"):
        title, body = _generate_reddit(angle)
        sub = _next_subreddit()
        _post_reddit(title, body, sub)


def run_loop():
    logger.info("LaunchFlow promo agent loop started (interval: %dh)", CYCLE_SECONDS // 3600)
    while True:
        try:
            run_cycle()
        except Exception as e:
            logger.error("Promo cycle uncaught error: %s", e)
        time.sleep(CYCLE_SECONDS)


def start():
    """Call once at server startup to launch the agent as a daemon thread."""
    enabled = any([
        BLUESKY_HANDLE, THREADS_TOKEN, TELEGRAM_TOKEN,
        PINTEREST_TOKEN, REDDIT_CLIENT_ID,
    ])
    if not enabled:
        logger.info("LaunchFlow promo agent: no platform credentials set — skipping")
        return
    t = threading.Thread(target=run_loop, daemon=True, name="launchflow-promo")
    t.start()
    logger.info("LaunchFlow promo agent started")
