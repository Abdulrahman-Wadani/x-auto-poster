# X Auto Poster

A small, fault-tolerant service that publishes posts to X (Twitter) on a randomised daily schedule, using the **official X API** through [tweepy](https://www.tweepy.org/). No browser automation, no scraping.

It has been running unattended on a Linux home server, posting twice a day.

## Features

- **Randomised schedule** – picks one random time inside each configurable daily window (default: 12:00–14:00 and 20:00–23:00), then sleeps until the next day.
- **No repeats** – tracks posted IDs in `posted_log.json` and cycles through the whole pool before reusing a post; never posts the same item twice in a row.
- **Restart-safe** – after a crash or reboot it skips any window that already has a post, so it never double-posts.
- **Optional images** – attaches a random image from `images/` to a configurable share of posts, resized with Pillow. Uploads via the v2 media endpoint with a v1.1 fallback, and posts without the image if the upload keeps failing.
- **Retries with backoff** – transient failures (DNS, timeouts, connection resets, 5xx) are retried with increasing delays; permanent errors (401/403/402) fail fast with a clear message.
- **Corrupt-state guard** – if `posted_log.json` is unreadable it is moved aside and posting stops, rather than starting over and re-posting old content.
- **Structured logs** – `scheduler.log` and `poster.log` record every decision.

## Project structure

```
main.py               # picks a post, uploads the optional image, publishes, records state
scheduler.py          # long-running loop: builds daily random slots and calls main.post_daily_tweet()
tweets.example.json   # sample post pool -> copy to tweets.json
.env.example          # API credential names -> copy to .env
images/               # optional images to attach (not committed)
```

## Setup

Requires Python 3.10+ and an X developer app with **Read and Write** permissions.

```bash
git clone https://github.com/Abdulrahman-Wadani/x-auto-poster.git
cd x-auto-poster
python3 -m venv venv
venv/bin/pip install -r requirements.txt

cp .env.example .env                 # fill in your four X API keys
cp tweets.example.json tweets.json   # add your own posts
```

## Usage

Post once, immediately:

```bash
venv/bin/python main.py
```

Run the scheduler continuously (for example inside `tmux` or as a systemd service):

```bash
venv/bin/python scheduler.py
```

Change the posting windows by editing `WINDOWS` in `scheduler.py` (24-hour clock):

```python
WINDOWS = [
    (12, 14),  # one post between 12:00 and 14:00
    (20, 23),  # one post between 20:00 and 23:00
]
```

## Notes

- `.env`, `tweets.json`, `images/`, logs and state files are git-ignored – never commit your API keys.

## License

MIT, see [LICENSE](LICENSE).
