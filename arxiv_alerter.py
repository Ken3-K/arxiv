# -*- coding: utf-8 -*-
"""
arXiv Keyword Alerter

This script searches for new arXiv papers matching specific keywords (with OR logic)
across multiple categories, generates a summary using the Gemini API, and sends an email notification.
It's designed to be run automatically.
"""
import os
import smtplib
import ssl
from email.mime.text import MIMEText
from email.header import Header
from datetime import datetime, timedelta, timezone
import time
from typing import List, Optional, TypedDict
import requests
import xml.etree.ElementTree as ET
from google import genai
from dotenv import load_dotenv
from bs4 import BeautifulSoup

REQUEST_TIMEOUT = 30
ARXIV_MAX_RESULTS = 25
GEMINI_MODEL_NAME = 'gemini-3-flash-preview'
GEMINI_INPUT_MAX_CHARS = 100000
PER_PAPER_DELAY_SECONDS = 60
LOCAL_TIMEZONE = timezone(timedelta(hours=9), name='JST')


class PaperInfo(TypedDict):
    id: str
    title: str
    summary: str
    authors: List[str]
    link: str
    html_link: str
    published: str

# --- 0. SETUP ---
# .envファイルから環境変数を読み込む (ローカルテスト用)
load_dotenv(dotenv_path='config.env')

# --- 1. CONFIGURATION LOADING ---
def get_env_var(var_name: str) -> str:
    """
    環境変数を取得する。見つからない場合はエラーメッセージを表示して終了する。
    """
    value = os.environ.get(var_name)
    if value is None or not value.strip():
        print(f"エラー: 必須の環境変数 '{var_name}' が設定されていません。")
        exit(1)
    return value.strip()


def parse_csv_env(value: str) -> List[str]:
    """カンマ区切り文字列をトリム済みリストへ変換する。空要素は除外する。"""
    return [item.strip() for item in value.split(',') if item.strip()]


def parse_bool_env(value: str) -> bool:
    """文字列環境変数をboolへ変換する。"""
    return value.strip().lower() in {"1", "true", "yes", "on"}


# arXiv用
SEARCH_KEYWORDS = get_env_var("SEARCH_KEYWORDS")
SEARCH_CATEGORY = get_env_var("SEARCH_CATEGORY")

# Gemini API用
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TEST_MODE = parse_bool_env(os.environ.get("TEST_MODE", "false"))

# メール送信用
SMTP_SERVER = get_env_var("SMTP_SERVER")
SMTP_PORT = int(get_env_var("SMTP_PORT"))
SMTP_USER = get_env_var("SMTP_USER")
SMTP_PASSWORD = get_env_var("SMTP_PASSWORD") 
MAIL_FROM = get_env_var("MAIL_FROM")
MAIL_TO = get_env_var("MAIL_TO")
MAIL_SUBJECT = get_env_var("MAIL_SUBJECT")


# --- 2. GEMINI API SETUP ---
gemini_client: Optional[genai.Client] = None
if GEMINI_API_KEY:
    try:
        gemini_client = genai.Client(api_key=GEMINI_API_KEY)
        print("Gemini APIキーが正常に設定されました。")
    except Exception as e:
        print(f"Gemini APIキーの設定中にエラー: {e}")
        GEMINI_API_KEY = None
else:
    print("警告: 環境変数 'GEMINI_API_KEY' が設定されていません。")


