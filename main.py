"""
Daily X poster built on the official X API (v2) via tweepy.
No browser automation, no human emulation, no Chrome profile.

How it works:
- Picks a random, not-yet-posted entry from tweets.json
- Publishes it, optionally with a random image from images/
- Records posted IDs in posted_log.json so nothing repeats
- Retries automatically on transient network errors (disconnects, DNS, timeouts, 5xx)
- Runs once per invocation (or is called from scheduler.py)
"""

import os
import io
import sys
import json
import time
import random
import logging
from datetime import datetime

import requests
import tweepy
from requests_oauthlib import OAuth1
from dotenv import load_dotenv

try:
    from PIL import Image
except ImportError:  # Pillow is optional: without it images are uploaded at original size
    Image = None

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TWEETS_FILE = os.path.join(BASE_DIR, "tweets.json")
LOG_FILE = os.path.join(BASE_DIR, "posted_log.json")
IMAGES_DIR = os.path.join(BASE_DIR, "images")

IMAGE_CHANCE_PERCENT = 40         # Share of posts that get an image attached
IMAGE_MAX_SIDE = 1600             # Max image side after resizing (pixels)
IMAGE_JPEG_QUALITY = 85
REQUEST_TIMEOUT = (15, 90)        # (connect timeout, read/write timeout) in seconds
RETRY_DELAYS = [30, 60, 120, 240, 300]  # Delays between retries (~12 minutes total)
MIN_POOL_WARNING = 30             # Warn when the post pool is smaller than this

V2_MEDIA_UPLOAD_URL = "https://api.x.com/2/media/upload"

log = logging.getLogger("x_daily_poster")
if not log.handlers:
    _fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    _fh = logging.FileHandler(os.path.join(BASE_DIR, "poster.log"), encoding="utf-8")
    _fh.setFormatter(_fmt)
    _sh = logging.StreamHandler()
    _sh.setFormatter(_fmt)
    log.addHandler(_fh)
    log.addHandler(_sh)
    log.setLevel(logging.INFO)
    log.propagate = False


class CorruptStateError(Exception):
    """State file is corrupt - stop posting instead of re-posting old content."""


# ---------------------------------------------------------------------------
# JSON files
# ---------------------------------------------------------------------------

