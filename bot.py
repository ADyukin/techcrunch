from __future__ import annotations

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


MSK = ZoneInfo("Europe/Moscow")
CATEGORY_URL = "https://techcrunch.com/category/apps/"
STATE_PATH = Path("work/state.json")
USER_AGENT = "Mozilla/5.0 (compatible; TechCrunchAppsDigestBot/1.0)"
ARTICLE_LINK_RE = re.compile(r"https://techcrunch\.com/\d{4}/\d{2}/\d{2}/[^\"'#?]+/?")
IMAGE_RE = re.compile(r"https://[^\"' )>]+\.(?:jpg|jpeg|png|webp)(?:\?[^\"' )>]*)?", re.IGNORECASE)
BAD_IMAGE_MARKERS = (
    "tc-logo",
    "lockup",
    "headshot",
    "avatar",
    "profile",
    "author",
    "disrupt",
    "event",
    "promo",
    "advert",
    "icon",
    "logo",
)


@dataclass
class ArticlePreview:
    title: str
    url: str


@dataclass
class Article:
    title: str
    url: str
    author: str
    published_at: str
    summary: str
    images: list[str]
    topics: list[str]


class ParagraphHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_paragraph = False
        self.paragraph_parts: list[str] = []
        self.paragraphs: list[str] = []
        self.capture_depth = 0
        self.in_script = False
        self.in_style = False
        self.images: list[str] = []
        self.in_figure = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = dict(attrs)
        if tag == "script":
            self.in_script = True
        if tag == "style":
            self.in_style = True
        if tag == "p" and not self.in_script and not self.in_style:
            self.in_paragraph = True
            self.paragraph_parts = []
            self.capture_depth = 1
        elif self.in_paragraph:
            self.capture_depth += 1

        if tag == "figure":
            self.in_figure = True

        if tag == "img":
            src = attrs_dict.get("src") or attrs_dict.get("data-src") or attrs_dict.get("srcset")
            if src and self.in_figure:
                candidate = src.split(",")[0].strip().split(" ")[0]
                if candidate.startswith("http"):
                    self.images.append(candidate)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.in_script = False
        if tag == "style":
            self.in_style = False
        if tag == "figure":
            self.in_figure = False
        if self.in_paragraph:
            self.capture_depth -= 1
            if tag == "p" and self.capture_depth <= 0:
                text = clean_text("".join(self.paragraph_parts))
                if text:
                    self.paragraphs.append(text)
                self.in_paragraph = False
                self.paragraph_parts = []
                self.capture_depth = 0

    def handle_data(self, data: str) -> None:
        if self.in_paragraph and not self.in_script and not self.in_style:
            self.paragraph_parts.append(data)


def clean_text(value: str) -> str:
    value = unescape(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def shorten_text(value: str, limit: int = 210) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    truncated = value[:limit].rsplit(" ", 1)[0].strip()
    return f"{truncated}..."


def html_to_lines(html: str) -> list[str]:
    sanitized = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    sanitized = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", sanitized)
    sanitized = re.sub(r"(?i)<br\s*/?>", "\n", sanitized)
    sanitized = re.sub(r"(?i)</(p|div|section|article|li|h1|h2|h3|h4|h5|h6|time)>", "\n", sanitized)
    sanitized = re.sub(r"<[^>]+>", " ", sanitized)
    sanitized = unescape(sanitized)
    sanitized = sanitized.replace("\r", "\n")
    lines = [clean_text(line) for line in sanitized.split("\n")]
    return [line for line in lines if line]


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30, context=build_ssl_context()) as response:
        return response.read().decode("utf-8", errors="ignore")


def build_ssl_context() -> ssl.SSLContext:
    cert_file = os.environ.get("SSL_CERT_FILE")
    if cert_file:
        return ssl.create_default_context(cafile=cert_file)

    if os.environ.get("TECHCRUNCH_ALLOW_INSECURE_SSL") == "1":
        return ssl._create_unverified_context()

    return ssl.create_default_context()


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"sent_urls": [], "last_digest_date": None}

    with STATE_PATH.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as file:
        json.dump(state, file, ensure_ascii=False, indent=2)


def use_publication_window() -> bool:
    return os.environ.get("USE_PUBLICATION_WINDOW", "0") == "1"


