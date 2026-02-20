#!/usr/bin/env python3
import argparse
import os
import re
import sys
from pathlib import Path

import requests

API_URL = "https://ja.wikipedia.org/w/api.php"
DEFAULT_USER_AGENT = "getwiki-bot/1.0 (https://example.com; contact: you@example.com)"
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
REF_MARK_RE = re.compile(r"\[\d+\]")
MULTI_WS_RE = re.compile(r"\s+")


def sanitize_title(title: str) -> str:
    title = INVALID_FILENAME_CHARS.sub("_", title).strip()
    return title or "untitled"


def clean_text(text: str) -> str:
    text = REF_MARK_RE.sub("", text)
    text = text.replace("*", "")
    text = MULTI_WS_RE.sub(" ", text)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[。！？!?])", text)
    return [c.strip() for c in chunks if c.strip()]


def is_valid_text(text: str) -> bool:
    length = len(text)
    return 30 <= length


def fetch_random_intro(session: requests.Session) -> tuple[str, str] | None:
    params = {
        "action": "query",
        "format": "json",
        "generator": "random",
        "grnnamespace": 0,
        "grnlimit": 1,
        "prop": "extracts",
        "explaintext": 1,
        "exintro": 1,
    }
    res = session.get(API_URL, params=params, timeout=15)
    res.raise_for_status()

    data = res.json()
    pages = data.get("query", {}).get("pages", {})
    if not pages:
        return None

    page = next(iter(pages.values()))
    title = page.get("title", "")
    extract = page.get("extract", "")
    if not title or not extract:
        return None

    cleaned = clean_text(extract)

    # Intro が長い場合は文単位で候補化し、長さ条件に合うものを優先する
    candidates = split_sentences(cleaned)
    for sentence in candidates:
        if is_valid_text(sentence):
            return title, sentence

    # 文単位で合わない場合は導入文全体を試す
    fallback = cleaned.strip()
    if is_valid_text(fallback):
        return title, fallback

    return None


def save_wiki_text(out_dir: Path, title: str, text: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    file_name = sanitize_title(title) + ".txt"
    target = out_dir / file_name

    # 取得できた時点で即時保存し、ディスクへ同期する
    with target.open("w", encoding="utf-8") as f:
        f.write(text + "\n")
        f.flush()
        os.fsync(f.fileno())

    return target


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wikipediaからランダム記事を取得し、条件に合う概要文をwikiフォルダへ保存する"
    )
    parser.add_argument("--count", type=int, default=1, help="保存する件数")
    parser.add_argument("--max-tries", type=int, default=50, help="1件あたりの最大試行回数")
    parser.add_argument("--out", default="wiki", help="保存先フォルダ")
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="Wikipedia APIへ送るUser-Agent",
    )
    args = parser.parse_args()

    if args.count <= 0:
        print("--count は 1 以上を指定してください", file=sys.stderr)
        return 1

    if args.max_tries <= 0:
        print("--max-tries は 1 以上を指定してください", file=sys.stderr)
        return 1

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": args.user_agent,
            "Accept": "application/json",
        }
    )
    saved = 0
    out_dir = Path(args.out)

    for _ in range(args.count):
        found = None
        for _ in range(args.max_tries):
            try:
                found = fetch_random_intro(session)
            except requests.RequestException as e:
                print(f"API呼び出しに失敗: {e}", file=sys.stderr)
                continue

            if found:
                break

        if not found:
            print("条件に合う文章が見つかりませんでした", file=sys.stderr)
            continue

        title, text = found
        save_wiki_text(out_dir, title, text)
        saved += 1
        print(f"進捗: {saved}/{args.count} 完了")

    if saved == 0:
        return 1

    print(f"完了: {saved} 件保存")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
