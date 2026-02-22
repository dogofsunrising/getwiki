#!/usr/bin/env python3
import argparse
import csv
import html
import os
import re
import sys
from pathlib import Path

import requests

API_URL = "https://ja.wikipedia.org/w/api.php"
DEFAULT_USER_AGENT = "getwiki-bot/1.0 (https://example.com; contact: you@example.com)"
INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')
REF_MARK_RE = re.compile(r"\[\d+\]")
EDIT_MARK_RE = re.compile(r"\[\s*編集\s*\]")
MULTI_WS_RE = re.compile(r"\s+")
HTML_TAG_RE = re.compile(r"<[^>]+>")


def sanitize_title(title: str) -> str:
    title = INVALID_FILENAME_CHARS.sub("_", title).strip()
    return title or "untitled"


def clean_text(text: str) -> str:
    text = REF_MARK_RE.sub("", text)
    text = EDIT_MARK_RE.sub("", text)
    # "*" は通常文でも出る可能性があるので、行頭の箇条書きだけ消す（安全寄り）
    text = re.sub(r"(?m)^\s*\*\s*", "", text)
    text = MULTI_WS_RE.sub(" ", text)
    return text.strip()


def strip_html_text(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</(p|li|h\d|div)>", "\n", text)
    text = HTML_TAG_RE.sub(" ", text)
    text = html.unescape(text)
    return clean_text(text)


def split_sentences(text: str) -> list[str]:
    chunks = re.split(r"(?<=[。！？!?])", text)
    return [c.strip() for c in chunks if c.strip()]


def is_valid_text(text: str) -> bool:
    return len(text) >= 30


def choose_valid_sentence(text: str) -> str | None:
    for sentence in split_sentences(text):
        if is_valid_text(sentence):
            return sentence

    fallback = text.strip()
    if is_valid_text(fallback):
        return fallback
    return None


def fetch_random_page_title(session: requests.Session) -> str | None:
    params = {
        "action": "query",
        "format": "json",
        "generator": "random",
        "grnnamespace": 0,
        "grnlimit": 1,
    }
    res = session.get(API_URL, params=params, timeout=15)
    res.raise_for_status()

    pages = res.json().get("query", {}).get("pages", {})
    if not pages:
        return None

    page = next(iter(pages.values()))
    title = str(page.get("title", "")).strip()
    return title or None


def fetch_lead_extract_all(session: requests.Session, title: str) -> str:
    """記事冒頭（見出しが始まる前まで = lead）をプレーンテキストで丸ごと取得"""
    params = {
        "action": "query",
        "format": "json",
        "prop": "extracts",
        "titles": title,
        "explaintext": 1,
        "exintro": 1,
        "exsectionformat": "plain",
        "redirects": 1,
    }
    res = session.get(API_URL, params=params, timeout=15)
    res.raise_for_status()

    pages = res.json().get("query", {}).get("pages", {})
    if not pages:
        return ""
    page = next(iter(pages.values()))
    return clean_text(page.get("extract", ""))


def fetch_overview_section_text(session: requests.Session, title: str) -> str:
    """見出しが「概要」のセクションがある場合、そのセクション本文を取得（HTML→テキスト）"""
    sections_params = {
        "action": "parse",
        "format": "json",
        "page": title,
        "prop": "sections",
        "redirects": 1,
    }
    res = session.get(API_URL, params=sections_params, timeout=15)
    res.raise_for_status()
    sections = res.json().get("parse", {}).get("sections", [])

    overview_index = None
    for section in sections:
        line = str(section.get("line", "")).strip()
        if line == "概要":
            overview_index = section.get("index")
            break

    if overview_index is None:
        return ""

    text_params = {
        "action": "parse",
        "format": "json",
        "page": title,
        "prop": "text",
        "section": overview_index,
        "redirects": 1,
    }
    res = session.get(API_URL, params=text_params, timeout=15)
    res.raise_for_status()
    html_text = res.json().get("parse", {}).get("text", {}).get("*", "")
    return strip_html_text(html_text)


def fetch_random_summary(session: requests.Session) -> tuple[str, str] | None:
    """
    1) 「概要」セクションがあって十分長ければそれを使う
    2) なければ lead（exintro）全文を使う
    3) それも短ければ、lead から条件を満たす文を1文拾う
    """
    title = fetch_random_page_title(session)
    if not title:
        return None

    overview_text = fetch_overview_section_text(session, title)
    if overview_text and is_valid_text(overview_text):
        return title, overview_text

    lead_text = fetch_lead_extract_all(session, title)
    if lead_text and is_valid_text(lead_text):
        return title, lead_text

    picked = choose_valid_sentence(lead_text) if lead_text else None
    if picked:
        return title, picked

    return None


def ensure_parent_dir(path: Path) -> None:
    parent = path.parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def append_to_csv(csv_path: Path, title: str, text: str) -> None:
    """
    CSVへ追記保存。
    - 1行 = 1記事
    - カラム: title, text
    """
    ensure_parent_dir(csv_path)

    # Excel互換を少し意識して utf-8-sig（BOM付き）を採用
    # いらなければ encoding="utf-8" に変更OK
    file_exists = csv_path.exists()
    need_header = True
    if file_exists:
        try:
            if csv_path.stat().st_size > 0:
                need_header = False
        except OSError:
            pass

    with csv_path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        if need_header:
            writer.writerow(["title", "text"])
        writer.writerow([title, text])

        # 取得できた時点で即時保存し、ディスクへ同期する
        f.flush()
        os.fsync(f.fileno())


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Wikipediaからランダム記事を取得し、概要文をCSVへ追記保存する"
    )
    parser.add_argument("--count", type=int, default=1, help="保存する件数")
    parser.add_argument("--max-tries", type=int, default=50, help="1件あたりの最大試行回数")
    parser.add_argument(
        "--out",
        default="wiki.csv",
        help="保存先CSVファイルパス（例: wiki.csv / out/wiki.csv）",
    )
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
    out_csv = Path(args.out)

    for _ in range(args.count):
        found: tuple[str, str] | None = None

        for _ in range(args.max_tries):
            try:
                found = fetch_random_summary(session)
            except requests.RequestException as e:
                print(f"API呼び出しに失敗: {e}", file=sys.stderr)
                continue

            if found:
                break

        if not found:
            print("条件に合う文章が見つかりませんでした", file=sys.stderr)
            continue

        title, text = found
        append_to_csv(out_csv, title, text)

        saved += 1
        print(f"進捗: {saved}/{args.count} 完了")

    if saved == 0:
        return 1

    print(f"完了: {saved} 件保存 -> {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
