import os
import re
import sys
import time
import json
import logging
import requests
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("telegram-groupme")

DEFAULT_CHANNELS = (
    "moriahdoron,MichaelShemesh,SuliandItay,amitsegal,US2020US,"
    "barak_betesh,yaronyanir1299,tamirmorag14,kastelmoti14,HallelBittonRosen"
)
CHANNELS = [c.strip() for c in os.environ.get("TELEGRAM_CHANNELS", DEFAULT_CHANNELS).split(",") if c.strip()]
GROUPME_BOT_ID = os.environ["GROUPME_BOT_ID"]
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/seen.json")
MAX_BACKFILL = int(os.environ.get("MAX_BACKFILL", "5"))
GROUPME_ACCESS_TOKEN = os.environ["GROUPME_ACCESS_TOKEN"]  # from dev.groupme.com, needed to upload images

GROUPME_POST_URL = "https://api.groupme.com/v3/bots/post"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def load_seen():
    try:
        with open(STATE_FILE, "r") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_seen(seen):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    trimmed = list(seen)[-500:]
    with open(STATE_FILE, "w") as f:
        json.dump(trimmed, f)


def split_message(text, limit=1000, reserve=10):
    """Split text into chunks that fit within GroupMe's 1000-char limit,
    leaving `reserve` characters of headroom for an "(i/n) " prefix added
    afterward. Splits on the nearest word boundary where possible."""
    max_len = limit - reserve
    chunks = []
    remaining = text
    while len(remaining) > max_len:
        cut = remaining.rfind(" ", 0, max_len)
        if cut == -1:
            cut = max_len
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def post_to_groupme(body, signature, picture_url=None):
    limit = 1000
    reserve = 10  # room for "(i/n) " prefix
    max_len = limit - reserve

    chunks = split_message(body, limit=limit, reserve=reserve)
    if not chunks:
        chunks = [""]

    # Try to attach the full signature to the last chunk without splitting
    # it. If it doesn't fit there, give the signature its own final message
    # instead of risking it getting cut mid-way.
    last_with_sig = f"{chunks[-1]}\n\n{signature}".strip()
    if len(last_with_sig) <= max_len:
        chunks[-1] = last_with_sig
    else:
        chunks.append(signature)

    total = len(chunks)
    for i, chunk in enumerate(chunks, start=1):
        prefix = f"({i}/{total}) " if total > 1 else ""
        payload = {"bot_id": GROUPME_BOT_ID, "text": prefix + chunk}
        if picture_url and i == 1:
            payload["picture_url"] = picture_url
        resp = requests.post(GROUPME_POST_URL, json=payload, timeout=15)
        if resp.status_code >= 300:
            log.error("GroupMe post failed (%s): %s", resp.status_code, resp.text)
        else:
            log.info("Posted: %s", (prefix + chunk)[:80])
        if total > 1 and i < total:
            time.sleep(1)


def upload_image_to_groupme(image_url):
    try:
        img_resp = requests.get(image_url, headers=HEADERS, timeout=15)
        img_resp.raise_for_status()
        upload_resp = requests.post(
            "https://image.groupme.com/pictures",
            headers={
                "X-Access-Token": GROUPME_ACCESS_TOKEN,
                "Content-Type": img_resp.headers.get("Content-Type", "image/jpeg"),
            },
            data=img_resp.content,
            timeout=20,
        )
        upload_resp.raise_for_status()
        return upload_resp.json()["payload"]["url"]
    except Exception:
        log.exception("Failed to upload image to GroupMe")
        return None