def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    """Atomic write: write to a temp file, then replace the original in one step."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def load_posted_log():
    try:
        data = load_json(LOG_FILE, {"posted_ids": []})
        if not isinstance(data, dict) or not isinstance(data.get("posted_ids", []), list):
            raise ValueError("invalid file structure")
        return data
    except (json.JSONDecodeError, ValueError, OSError) as e:
        backup = f"{LOG_FILE}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
        try:
            os.replace(LOG_FILE, backup)
        except OSError:
            backup = "(backup failed)"
        raise CorruptStateError(
            f"{LOG_FILE} is corrupt ({e}). Moved to {backup}. "
            "Posting stopped to avoid repeating old posts - check the file, then restart."
        )


# ---------------------------------------------------------------------------
# X clients
# ---------------------------------------------------------------------------

def get_credentials():
    required = ["X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing keys in .env: {', '.join(missing)}.")
    return {k: os.getenv(k) for k in required}


def get_client(creds):
    return tweepy.Client(
        consumer_key=creds["X_API_KEY"],
        consumer_secret=creds["X_API_SECRET"],
        access_token=creds["X_ACCESS_TOKEN"],
        access_token_secret=creds["X_ACCESS_SECRET"],
    )


def get_api_v1(creds):
    auth = tweepy.OAuth1UserHandler(
        creds["X_API_KEY"], creds["X_API_SECRET"],
        creds["X_ACCESS_TOKEN"], creds["X_ACCESS_SECRET"],
    )
    return tweepy.API(auth, timeout=REQUEST_TIMEOUT[1])


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------

def is_transient(exc):
    """Transient errors worth retrying: network, timeouts, 5xx server errors."""
    if isinstance(exc, (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout,
                        tweepy.errors.TwitterServerError,
                        ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code >= 500
    # tweepy sometimes wraps network errors in a TweepyException with "Failed to send request"
    if isinstance(exc, tweepy.errors.TweepyException) and not isinstance(exc, tweepy.errors.HTTPException):
        return "Failed to send request" in str(exc)
    return False


def with_retries(action, what, delays=RETRY_DELAYS):
    """Run action() and retry on transient errors only."""
    attempt = 0
    while True:
        try:
            return action(attempt)
        except Exception as e:
            if not is_transient(e) or attempt >= len(delays):
                raise
            wait = delays[attempt]
            attempt += 1
            log.warning(f"⚠️ {what}: transient error ({short_error(e)}). Attempt {attempt + 1} in {wait} seconds...")
            time.sleep(wait)


def short_error(e, limit=160):
    s = f"{type(e).__name__}: {e}"
    return s if len(s) <= limit else s[:limit] + "..."


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

def pick_random_image():
    if not os.path.isdir(IMAGES_DIR):
        return None
    valid_ext = (".png", ".jpg", ".jpeg")
    images = [f for f in os.listdir(IMAGES_DIR) if f.lower().endswith(valid_ext)]
    if not images:
        return None
    return os.path.join(IMAGES_DIR, random.choice(images))


def prepare_image(path):
    """Resize and compress the image to speed up upload. Returns (bytes, mime)."""
    if Image is None:
        with open(path, "rb") as f:
            data = f.read()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        return data, mime

    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), "image/jpeg"


def upload_media_v2(creds, data, mime):
    auth = OAuth1(creds["X_API_KEY"], creds["X_API_SECRET"],
                  creds["X_ACCESS_TOKEN"], creds["X_ACCESS_SECRET"])
    resp = requests.post(
        V2_MEDIA_UPLOAD_URL,
        auth=auth,
        files={"media": ("image.jpg", data, mime)},
        data={"media_category": "tweet_image"},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    media_id = (resp.json().get("data") or {}).get("id")
    if not media_id:
        raise RuntimeError(f"Unexpected response from v2 media upload: {resp.text[:200]}")
    return str(media_id)


def upload_media_v1(api_v1, data):
    media = api_v1.media_upload(filename="image.jpg", file=io.BytesIO(data))
    return str(media.media_id)


def upload_image(creds, image_path):
    """Upload via v2, falling back to legacy v1.1 if it keeps failing. Returns media_id or None."""
    name = os.path.basename(image_path)
    try:
        data, mime = prepare_image(image_path)
    except Exception as e:
        log.warning(f"Could not prepare image {name}, posting without it: {short_error(e)}")
        return None

    log.info(f"Prepared image {name}: {os.path.getsize(image_path) // 1024}KB -> {len(data) // 1024}KB")

    try:
        media_id = with_retries(lambda _: upload_media_v2(creds, data, mime), "Image upload (v2)", delays=[15, 30, 60])
        log.info(f"Image uploaded: {name}")
        return media_id
    except Exception as e:
        log.warning(f"v2 image upload failed ({short_error(e)}), trying v1.1...")

    try:
        media_id = with_retries(lambda _: upload_media_v1(get_api_v1(creds), data), "Image upload (v1.1)", delays=[15, 30])
        log.info(f"Image uploaded (v1.1): {name}")
        return media_id
    except Exception as e:
        log.warning(f"Image upload failed, posting without it: {short_error(e)}")
        return None


# ---------------------------------------------------------------------------
# Post selection
# ---------------------------------------------------------------------------

def pick_unused_tweet(all_tweets, posted_ids, last_id=None, exclude=()):
    available = [t for t in all_tweets if t["id"] not in posted_ids and t["id"] not in exclude]
    if not available:
        log.info("All posts have been used, starting a new cycle.")
        posted_ids.clear()
        # Never repeat the last posted entry right after a new cycle starts
        available = [t for t in all_tweets if t["id"] != last_id and t["id"] not in exclude] or \
                    [t for t in all_tweets if t["id"] not in exclude]
    return random.choice(available) if available else None


def is_duplicate_error(e):
    return isinstance(e, tweepy.errors.Forbidden) and "duplicate" in str(e).lower()


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------

def post_daily_tweet():
    log.info("Starting daily posting cycle...")

    try:
        tweets_data = load_json(TWEETS_FILE, {"tweets": []})
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"❌ Could not read {TWEETS_FILE}: {e}")
        return False

    all_tweets = tweets_data.get("tweets", [])
    if not all_tweets:
        log.error(f"{TWEETS_FILE} is empty or missing. Nothing to post.")
        return False
    if len(all_tweets) < MIN_POOL_WARNING:
        log.info(f"ℹ️ Only {len(all_tweets)} posts in the pool - add more to reduce repetition.")

    try:
        log_data = load_posted_log()
    except CorruptStateError as e:
        log.error(f"❌ {e}")
        return False

    posted_ids = set(log_data.get("posted_ids", []))
    last_id = log_data.get("last_id")

    try:
        creds = get_credentials()
        client = get_client(creds)
    except RuntimeError as e:
        log.error(f"❌ {e}")
        return False

    media_ids = None
    if random.randint(1, 100) <= IMAGE_CHANCE_PERCENT:
        image_path = pick_random_image()
        if image_path:
            media_id = upload_image(creds, image_path)
            if media_id:
                media_ids = [media_id]

    skipped = set()
    for _ in range(3):  # If X rejects the post as a duplicate, try another one
        tweet_obj = pick_unused_tweet(all_tweets, posted_ids, last_id, exclude=skipped)
        if tweet_obj is None:
            log.error("❌ No postable entry left (all rejected as duplicates).")
            return False

        try:
            def send(attempt):
                try:
                    return client.create_tweet(text=tweet_obj["text"], media_ids=media_ids)
                except tweepy.errors.Forbidden as e:
                    # If the connection dropped after the request arrived, the retry gets "duplicate",
                    # meaning the post was already published by the previous attempt
                    if attempt > 0 and is_duplicate_error(e):
                        log.info("Post was already published by the previous attempt (X replied: duplicate).")
                        return None
                    raise

            response = with_retries(send, "Publishing post")

        except tweepy.errors.Forbidden as e:
            if is_duplicate_error(e):
                log.warning(f"X rejected post {tweet_obj['id']} as a duplicate, trying another...")
                skipped.add(tweet_obj["id"])
                continue
            log.error(f"❌ Forbidden - check the app permissions (Read+Write) in the X Developer Portal: {e}")
            return False
        except tweepy.errors.TooManyRequests:
            log.error("❌ Rate limit exceeded. Try again later.")
            return False
        except tweepy.errors.Unauthorized as e:
            log.error(f"❌ Unauthorized - check the keys in .env: {e}")
            return False
        except tweepy.errors.HTTPException as e:
            if getattr(e.response, "status_code", None) == 402:
                log.error("❌ 402 Payment Required - X API credits/subscription ran out. Check billing in the X Developer Portal.")
            else:
                log.error(f"❌ X API error: {short_error(e, 300)}")
            return False
        except Exception as e:
            log.error(f"❌ Posting failed after all retries: {short_error(e, 300)}")
            return False

        tweet_id = response.data.get("id") if response is not None else None
        if tweet_id:
            log.info(f"✅ Posted successfully ({tweet_obj['id']}). Link: https://x.com/i/web/status/{tweet_id}")
        else:
            log.info(f"✅ Posted successfully ({tweet_obj['id']}).")

        posted_ids.add(tweet_obj["id"])
        log_data["posted_ids"] = sorted(posted_ids)
        log_data["last_id"] = tweet_obj["id"]
        log_data["last_post_at"] = datetime.now().isoformat()
        try:
            save_json(LOG_FILE, log_data)
        except OSError as e:
            log.error(f"⚠️ Posted, but could not save {LOG_FILE}: {e}")
        return True

    log.error("❌ Could not post: X rejected several posts as duplicates.")
    return False


if __name__ == "__main__":
    sys.exit(0 if post_daily_tweet() else 1)