def generate_summary_with_gemini(paper_info: PaperInfo, full_text: str) -> str:
    """Geminiを使用して論文の概要を生成する。"""
    if not GEMINI_API_KEY or gemini_client is None:
        return "（Geminiによる解説はスキップされました：APIキーが未設定です）"

    prompt = f"""以下のarXiv論文について、同分野の研究者が短時間で要点を把握できるように解説してください。

# 論文情報
- **タイトル:** {paper_info['title']}
- **著者:** {", ".join(paper_info['authors'])}

# 論文本文（またはアブストラクト）
{full_text[:GEMINI_INPUT_MAX_CHARS]}

# 出力フォーマット（必ずこの順番・見出しで出力）
## 背景・課題
- ...

## 手法
- ...

## 主結果
- ...

## 新規性（先行研究との差分）
- ...

## 限界・今後の課題
- ...

要件:
- 各セクションは2〜4個の箇条書きで簡潔に書くこと。
- 数式の厳密な導出は省略してよいが、専門用語は適切に用いること。
- 推測ではなく、与えられた本文（またはアブストラクト）に根拠がある内容を優先すること。
- 日本語で出力すること。
"""
    try:
        response = gemini_client.models.generate_content(
            model=GEMINI_MODEL_NAME,
            contents=prompt,
        )
        time.sleep(1)
        return response.text or "（Geminiから空の応答が返されました）"
    except Exception as e:
        print(f"Gemini APIでの解説生成中にエラー: {e}")
        return f"（Geminiによる解説生成中にエラーが発生しました: {e}）"

# --- 3. ARXIV SEARCH & CONTENT FETCHING ---


def fetch_paper_full_text(html_url: str) -> Optional[str]:
    """論文のHTMLページから本文テキストを抽出する。"""
    try:
        print(f"  > HTML版の本文を取得中: {html_url}")
        response = requests.get(html_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'lxml')

        content_div = soup.find('div', class_='ltx_page_content')
        if content_div:
            for tag in content_div.find_all(['header', 'footer', 'nav']):
                tag.decompose()
            return content_div.get_text(separator=' ', strip=True)
        else:
            return soup.body.get_text(separator=' ', strip=True)

    except requests.RequestException as e:
        print(f"  > HTMLの取得に失敗: {e}")
        return None
    except Exception as e:
        print(f"  > HTMLの解析中にエラー（URL: {html_url}）: {e}")
        return None


def build_search_query() -> str:
    """検索キーワードとカテゴリからarXiv検索クエリを組み立てる。"""
    keyword_terms = parse_csv_env(SEARCH_KEYWORDS)
    keywords = [f'(ti:"{keyword}" OR abs:"{keyword}")' for keyword in keyword_terms]
    query = f"({' OR '.join(keywords)})"

    if SEARCH_CATEGORY and SEARCH_CATEGORY.lower() != 'all':
        category_terms = parse_csv_env(SEARCH_CATEGORY)
        categories = [f'cat:{category}' for category in category_terms]
        query += f" AND ({' OR '.join(categories)})"

    return query


def get_required_entry_text(entry: ET.Element, tag: str, ns: dict) -> str:
    """XMLエントリから必須テキストを取得する。"""
    element = entry.find(tag, ns)
    if element is None or element.text is None:
        raise ValueError(f"必須フィールドが欠落しています: {tag}")
    return element.text.strip()


def build_paper_info(entry: ET.Element, ns: dict, published_dt: datetime) -> PaperInfo:
    """XMLエントリからメール送信用の論文情報を生成する。"""
    link = get_required_entry_text(entry, 'atom:id', ns)
    arxiv_id = link.split('/abs/')[-1]

    return {
        'id': arxiv_id,
        'title': get_required_entry_text(entry, 'atom:title', ns),
        'summary': get_required_entry_text(entry, 'atom:summary', ns),
        'authors': [
            author_name.text.strip()
            for author_name in entry.findall('atom:author/atom:name', ns)
            if author_name.text and author_name.text.strip()
        ],
        'link': link,
        'html_link': f"https://arxiv.org/html/{arxiv_id}",
        'published': published_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
    }