def get_digest_hour_msk() -> int:
    return int(os.environ.get("DIGEST_HOUR_MSK", "22"))


def get_publication_window(now_msk: datetime | None = None) -> tuple[datetime, datetime]:
    now_msk = now_msk or datetime.now(MSK)
    digest_hour = get_digest_hour_msk()
    window_end = now_msk.replace(hour=digest_hour, minute=0, second=0, microsecond=0)
    if now_msk < window_end:
        window_end = now_msk
    window_start = window_end - timedelta(days=1)
    return window_start, window_end


def parse_published_at(value: str) -> datetime | None:
    value = clean_text(value)
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MSK)


def is_article_in_window(article: Article, now_msk: datetime | None = None) -> bool:
    published_at = parse_published_at(article.published_at)
    if not published_at:
        return False
    window_start, window_end = get_publication_window(now_msk)
    return window_start <= published_at <= window_end


def get_category_previews() -> list[ArticlePreview]:
    html = fetch_text(CATEGORY_URL)
    pairs = re.findall(
        r'<a[^>]+href="(https://techcrunch\.com/\d{4}/\d{2}/\d{2}/[^"]+/?)"[^>]*>(.*?)</a>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    previews: list[ArticlePreview] = []
    seen: set[str] = set()
    for url, raw_title in pairs:
        if url in seen:
            continue
        title = clean_text(re.sub(r"<[^>]+>", " ", raw_title))
        if not title or len(title) < 8:
            continue
        previews.append(ArticlePreview(title=title, url=url))
        seen.add(url)
    return previews


def extract_json_ld_blocks(html: str) -> list[dict[str, Any]]:
    matches = re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    blocks: list[dict[str, Any]] = []
    for raw_block in matches:
        try:
            data = json.loads(clean_text(raw_block))
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            blocks.extend(item for item in data if isinstance(item, dict))
        elif isinstance(data, dict):
            graph = data.get("@graph")
            if isinstance(graph, list):
                blocks.extend(item for item in graph if isinstance(item, dict))
            else:
                blocks.append(data)
    return blocks


def first_article_schema(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    for block in blocks:
        block_type = block.get("@type")
        if block_type == "NewsArticle":
            return block
        if isinstance(block_type, list) and "NewsArticle" in block_type:
            return block
    return {}


def extract_images_from_html(html: str) -> list[str]:
    article_html = extract_article_body_html(html)
    parser = ParagraphHTMLParser()
    parser.feed(article_html)
    candidates = parser.images
    filtered: list[str] = []
    seen: set[str] = set()
    for image_url in candidates:
        image_url = image_url.replace("&amp;", "&")
        normalized_url = image_url.lower()
        if "techcrunch.com/wp-content/" not in normalized_url and "images.unsplash.com" not in normalized_url:
            continue
        if normalized_url.endswith(".svg") or ".svg?" in normalized_url:
            continue
        if any(marker in normalized_url for marker in BAD_IMAGE_MARKERS):
            continue
        if image_url in seen:
            continue
        seen.add(image_url)
        filtered.append(image_url)
    if filtered:
        return filtered[:10]

    fallback_image = extract_primary_image_from_meta(html)
    if fallback_image:
        return [fallback_image]
    return filtered[:10]


def extract_article_body_html(html: str) -> str:
    start_marker = '<div class="entry-content wp-block-post-content'
    start = html.find(start_marker)
    if start == -1:
        return html

    div_start = html.find("<div", start)
    if div_start == -1:
        return html

    tag_pattern = re.compile(r"</?div\b[^>]*>", re.IGNORECASE)
    depth = 0
    started = False
    end = len(html)

    for match in tag_pattern.finditer(html, div_start):
        token = match.group(0)
        if token.startswith("</"):
            depth -= 1
            if started and depth == 0:
                end = match.end()
                break
        else:
            depth += 1
            started = True

    return html[div_start:end]


def extract_primary_image_from_meta(html: str) -> str:
    for attr_name in ("property", "name"):
        image_url = extract_meta_content(html, attr_name, "og:image")
        if image_url:
            return image_url
    return ""


def extract_meta_content(html: str, attr_name: str, attr_value: str) -> str:
    pattern = rf'<meta[^>]+{attr_name}="{re.escape(attr_value)}"[^>]+content="([^"]+)"'
    match = re.search(pattern, html, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))

    pattern = rf'<meta[^>]+content="([^"]+)"[^>]+{attr_name}="{re.escape(attr_value)}"'
    match = re.search(pattern, html, flags=re.IGNORECASE)
    if match:
        return clean_text(match.group(1))

    return ""


def extract_summary_from_html(html: str) -> str:
    parser = ParagraphHTMLParser()
    parser.feed(html)

    useful_paragraphs: list[str] = []
    stop_markers = (
        "Topics",
        "When you purchase through links in our articles",
        "Loading the player",
        "View Bio",
        "Most Popular",
    )
    for paragraph in parser.paragraphs:
        if any(marker in paragraph for marker in stop_markers):
            continue
        if len(paragraph) < 80:
            continue
        useful_paragraphs.append(paragraph)
        if len(useful_paragraphs) == 2:
            break

    if useful_paragraphs:
        return "\n\n".join(useful_paragraphs)

    text_only = clean_text(re.sub(r"<[^>]+>", " ", html))
    sentences = re.split(r"(?<=[.!?])\s+", text_only)
    return " ".join(sentences[:3]).strip()


def translate_to_russian(text: str) -> str:
    text = clean_text(text)
    if not text:
        return ""
    if re.search(r"[А-Яа-яЁё]", text):
        return text

    url = "https://translate.googleapis.com/translate_a/single"
    params = urllib.parse.urlencode(
        {
            "client": "gtx",
            "sl": "en",
            "tl": "ru",
            "dt": "t",
            "q": text,
        }
    )
    request = urllib.request.Request(f"{url}?{params}", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=30, context=build_ssl_context()) as response:
            payload = json.loads(response.read().decode("utf-8", errors="ignore"))
    except Exception:
        return text

    chunks = payload[0] if isinstance(payload, list) and payload else []
    translated = "".join(part[0] for part in chunks if isinstance(part, list) and part and isinstance(part[0], str))
    return clean_text(translated) or text


def extract_topics_from_html(html: str) -> list[str]:
    lines = html_to_lines(html)
    topic_indexes = [index for index, line in enumerate(lines) if line == "Topics"]
    if not topic_indexes:
        return []

    end = len(lines)
    for index, line in enumerate(lines):
        if line.startswith("When you purchase through links in our articles"):
            end = index
            break

    start = topic_indexes[-1] + 1
    if start >= end:
        return []

    topics: list[str] = []
    for line in lines[start:end]:
        parts = [clean_text(part) for part in line.split(",")]
        for part in parts:
            if part and part not in topics:
                topics.append(part)
        if len(topics) >= 10:
            break
    return topics


def split_digest_messages(articles: list[Article], limit: int = 4000) -> list[str]:
    header = f"Дайджест TechCrunch Apps за {datetime.now(MSK).strftime('%d.%m.%Y')}"
    messages: list[str] = []
    current = header

    for index, article in enumerate(articles, start=1):
        block_lines = [
            f"{index}. {article.title}",
            article.summary,
            f"Полная статья: {article.url}",
        ]
        if article.author:
            block_lines.insert(1, f"Автор: {article.author}")
        block = "\n".join(block_lines)

        candidate = f"{current}\n\n{block}"
        if len(candidate) <= limit:
            current = candidate
            continue

        messages.append(current)
        current = f"{header}\n\n{block}"

    messages.append(current)
    return messages


def build_article_caption(article: Article, index: int) -> str:
    title = shorten_text(translate_to_russian(article.title), 120)
    summary = shorten_text(translate_to_russian(article.summary), 220)
    lines = [
        f"{index}. {title}",
        f"Коротко: {summary}",
        f"Статья: {article.url}",
    ]
    return "\n".join(lines)[:1024]


def get_article_details(preview: ArticlePreview) -> Article:
    html = fetch_text(preview.url)
    schema = first_article_schema(extract_json_ld_blocks(html))

    title = clean_text(schema.get("headline", preview.title)) or preview.title
    published_at = clean_text(schema.get("datePublished", ""))
    author = ""
    author_data = schema.get("author")
    if isinstance(author_data, dict):
        author = clean_text(str(author_data.get("name", "")))
    elif isinstance(author_data, list) and author_data:
        first_author = author_data[0]
        if isinstance(first_author, dict):
            author = clean_text(str(first_author.get("name", "")))
    if not author:
        author = extract_meta_content(html, "name", "author")

    summary = extract_summary_from_html(html)
    images = extract_images_from_html(html)
    topics = extract_topics_from_html(html)

    schema_images = schema.get("image")
    if isinstance(schema_images, list):
        for image_url in schema_images:
            if isinstance(image_url, str) and image_url not in images:
                images.insert(0, image_url)
    elif isinstance(schema_images, str) and schema_images not in images:
        images.insert(0, schema_images)

    deduped_images: list[str] = []
    seen: set[str] = set()
    for image_url in images:
        if image_url in seen:
            continue
        deduped_images.append(image_url)
        seen.add(image_url)

    return Article(
        title=title,
        url=preview.url,
        author=author,
        published_at=published_at,
        summary=summary,
        images=deduped_images[:10],
        topics=topics,
    )


def telegram_request(method: str, payload: dict[str, Any]) -> dict[str, Any]:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(request, timeout=30, context=build_ssl_context()) as response:
        raw = response.read().decode("utf-8", errors="ignore")
    data = json.loads(raw)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {raw}")
    return data


def send_digest(articles: list[Article]) -> None:
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    for index, article in enumerate(articles, start=1):
        caption = build_article_caption(article, index)
        if not article.images:
            telegram_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": caption,
                    "disable_web_page_preview": False,
                },
            )
            time.sleep(1)
            continue

        media = []
        for index, image_url in enumerate(article.images[:5]):
            item: dict[str, Any] = {"type": "photo", "media": image_url}
            if index == 0:
                item["caption"] = caption
            media.append(item)
        try:
            telegram_request(
                "sendMediaGroup",
                {
                    "chat_id": chat_id,
                    "media": media,
                },
            )
        except Exception:
            telegram_request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": caption,
                    "disable_web_page_preview": False,
                },
            )
        time.sleep(1)


