"""
AI news digest -> Telegram.

Reads several AI news RSS/Atom feeds, ranks the stories, picks the top 5
and sends them to a Telegram chat. Uses only the Python standard library.

Usage:
    python ai_news_bot.py            # fetch, rank, send to Telegram
    python ai_news_bot.py --dry-run  # fetch, rank, print only (no Telegram)

Config (in a .env file next to this script, or as environment variables):
    TELEGRAM_BOT_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=123456789
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------- settings

FEEDS = {
    "TechCrunch": "https://techcrunch.com/category/artificial-intelligence/feed/",
    "The Verge": "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "VentureBeat": "https://venturebeat.com/category/ai/feed/",
    "MIT Tech Review": "https://www.technologyreview.com/topic/artificial-intelligence/feed",
    "Ars Technica": "https://arstechnica.com/ai/feed/",
}

TOP_N = 5
MAX_AGE_HOURS = 48  # ignore stories older than this

# Words that make a story more likely to be "big" AI news. Weight = bonus points.
KEYWORDS = {
    "openai": 3, "anthropic": 3, "claude": 3, "gpt": 3, "gemini": 3,
    "deepmind": 3, "llama": 2, "mistral": 2, "nvidia": 2, "meta": 1,
    "google": 1, "microsoft": 1, "launch": 2, "releases": 2, "announces": 2,
    "model": 1, "agent": 1, "regulation": 2, "funding": 1, "raises": 1,
    "acquires": 2, "open source": 2, "benchmark": 1, "breakthrough": 2,
}

BASE_DIR = Path(__file__).resolve().parent
SENT_FILE = BASE_DIR / "sent_links.json"  # remembers what was already sent
USER_AGENT = "ai-news-digest/1.0 (+rss reader)"

ATOM = "{http://www.w3.org/2005/Atom}"

# ---------------------------------------------------------------- helpers


def load_env(path: Path) -> None:
    """Minimal .env loader: KEY=value lines, # comments, optional quotes."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Story:
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str = ""
    score: float = 0.0
    also_in: int = 0  # how many other feeds covered a similar story


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:  # RSS: "Sat, 22 Nov 2026 10:00:00 +0000"
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:  # Atom: "2026-11-22T10:00:00Z"
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_feed(source: str, data: bytes) -> list[Story]:
    root = ET.fromstring(data)
    stories: list[Story] = []

    # RSS 2.0
    for item in root.iter("item"):
        stories.append(Story(
            title=strip_html(item.findtext("title", "")),
            link=(item.findtext("link") or "").strip(),
            source=source,
            published=parse_date(item.findtext("pubDate")),
            summary=strip_html(item.findtext("description", "")),
        ))

    # Atom
    for entry in root.iter(f"{ATOM}entry"):
        link = ""
        for l in entry.findall(f"{ATOM}link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        stories.append(Story(
            title=strip_html(entry.findtext(f"{ATOM}title", "")),
            link=link.strip(),
            source=source,
            published=parse_date(entry.findtext(f"{ATOM}published")
                                 or entry.findtext(f"{ATOM}updated")),
            summary=strip_html(entry.findtext(f"{ATOM}summary", "")),
        ))

    return [s for s in stories if s.title and s.link]


def title_words(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower())
    return {w for w in words if len(w) > 3}


def similar(a: Story, b: Story) -> bool:
    wa, wb = title_words(a.title), title_words(b.title)
    if not wa or not wb:
        return False
    return len(wa & wb) / min(len(wa), len(wb)) >= 0.5


def rank(stories: list[Story], now: datetime) -> list[Story]:
    # Coverage: a story reported by several outlets is probably important.
    for s in stories:
        others = {o.source for o in stories if o.source != s.source and similar(s, o)}
        s.also_in = len(others)

    for s in stories:
        text = f"{s.title} {s.summary}".lower()
        keyword_score = sum(w for k, w in KEYWORDS.items() if re.search(rf"\b{re.escape(k)}", text))
        if s.published:
            age_h = max((now - s.published).total_seconds() / 3600, 0)
            recency = max(0.0, 10 * (1 - age_h / MAX_AGE_HOURS))
        else:
            recency = 2.0
        s.score = recency + keyword_score + 4 * s.also_in

    stories.sort(key=lambda s: s.score, reverse=True)

    # Drop near-duplicates so the top 5 are 5 different stories.
    picked: list[Story] = []
    for s in stories:
        if not any(similar(s, p) for p in picked):
            picked.append(s)
    return picked


def load_sent() -> list[str]:
    """Links sent on earlier runs, oldest first."""
    if SENT_FILE.exists():
        try:
            return list(json.loads(SENT_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_sent(new_links: list[str]) -> None:
    links = load_sent() + [l for l in new_links if l not in load_sent()]
    # Keep the file small: the most recent 500 links are plenty.
    SENT_FILE.write_text(json.dumps(links[-500:], indent=0), encoding="utf-8")


def format_message(stories: list[Story], now: datetime) -> str:
    lines = [f"<b>🤖 Top {len(stories)} AI stories</b> — {now:%d %b %Y}", ""]
    for i, s in enumerate(stories, 1):
        lines.append(f'{i}. <a href="{html.escape(s.link, quote=True)}">{html.escape(s.title, quote=False)}</a>')
        meta = s.source
        if s.also_in:
            meta += f" (+{s.also_in} other source{'s' if s.also_in > 1 else ''})"
        lines.append(f"   <i>{html.escape(meta)}</i>")
        if s.summary:
            short = s.summary[:180].rsplit(" ", 1)[0] + "…" if len(s.summary) > 180 else s.summary
            lines.append(f"   {html.escape(short, quote=False)}")
        lines.append("")
    return "\n".join(lines).strip()


def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Telegram error {e.code}: {e.read().decode(errors='replace')}")
    if not result.get("ok"):
        raise SystemExit(f"Telegram error: {result}")


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Send the top AI news stories to Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="print the digest instead of sending it")
    parser.add_argument("--include-sent", action="store_true", help="don't skip stories sent on earlier runs")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # emoji-safe output on Windows

    load_env(BASE_DIR / ".env")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not args.dry_run and not (token and chat_id):
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env (or use --dry-run).", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)

    stories: list[Story] = []
    for source, url in FEEDS.items():
        try:
            items = parse_feed(source, fetch(url))
            print(f"[ok]   {source}: {len(items)} items")
            stories.extend(items)
        except Exception as e:  # one broken feed shouldn't stop the digest
            print(f"[fail] {source}: {e}", file=sys.stderr)

    stories = [s for s in stories if s.published is None or s.published >= cutoff]
    sent = set() if args.include_sent else set(load_sent())
    ranked = rank(stories, now)
    top = [s for s in ranked if s.link not in sent][:TOP_N]

    if not top:
        print("No new stories to send.")
        return 0

    message = format_message(top, now)

    if args.dry_run:
        print("\n" + message)
        return 0

    send_telegram(token, chat_id, message)
    save_sent([s.link for s in top])
    print(f"Sent {len(top)} stories to Telegram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