def search_arxiv() -> List[PaperInfo]:
    """arXiv APIでキーワードに合致する前日投稿分（JST）の論文を検索する。"""
    print(f"キーワード '{SEARCH_KEYWORDS}' で論文を検索中...")

    query = build_search_query()

    params = {
        'search_query': query,
        'sortBy': 'submittedDate',
        'sortOrder': 'descending',
        'max_results': ARXIV_MAX_RESULTS
    }

    try:
        response = requests.get(
            'http://export.arxiv.org/api/query?', params=params, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"arXiv APIへのリクエスト中にエラー: {e}")
        return []

    root = ET.fromstring(response.content)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}

    found_papers: List[PaperInfo] = []
    target_date = (datetime.now(LOCAL_TIMEZONE) - timedelta(days=1)).date()

    for entry in root.findall('atom:entry', ns):
        try:
            published_str = get_required_entry_text(entry, 'atom:published', ns)
            published_dt = datetime.strptime(
                published_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

            published_local_date = published_dt.astimezone(LOCAL_TIMEZONE).date()
            if published_local_date == target_date:
                found_papers.append(build_paper_info(entry, ns, published_dt))
        except Exception as e:
            print(f"エントリ解析中にスキップ（理由: {e}）")
            continue

    print(f"{len(found_papers)}件の新しい論文が見つかりました。")
    return found_papers

# --- 4. EMAIL NOTIFICATION ---


def send_email(subject: str, body: str) -> None:
    """メールを送信する。"""
    if TEST_MODE:
        print("TEST_MODE=true のため、メール送信をスキップします。")
        print("\n===== メール本文（テスト表示） =====\n")
        print(body)
        print("\n===== ここまで =====\n")
        return

    if not all([SMTP_SERVER, SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO]):
        print("メール設定が不完全なため、送信をスキップします。")
        return

    print(f"{MAIL_TO} 宛にメールを送信中...")
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')
    msg['From'] = MAIL_FROM
    msg['To'] = MAIL_TO

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls(context=context)
            server.login(SMTP_USER, SMTP_PASSWORD)
            server.send_message(msg)
        print("メールが正常に送信されました。")
    except Exception as e:
        print(f"メール送信中にエラーが発生しました: {e}")


def build_paper_section(index: int, paper: PaperInfo, summary_ja: str) -> str:
    """1本分の論文情報をメール本文のセクション文字列に変換する。"""
    return f"""
==================================================
論文 {index}: {paper['title']}
==================================================

著者: {", ".join(paper['authors'])}
投稿日: {paper['published']}
リンク: {paper['link']}

--- Geminiによる解説 ---
{summary_ja}
--------------------------

--- Original Abstract ---
{paper['summary']}
--------------------------

"""


def build_keyword_counts_section(papers: List[PaperInfo]) -> str:
    """キーワードごとの対象件数をメール表示用文字列で返す。"""
    keywords = parse_csv_env(SEARCH_KEYWORDS)
    if not keywords:
        return ""

    searchable_texts = [f"{paper['title']}\n{paper['summary']}".lower() for paper in papers]
    lines = ["【キーワード別対象件数】"]
    for keyword in keywords:
        normalized_keyword = keyword.lower()
        hit_count = sum(1 for text in searchable_texts if normalized_keyword in text)
        lines.append(f"- {keyword}: {hit_count}件")

    return "\n".join(lines) + "\n\n"


def build_email_body(papers: List[PaperInfo]) -> str:
    """検索結果からメール本文を構築する。"""
    full_email_body = (
        f"キーワード「{SEARCH_KEYWORDS}」に関する新しい論文が {len(papers)}件 見つかりました。\n\n"
    )

    full_email_body += build_keyword_counts_section(papers)

    full_email_body += "【対象論文タイトル一覧】\n"
    for i, paper in enumerate(papers, 1):
        full_email_body += f"{i}. {paper['title']}\n"
    full_email_body += "\n"

    for i, paper in enumerate(papers, 1):
        if i > 1:
            time.sleep(PER_PAPER_DELAY_SECONDS)
        print(f"--- 論文 {i}/{len(papers)} の処理を開始: {paper['title']} ---")

        full_text = fetch_paper_full_text(paper['html_link'])

        if not full_text:
            print("  > HTML版の取得に失敗したため、アブストラクトを要約します。")
            full_text = paper['summary']

        summary_ja = generate_summary_with_gemini(paper, full_text)
        full_email_body += build_paper_section(i, paper, summary_ja)

    return full_email_body

# --- 5. MAIN EXECUTION ---


def main() -> None:
    """メイン処理"""
    papers = search_arxiv()
    if not papers:
        print("処理対象の論文はありませんでした。")
        return

    full_email_body = build_email_body(papers)
    send_email(MAIL_SUBJECT, full_email_body)


if __name__ == "__main__":
    main()