def collect_new_articles(state: dict[str, Any]) -> list[Article]:
    sent_urls = set(state.get("sent_urls", []))
    articles: list[Article] = []
    max_articles = int(os.environ.get("MAX_ARTICLES_PER_RUN", "3"))
    publication_window_mode = use_publication_window()
    now_msk = datetime.now(MSK)
    for preview in get_category_previews():
        if not publication_window_mode and preview.url in sent_urls:
            continue
        try:
            article = get_article_details(preview)
        except urllib.error.URLError as error:
            print(f"Не удалось получить статью: {preview.url} ({error})", file=sys.stderr)
            continue
        except Exception as error:  # noqa: BLE001
            print(f"Ошибка обработки статьи {preview.url}: {error}", file=sys.stderr)
            continue
        if "Apps" not in article.topics:
            continue
        if publication_window_mode and not is_article_in_window(article, now_msk):
            continue
        articles.append(article)
        if len(articles) >= max_articles:
            break
    return articles


def run_digest() -> int:
    missing = [key for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(key)]
    if missing:
        print(f"Не заданы переменные окружения: {', '.join(missing)}", file=sys.stderr)
        return 1

    state = load_state()
    articles = collect_new_articles(state)
    if not articles:
        print("Новых статей нет.")
        return 0

    send_digest(articles)
    if not use_publication_window():
        sent_urls = set(state.get("sent_urls", []))
        for article in articles:
            sent_urls.add(article.url)
        state["sent_urls"] = sorted(sent_urls)
        state["last_digest_date"] = datetime.now(MSK).date().isoformat()
        save_state(state)
    print(f"Отправлено статей: {len(articles)}")
    return 0


def should_run_now(state: dict[str, Any], now_msk: datetime) -> bool:
    last_digest_date = state.get("last_digest_date")
    if now_msk.hour != 19 or now_msk.minute != 0:
        return False
    return last_digest_date != now_msk.date().isoformat()


def scheduler_loop() -> int:
    print("Планировщик запущен. Ожидаю 19:00 по Москве.")
    while True:
        now_msk = datetime.now(MSK)
        state = load_state()
        if should_run_now(state, now_msk):
            print(f"Запускаю дайджест: {now_msk.isoformat()}")
            run_digest()
        time.sleep(30)


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--run-once":
        return run_digest()
    return scheduler_loop()


if __name__ == "__main__":
    raise SystemExit(main())
