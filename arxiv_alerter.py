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
import requests
import xml.etree.ElementTree as ET
import google.generativeai as genai
from dotenv import load_dotenv
from bs4 import BeautifulSoup
# --- 0. SETUP ---
# .envファイルから環境変数を読み込む (ローカルテスト用)
load_dotenv(dotenv_path='config.env')

# --- 1. CONFIGURATION LOADING ---
def get_env_var(var_name):
    """
    環境変数を取得する。見つからない場合はエラーメッセージを表示して終了する。
    """
    value = os.environ.get(var_name)
    if value is None:
        print(f"エラー: 必須の環境変数 '{var_name}' が設定されていません。")
        exit(1)
    return value


# arXiv用
SEARCH_KEYWORDS = get_env_var("SEARCH_KEYWORDS")
SEARCH_CATEGORY = get_env_var("SEARCH_CATEGORY")

# Gemini API用
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# メール送信用
SMTP_SERVER = get_env_var("SMTP_SERVER")
SMTP_PORT = int(get_env_var("SMTP_PORT"))
SMTP_USER = get_env_var("SMTP_USER")
SMTP_PASSWORD = get_env_var("SMTP_PASSWORD") 
MAIL_FROM = get_env_var("MAIL_FROM")
MAIL_TO = get_env_var("MAIL_TO")
MAIL_SUBJECT = get_env_var("MAIL_SUBJECT")


# --- 2. GEMINI API SETUP ---
if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        print("Gemini APIキーが正常に設定されました。")
    except Exception as e:
        print(f"Gemini APIキーの設定中にエラー: {e}")
        GEMINI_API_KEY = None
else:
    print("警告: 環境変数 'GEMINI_API_KEY' が設定されていません。")


def generate_summary_with_gemini(paper_info, full_text):
    """Geminiを使用して論文の概要を生成する。"""
    if not GEMINI_API_KEY:
        return "（Geminiによる解説はスキップされました：APIキーが未設定です）"

    model = genai.GenerativeModel('gemini-2.5-flash-lite-preview-06-17')
    # model = genai.GenerativeModel('gemini-2.0-flash-lite')

    prompt = f"""以下のarXiv論文について、内容を専門外の人が読んでも理解できるように、重要なポイントを箇条書きで分かりやすく解説してください。

# 論文情報
- **タイトル:** {paper_info['title']}
- **著者:** {", ".join(paper_info['authors'])}

# 論文本文（またはアブストラクト）
{full_text[:100000]}

# 解説のポイント
1. この研究が解決しようとしている問題点は何か？
2. どのような新しい手法やアプローチを用いたか？
3. この研究の最も重要な発見や結論は何か？
4. この発見が将来どのような影響を与える可能性があるか？

以上の点を踏まえて、日本語で解説を生成してください。
"""
    try:
        response = model.generate_content(prompt)
        time.sleep(1)
        return response.text
    except Exception as e:
        print(f"Gemini APIでの解説生成中にエラー: {e}")
        return f"（Geminiによる解説生成中にエラーが発生しました: {e}）"

# --- 3. ARXIV SEARCH & CONTENT FETCHING ---


def fetch_paper_full_text(html_url):
    """論文のHTMLページから本文テキストを抽出する。"""
    try:
        print(f"  > HTML版の本文を取得中: {html_url}")
        response = requests.get(html_url, timeout=30)
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
        print(f"  > HTMLの解析中にエラー: {e}")
        return None


def search_arxiv():
    """arXiv APIでキーワードに合致する過去24時間の論文を検索する。"""
    print(f"キーワード '{SEARCH_KEYWORDS}' で論文を検索中...")

    # タイトルとアブストラクトから検索するので、各キーワードを (ti:"..." OR abs:"...") の形式にする
    keywords = [
        f'(ti:"{k.strip()}" OR abs:"{k.strip()}")' for k in SEARCH_KEYWORDS.split(',')]
    # 各キーワード検索を "OR" で連結し、全体をカッコで囲む
    keyword_query = f"({' OR '.join(keywords)})"

    query = keyword_query

    if SEARCH_CATEGORY and SEARCH_CATEGORY.lower() != 'all':
        categories = [f'cat:{c.strip()}' for c in SEARCH_CATEGORY.split(',')]
        category_query = f"({' OR '.join(categories)})"
        query += f' AND {category_query}'

    params = {
        'search_query': query,
        'sortBy': 'submittedDate',
        'sortOrder': 'descending',
        'max_results': 25
    }

    try:
        response = requests.get(
            'http://export.arxiv.org/api/query?', params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"arXiv APIへのリクエスト中にエラー: {e}")
        return []

    root = ET.fromstring(response.content)
    ns = {'atom': 'http://www.w3.org/2005/Atom'}

    found_papers = []
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)

    for entry in root.findall('atom:entry', ns):
        published_str = entry.find('atom:published', ns).text
        published_dt = datetime.strptime(
            published_str, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)

        if published_dt > yesterday:
            arxiv_id = entry.find('atom:id', ns).text.split('/abs/')[-1]
            paper_info = {
                'id': arxiv_id,
                'title': entry.find('atom:title', ns).text.strip(),
                'summary': entry.find('atom:summary', ns).text.strip(),
                'authors': [author.find('atom:name', ns).text for author in entry.findall('atom:author', ns)],
                'link': entry.find('atom:id', ns).text,
                'html_link': f"https://arxiv.org/html/{arxiv_id}",
                'published': published_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
            }
            found_papers.append(paper_info)

    print(f"{len(found_papers)}件の新しい論文が見つかりました。")
    return found_papers

# --- 4. EMAIL NOTIFICATION ---


def send_email(subject, body):
    """メールを送信する。"""
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

# --- 5. MAIN EXECUTION ---


def main():
    """メイン処理"""
    papers = search_arxiv()
    if not papers:
        print("処理対象の論文はありませんでした。")
        return

    full_email_body = f"キーワード「{SEARCH_KEYWORDS}」に関する新しい論文が {len(papers)}件 見つかりました。\n\n"

    for i, paper in enumerate(papers, 1):
        if i != 0:
            time.sleep(60)
        print(f"--- 論文 {i}/{len(papers)} の処理を開始: {paper['title']} ---")

        full_text = fetch_paper_full_text(paper['html_link'])

        if not full_text:
            print("  > HTML版の取得に失敗したため、アブストラクトを要約します。")
            full_text = paper['summary']

        summary_ja = generate_summary_with_gemini(paper, full_text)

        full_email_body += f"""
==================================================
論文 {i}: {paper['title']}
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
    send_email(MAIL_SUBJECT, full_email_body)


if __name__ == "__main__":
    # beautifulsoup4とlxmlのインストールを促す
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("エラー: 必要なライブラリがインストールされていません。")
        print("pip install beautifulsoup4 lxml python-dotenv")
        exit(1)
    main()