def fetch_messages(channel):
    """
    Scrape t.me/s/<channel> — Telegram's public web preview.
    Each message lives in a div with class 'tgme_widget_message' and has a
    data-post attribute like 'channelname/1234' which we use as a stable ID.
    NOTE: Telegram's markup has changed before and may change again — if this
    stops finding messages, open t.me/s/<channel> in a browser, view source,
    and check these class names still match.
    """
    preview_url = f"https://t.me/s/{channel}"
    resp = requests.get(preview_url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    messages = []
    for block in soup.select("div.tgme_widget_message"):
        post_id = block.get("data-post")  # e.g. "moriahdoron/4821"
        if not post_id:
            continue

        # Strip any quoted/replied-to message first — it has its own
        # tgme_widget_message_text div that would otherwise get matched
        # instead of the actual new message text.
        reply_block = block.select_one("div.tgme_widget_message_reply")
        if reply_block:
            reply_block.decompose()

        # A message quoting/replying to another has TWO tgme_widget_message_text
        # divs — the quoted one first, the actual new message last. Taking the
        # last one skips the quote regardless of what wrapper class Telegram uses.
        text_divs = block.select("div.tgme_widget_message_text")
        text_div = text_divs[-1] if text_divs else None

        # NOTE: GroupMe's official app has no Markdown/bold-text support in
        # message bodies — *word* is just displayed as literal asterisks.
        # So we don't try to preserve Telegram's bold formatting; just pull
        # the plain text.
        if text_div:
            text = text_div.get_text("\n", strip=True)
        else:
            text = ""

        link_tag = block.select_one("a.tgme_widget_message_date")
        link = link_tag["href"] if link_tag and link_tag.get("href") else f"https://t.me/{post_id}"

        # If the channel has "Sign messages" turned on, Telegram renders the
        # author's name as its own element next to the date/time (NOT as
        # part of the message text), e.g.:
        #   <span class="tgme_widget_message_from_author">Doron Kadosh</span>
        # Grab it separately so we can append it to the outgoing message.
        author_tag = block.select_one("span.tgme_widget_message_from_author")
        author = author_tag.get_text(strip=True) if author_tag else None

        # If this post is a forward from another channel, Telegram shows a
        # "Forwarded from X" element above the message content.
        forward_tag = block.select_one("a.tgme_widget_message_forwarded_from_name")
        forwarded_from = forward_tag.get_text(strip=True) if forward_tag else None

        # The channel's real display name (e.g. in Hebrew), shown per-post
        # in the preview next to the avatar — separate from the raw URL
        # username and separate from the per-message "Sign messages" author.
        channel_name_tag = block.select_one("a.tgme_widget_message_owner_name span")
        channel_display_name = channel_name_tag.get_text(strip=True) if channel_name_tag else channel

        # Photo posts render as <a> tags with a background-image inline style
        # rather than <img> tags. A single post can have multiple photos
        # (an album/media group), so collect all of them, not just the first.
        photo_els = block.select("a.tgme_widget_message_photo_wrap")
        image_urls = []
        for photo_el in photo_els:
            if photo_el.get("style"):
                match = re.search(r"background-image:\s*url\('(.+?)'\)", photo_el["style"])
                if match:
                    image_urls.append(match.group(1))

        # NOTE: Telegram's public t.me/s/ widget does NOT expose a distinct
        # "original author of the forwarded post" field separate from
        # forwarded_from — there's only one forwarded-from name element.
        # If the receiving channel (n12chat) has "Sign messages" on, the
        # per-post author_tag signature sometimes carries through as the
        # original signer even on forwarded posts, so we reuse it here —
        # but this is an assumption, not a confirmed distinct selector.
        # Verify against a real forwarded n12chat post if this looks wrong.
        forwarded_from_author = author if forwarded_from else None

        if text:  # skip pure media posts with no caption for now
            messages.append({
                "id": post_id,
                "text": text,
                "link": link,
                "image_urls": image_urls,
                "author": author,
                "channel": channel_display_name,
                "forwarded_from": forwarded_from,
                "forwarded_from_author": forwarded_from_author,
            })

    return messages


def format_message(msg):
    if msg.get("forwarded_from"):
        original_source = msg.get("forwarded_from_author") or msg["forwarded_from"]
        signature = f"{msg['channel']} — fwd from {original_source}"
    else:
        signature = msg.get("author") or msg["channel"]
    return msg["text"], f"— {signature}"


def poll_once(seen, first_run):
    all_messages = []
    for channel in CHANNELS:
        try:
            all_messages.extend(fetch_messages(channel))
        except Exception:
            log.exception("Failed to fetch/parse Telegram preview page for %s", channel)

    if first_run:
        # Mark every currently-existing post as seen WITHOUT posting any of
        # them — the bot should only announce posts that appear after this
        # first run, not backfill old channel history.
        for msg in all_messages:
            seen.add(msg["id"])
        log.info("First run: marked %d existing posts as seen, posting nothing", len(all_messages))
        save_seen(seen)
        return seen

    new_messages = [m for m in all_messages if m["id"] not in seen]

    if len(new_messages) > MAX_BACKFILL:
        new_messages = new_messages[-MAX_BACKFILL:]

    for msg in new_messages:
        image_urls = msg.get("image_urls") or []
        first_image = upload_image_to_groupme(image_urls[0]) if image_urls else None
        body, signature = format_message(msg)
        post_to_groupme(body, signature, picture_url=first_image)
        seen.add(msg["id"])
        time.sleep(1)

        # GroupMe's bot API only supports one image per message, so any
        # additional photos in the same Telegram post (an album) go out as
        # separate image-only follow-up messages.
        for extra_url in image_urls[1:]:
            extra_image = upload_image_to_groupme(extra_url)
            if extra_image:
                post_to_groupme("", "", picture_url=extra_image)
                time.sleep(1)

    if new_messages:
        save_seen(seen)

    return seen


def main():
    log.info("Starting Telegram -> GroupMe bot. Channels: %s Poll interval: %ss", ", ".join(CHANNELS), POLL_SECONDS)
    seen = load_seen()
    first_run = len(seen) == 0
    while True:
        try:
            seen = poll_once(seen, first_run)
            first_run = False
        except Exception:
            log.exception("Error during poll cycle")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
