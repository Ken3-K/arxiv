# -*- coding: utf-8 -*-
"""
arXiv Keyword Alerter

arXivの新着論文をキーワード検索し、Geminiで日本語解説を生成してメール通知するスクリプト。
- 前日（JST）投稿の論文を対象
- キーワードはOR検索
- 設定は環境変数（機密）とYAMLファイル（公開）で分離
"""
from __future__ import annotations

import os
import smtplib
import ssl
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import Header
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, List, Optional, TypedDict

import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai

# --- 型定義 ---


class PaperInfo(TypedDict):
    id: str
    title: str
    summary: str
    authors: List[str]
    link: str
    html_link: str
    published: str


# --- 設定データクラス ---


@dataclass
class ArxivConfig:
    search_keywords: str
    search_category: str
    max_results: int
    request_timeout_seconds: int


@dataclass
class GeminiConfig:
    model_name: str
    input_max_chars: int
    max_requests_per_minute: int
    max_retries: int
    retry_base_delay_seconds: int
    api_key: Optional[str]


@dataclass
class ProcessingConfig:
    per_paper_delay_seconds: int


@dataclass
class TimezoneConfig:
    utc_offset_hours: int
    name: str

    @property
    def tz(self) -> timezone:
        return timezone(timedelta(hours=self.utc_offset_hours), name=self.name)


@dataclass
class MailConfig:
    smtp_server: str
    smtp_port: int
    smtp_user: Optional[str]
    smtp_password: Optional[str]
    mail_from: Optional[str]
    mail_to: Optional[str]
    subject: str


@dataclass
class MailTemplateConfig:
    header: str
    keyword_counts_title: str
    paper_list_title: str
    paper_separator: str
    gemini_section_header: str
    abstract_section_header: str
    section_footer: str
    gemini_skip_message: str
    gemini_error_message: str
    gemini_empty_message: str


@dataclass
class AppConfig:
    arxiv: ArxivConfig
    gemini: GeminiConfig
    processing: ProcessingConfig
    timezone: TimezoneConfig
    mail: MailConfig
    mail_template: MailTemplateConfig
    prompt_template: str
    test_mode: bool = False


# --- 設定読み込み ---


def parse_csv(value: str) -> List[str]:
    """カンマ区切り文字列をトリム済みリストへ変換する。空要素は除外する。"""
    return [item.strip() for item in value.split(",") if item.strip()]


def get_env(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    """環境変数を取得する。required=Trueで未設定の場合はエラー終了。"""
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    if required and not value:
        print(f"エラー: 必須の環境変数 '{name}' が設定されていません。")
        sys.exit(1)
    return value if value else None


def ensure_int(value: Any, field_name: str, minimum: Optional[int] = None) -> int:
    """設定値を整数として検証する。"""
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        print(f"エラー: 設定値 '{field_name}' は整数で指定してください。現在値: {value}")
        sys.exit(1)

    if minimum is not None and parsed < minimum:
        print(f"エラー: 設定値 '{field_name}' は {minimum} 以上で指定してください。現在値: {parsed}")
        sys.exit(1)
    return parsed


def ensure_bool(value: Any, field_name: str) -> bool:
    """設定値をboolとして検証する。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    print(f"エラー: 設定値 '{field_name}' は true/false で指定してください。現在値: {value}")
    sys.exit(1)


def render_template_text(template: str, values: dict[str, Any]) -> str:
    """テンプレート内の {key} を値で置換する。"""
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{key}}}", str(value))
    return rendered


def load_yaml_config(path: Path) -> dict[str, Any]:
    """YAMLファイルを読み込む。"""
    if not path.exists():
        print(f"エラー: 設定ファイルが見つかりません: {path}")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        print(f"エラー: YAML設定ファイルの解析に失敗しました: {path}\n{e}")
        sys.exit(1)


def load_prompt_template(path: Path) -> str:
    """プロンプトテンプレートファイルを読み込む。"""
    if not path.exists():
        print(f"エラー: プロンプトテンプレートが見つかりません: {path}")
        sys.exit(1)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"エラー: プロンプトファイルの読み込みに失敗しました: {path}\n{e}")
        sys.exit(1)


def get_required_yaml_section(yaml_cfg: dict[str, Any], section_name: str) -> dict[str, Any]:
    """必須のYAMLセクションを取得する。"""
    section = yaml_cfg.get(section_name)
    if not isinstance(section, dict):
        print(f"エラー: settings.public.yaml の '{section_name}' セクションが未設定です。")
        sys.exit(1)
    return section


def get_required_yaml_value(section: dict[str, Any], key: str, field_name: str) -> Any:
    """必須のYAML値を取得する。空文字は未設定扱いにする。"""
    value = section.get(key)
    if value is None:
        print(f"エラー: settings.public.yaml の '{field_name}' が未設定です。")
        sys.exit(1)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            print(f"エラー: settings.public.yaml の '{field_name}' が空文字です。")
            sys.exit(1)
        return normalized
    return value


def load_config() -> AppConfig:
    """YAMLファイル（公開設定）と環境変数（機密設定）から設定を読み込む。"""
    script_dir = Path(__file__).parent
    # .envファイルの読み込み（ローカル用、存在しなくてもOK）
    load_dotenv(dotenv_path=script_dir / "config.env")

    # 公開設定ファイルの読み込み（パス固定）
    settings_path = script_dir / "settings.public.yaml"

    # YAML設定の読み込み
    yaml_cfg = load_yaml_config(settings_path)
    runtime_yaml = get_required_yaml_section(yaml_cfg, "runtime")
    arxiv_yaml = get_required_yaml_section(yaml_cfg, "arxiv")
    gemini_yaml = get_required_yaml_section(yaml_cfg, "gemini")
    processing_yaml = get_required_yaml_section(yaml_cfg, "processing")
    tz_yaml = get_required_yaml_section(yaml_cfg, "timezone")
    mail_yaml = get_required_yaml_section(yaml_cfg, "mail")
    mail_template_yaml = get_required_yaml_section(yaml_cfg, "mail_template")

    # 実行設定
    test_mode = ensure_bool(
        get_required_yaml_value(runtime_yaml, "test_mode", "runtime.test_mode"),
        "runtime.test_mode",
    )
    prompt_path_str = str(
        get_required_yaml_value(runtime_yaml, "gemini_prompt_path", "runtime.gemini_prompt_path")
    )
    prompt_path = Path(prompt_path_str)
    if not prompt_path.is_absolute():
        prompt_path = script_dir / prompt_path

    # プロンプトの読み込み
    prompt_template = load_prompt_template(prompt_path)

    # arXiv設定
    search_keywords = str(get_required_yaml_value(arxiv_yaml, "search_keywords", "arxiv.search_keywords"))
    search_category = str(get_required_yaml_value(arxiv_yaml, "search_category", "arxiv.search_category"))
    if not parse_csv(search_keywords):
        print("エラー: settings.public.yaml の 'arxiv.search_keywords' は空要素のみです。")
        sys.exit(1)
    if search_category.lower() != "all" and not parse_csv(search_category):
        print("エラー: settings.public.yaml の 'arxiv.search_category' は空要素のみです。")
        sys.exit(1)

    arxiv_config = ArxivConfig(
        search_keywords=search_keywords,
        search_category=search_category,
        max_results=ensure_int(
            get_required_yaml_value(arxiv_yaml, "max_results", "arxiv.max_results"),
            "arxiv.max_results",
            minimum=1,
        ),
        request_timeout_seconds=ensure_int(
            get_required_yaml_value(arxiv_yaml, "request_timeout_seconds", "arxiv.request_timeout_seconds"),
            "arxiv.request_timeout_seconds",
            minimum=1,
        ),
    )

    # Gemini設定
    gemini_config = GeminiConfig(
        model_name=str(get_required_yaml_value(gemini_yaml, "model_name", "gemini.model_name")),
        input_max_chars=ensure_int(
            get_required_yaml_value(gemini_yaml, "input_max_chars", "gemini.input_max_chars"),
            "gemini.input_max_chars",
            minimum=1,
        ),
        max_requests_per_minute=ensure_int(
            get_required_yaml_value(gemini_yaml, "max_requests_per_minute", "gemini.max_requests_per_minute"),
            "gemini.max_requests_per_minute",
            minimum=0,
        ),
        max_retries=ensure_int(
            get_required_yaml_value(gemini_yaml, "max_retries", "gemini.max_retries"),
            "gemini.max_retries",
            minimum=0,
        ),
        retry_base_delay_seconds=ensure_int(
            get_required_yaml_value(gemini_yaml, "retry_base_delay_seconds", "gemini.retry_base_delay_seconds"),
            "gemini.retry_base_delay_seconds",
            minimum=1,
        ),
        api_key=get_env("GEMINI_API_KEY"),
    )

    # Processing設定
    processing_config = ProcessingConfig(
        per_paper_delay_seconds=ensure_int(
            get_required_yaml_value(
                processing_yaml,
                "per_paper_delay_seconds",
                "processing.per_paper_delay_seconds",
            ),
            "processing.per_paper_delay_seconds",
            minimum=0,
        ),
    )

    # Timezone設定
    timezone_config = TimezoneConfig(
        utc_offset_hours=ensure_int(
            get_required_yaml_value(tz_yaml, "utc_offset_hours", "timezone.utc_offset_hours"),
            "timezone.utc_offset_hours",
        ),
        name=str(get_required_yaml_value(tz_yaml, "name", "timezone.name")),
    )

    # Mail設定（送信先/認証情報は環境変数。非機密はYAML）
    mail_config = MailConfig(
        smtp_server=str(get_required_yaml_value(mail_yaml, "smtp_server_default", "mail.smtp_server_default")),
        smtp_port=ensure_int(
            get_required_yaml_value(mail_yaml, "smtp_port_default", "mail.smtp_port_default"),
            "mail.smtp_port_default",
            minimum=1,
        ),
        smtp_user=get_env("SMTP_USER"),
        smtp_password=get_env("SMTP_PASSWORD"),
        mail_from=get_env("MAIL_FROM"),
        mail_to=get_env("MAIL_TO"),
        subject=str(get_required_yaml_value(mail_yaml, "subject_default", "mail.subject_default")),
    )

    # runtime.test_mode=false の場合のみSMTP認証情報を必須チェック
    if not test_mode:
        missing = []
        if not mail_config.smtp_user:
            missing.append("SMTP_USER")
        if not mail_config.smtp_password:
            missing.append("SMTP_PASSWORD")
        if not mail_config.mail_from:
            missing.append("MAIL_FROM")
        if not mail_config.mail_to:
            missing.append("MAIL_TO")
        if missing:
            print(
                "エラー: 以下の環境変数が設定されていません（runtime.test_mode=false時は必須）: "
                f"{', '.join(missing)}"
            )
            sys.exit(1)

    # MailTemplate設定
    mail_template_config = MailTemplateConfig(
        header=str(get_required_yaml_value(mail_template_yaml, "header", "mail_template.header")),
        keyword_counts_title=str(
            get_required_yaml_value(
                mail_template_yaml,
                "keyword_counts_title",
                "mail_template.keyword_counts_title",
            )
        ),
        paper_list_title=str(
            get_required_yaml_value(mail_template_yaml, "paper_list_title", "mail_template.paper_list_title")
        ),
        paper_separator=str(
            get_required_yaml_value(mail_template_yaml, "paper_separator", "mail_template.paper_separator")
        ),
        gemini_section_header=str(
            get_required_yaml_value(
                mail_template_yaml,
                "gemini_section_header",
                "mail_template.gemini_section_header",
            )
        ),
        abstract_section_header=str(
            get_required_yaml_value(
                mail_template_yaml,
                "abstract_section_header",
                "mail_template.abstract_section_header",
            )
        ),
        section_footer=str(
            get_required_yaml_value(mail_template_yaml, "section_footer", "mail_template.section_footer")
        ),
        gemini_skip_message=str(
            get_required_yaml_value(mail_template_yaml, "gemini_skip_message", "mail_template.gemini_skip_message")
        ),
        gemini_error_message=str(
            get_required_yaml_value(
                mail_template_yaml,
                "gemini_error_message",
                "mail_template.gemini_error_message",
            )
        ),
        gemini_empty_message=str(
            get_required_yaml_value(mail_template_yaml, "gemini_empty_message", "mail_template.gemini_empty_message")
        ),
    )

    return AppConfig(
        arxiv=arxiv_config,
        gemini=gemini_config,
        processing=processing_config,
        timezone=timezone_config,
        mail=mail_config,
        mail_template=mail_template_config,
        prompt_template=prompt_template,
        test_mode=test_mode,
    )


# --- HTTP セッション ---


class HttpClient:
    """共有HTTPセッションを持つHTTPクライアント。"""

    def __init__(self, timeout: int = 30):
        self._session = requests.Session()
        self._timeout = timeout

    def get(self, url: str, **kwargs) -> requests.Response:
        """GETリクエストを実行する。"""
        kwargs.setdefault("timeout", self._timeout)
        response = self._session.get(url, **kwargs)
        response.raise_for_status()
        return response

    def close(self):
        self._session.close()


# --- Geminiレート制限ガード ---


@dataclass
class RateLimiter:
    """Gemini APIのレート制限を管理する。"""

    max_requests_per_minute: int
    max_retries: int = 3
    retry_base_delay_seconds: int = 5
    _last_request_time: float = field(default=0.0, init=False, repr=False)

    @property
    def min_interval(self) -> float:
        """リクエスト間の最小間隔（秒）。"""
        if self.max_requests_per_minute <= 0:
            return 0.0
        return 60.0 / self.max_requests_per_minute

    def wait_if_needed(self):
        """必要に応じてレート制限のための待機を行う。"""
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last_request_time
        if elapsed < self.min_interval:
            wait_time = self.min_interval - elapsed
            print(f"  > レート制限: {wait_time:.1f}秒待機中...")
            time.sleep(wait_time)

    def record_request(self):
        """リクエスト実行を記録する。"""
        self._last_request_time = time.time()

    def get_retry_delay(self, attempt: int) -> float:
        """指数バックオフでリトライ待機時間を計算する。"""
        return self.retry_base_delay_seconds * (2 ** attempt)


# --- Gemini API ---


class GeminiClient:
    """Gemini APIクライアント（レート制限対応）。"""

    def __init__(self, config: GeminiConfig, template: MailTemplateConfig, prompt_template: str):
        self._config = config
        self._template = template
        self._prompt_template = prompt_template
        self._client: Optional[genai.Client] = None
        self._rate_limiter = RateLimiter(
            max_requests_per_minute=config.max_requests_per_minute,
            max_retries=config.max_retries,
            retry_base_delay_seconds=config.retry_base_delay_seconds,
        )

        if config.api_key:
            try:
                self._client = genai.Client(api_key=config.api_key)
                print("Gemini APIキーが正常に設定されました。")
            except Exception as e:
                print(f"Gemini APIキーの設定中にエラー: {e}")
        else:
            print("警告: 環境変数 'GEMINI_API_KEY' が設定されていません。")

    def is_available(self) -> bool:
        return self._client is not None

    def generate_summary(self, paper: PaperInfo, full_text: str) -> str:
        """論文の日本語解説を生成する。"""
        if not self.is_available():
            return self._template.gemini_skip_message

        # プロンプト生成
        body = full_text[: self._config.input_max_chars]
        prompt = render_template_text(
            self._prompt_template,
            {
                "title": paper["title"],
                "authors": ", ".join(paper["authors"]),
                "body": body,
            },
        )

        # リトライ付きでAPI呼び出し
        last_error: Optional[Exception] = None
        for attempt in range(self._rate_limiter.max_retries + 1):
            try:
                self._rate_limiter.wait_if_needed()
                response = self._client.models.generate_content(
                    model=self._config.model_name,
                    contents=prompt,
                )
                self._rate_limiter.record_request()
                return response.text or self._template.gemini_empty_message

            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                # レート制限エラー (429) の検出
                is_rate_limit = "429" in error_str or "rate" in error_str or "quota" in error_str

                if is_rate_limit and attempt < self._rate_limiter.max_retries:
                    delay = self._rate_limiter.get_retry_delay(attempt)
                    print(f"  > Geminiレート制限エラー: {delay:.0f}秒後にリトライ ({attempt + 1}/{self._rate_limiter.max_retries})")
                    time.sleep(delay)
                    continue
                else:
                    break

        print(f"Gemini APIでの解説生成中にエラー: {last_error}")
        return render_template_text(self._template.gemini_error_message, {"error": last_error})


# --- arXiv検索 ---


def get_required_entry_text(entry: ET.Element, tag: str, ns: dict) -> str:
    """XMLエントリから必須テキストを取得する。"""
    element = entry.find(tag, ns)
    if element is None or element.text is None:
        raise ValueError(f"必須フィールドが欠落しています: {tag}")
    return element.text.strip()


def build_paper_info(entry: ET.Element, ns: dict, published_dt: datetime) -> PaperInfo:
    """XMLエントリからメール送信用の論文情報を生成する。"""
    link = get_required_entry_text(entry, "atom:id", ns)
    arxiv_id = link.split("/abs/")[-1]

    return {
        "id": arxiv_id,
        "title": get_required_entry_text(entry, "atom:title", ns),
        "summary": get_required_entry_text(entry, "atom:summary", ns),
        "authors": [
            author_name.text.strip()
            for author_name in entry.findall("atom:author/atom:name", ns)
            if author_name.text and author_name.text.strip()
        ],
        "link": link,
        "html_link": f"https://arxiv.org/html/{arxiv_id}",
        "published": published_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
    }


def build_search_query(keywords: str, category: str) -> str:
    """検索キーワードとカテゴリからarXiv検索クエリを組み立てる。"""
    keyword_terms = parse_csv(keywords)
    keywords_query = [f'(ti:"{kw}" OR abs:"{kw}")' for kw in keyword_terms]
    query = f"({' OR '.join(keywords_query)})"

    if category and category.lower() != "all":
        category_terms = parse_csv(category)
        categories = [f"cat:{cat}" for cat in category_terms]
        query += f" AND ({' OR '.join(categories)})"

    return query


def search_arxiv(config: ArxivConfig, tz_config: TimezoneConfig, http: HttpClient) -> List[PaperInfo]:
    """arXiv APIでキーワードに合致する前日投稿分（ローカル時間）の論文を検索する。"""
    print(f"キーワード '{config.search_keywords}' で論文を検索中...")

    query = build_search_query(config.search_keywords, config.search_category)
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "max_results": config.max_results,
    }

    try:
        response = http.get("https://export.arxiv.org/api/query", params=params)
    except requests.RequestException as e:
        print(f"arXiv APIへのリクエスト中にエラー: {e}")
        return []

    root = ET.fromstring(response.content)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    found_papers: List[PaperInfo] = []
    local_tz = tz_config.tz
    target_date = (datetime.now(local_tz) - timedelta(days=1)).date()

    for entry in root.findall("atom:entry", ns):
        try:
            published_str = get_required_entry_text(entry, "atom:published", ns)
            published_dt = datetime.strptime(published_str, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            )
            published_local_date = published_dt.astimezone(local_tz).date()
            if published_local_date == target_date:
                found_papers.append(build_paper_info(entry, ns, published_dt))
        except Exception as e:
            print(f"エントリ解析中にスキップ（理由: {e}）")
            continue

    print(f"{len(found_papers)}件の新しい論文が見つかりました。")
    return found_papers


def fetch_paper_full_text(html_url: str, http: HttpClient) -> Optional[str]:
    """論文のHTMLページから本文テキストを抽出する。"""
    try:
        print(f"  > HTML版の本文を取得中: {html_url}")
        response = http.get(html_url)
        soup = BeautifulSoup(response.content, "lxml")

        content_div = soup.find("div", class_="ltx_page_content")
        if content_div:
            for tag in content_div.find_all(["header", "footer", "nav"]):
                tag.decompose()
            return content_div.get_text(separator=" ", strip=True)
        else:
            return soup.body.get_text(separator=" ", strip=True) if soup.body else None

    except requests.RequestException as e:
        print(f"  > HTMLの取得に失敗: {e}")
        return None
    except Exception as e:
        print(f"  > HTMLの解析中にエラー（URL: {html_url}）: {e}")
        return None


# --- メール作成・送信 ---


def build_keyword_counts_section(papers: List[PaperInfo], keywords: str, template: MailTemplateConfig) -> str:
    """キーワードごとの対象件数をメール表示用文字列で返す。"""
    keyword_list = parse_csv(keywords)
    if not keyword_list:
        return ""

    searchable_texts = [f"{p['title']}\n{p['summary']}".lower() for p in papers]
    lines = [template.keyword_counts_title]
    for kw in keyword_list:
        hit_count = sum(1 for text in searchable_texts if kw.lower() in text)
        lines.append(f"- {kw}: {hit_count}件")

    return "\n".join(lines) + "\n\n"


def build_paper_section(
    index: int, paper: PaperInfo, summary_ja: str, template: MailTemplateConfig
) -> str:
    """1本分の論文情報をメール本文のセクション文字列に変換する。"""
    return f"""
{template.paper_separator}
論文 {index}: {paper['title']}
{template.paper_separator}

著者: {", ".join(paper['authors'])}
投稿日: {paper['published']}
リンク: {paper['link']}

{template.gemini_section_header}
{summary_ja}
{template.section_footer}

{template.abstract_section_header}
{paper['summary']}
{template.section_footer}

リンク: {paper['link']}

"""


def build_email_body(
    papers: List[PaperInfo],
    config: AppConfig,
    http: HttpClient,
    gemini: GeminiClient,
) -> str:
    """検索結果からメール本文を構築する。"""
    template = config.mail_template

    # ヘッダー
    body = render_template_text(
        template.header,
        {"keywords": config.arxiv.search_keywords, "count": len(papers)},
    )

    # キーワード別件数
    body += build_keyword_counts_section(papers, config.arxiv.search_keywords, template)

    # タイトル一覧
    body += f"{template.paper_list_title}\n"
    for i, paper in enumerate(papers, 1):
        body += f"{i}. {paper['title']}\n"
    body += "\n"

    # 各論文の詳細
    for i, paper in enumerate(papers, 1):
        if i > 1:
            time.sleep(config.processing.per_paper_delay_seconds)

        print(f"--- 論文 {i}/{len(papers)} の処理を開始: {paper['title']} ---")

        full_text = fetch_paper_full_text(paper["html_link"], http)
        if not full_text:
            print("  > HTML版の取得に失敗したため、アブストラクトを要約します。")
            full_text = paper["summary"]

        summary_ja = gemini.generate_summary(paper, full_text)
        body += build_paper_section(i, paper, summary_ja, template)

    return body


def send_email(subject: str, body: str, config: AppConfig) -> None:
    """メールを送信する。"""
    if config.test_mode:
        print("runtime.test_mode=true のため、メール送信をスキップします。")
        print("\n===== メール本文（テスト表示） =====\n")
        print(body)
        print("\n===== ここまで =====\n")
        return

    mail = config.mail
    if not all([mail.smtp_server, mail.smtp_user, mail.smtp_password, mail.mail_from, mail.mail_to]):
        print("エラー: メール設定が不完全です。SMTP_USER/SMTP_PASSWORD/MAIL_FROM/MAIL_TO を確認してください。")
        sys.exit(1)

    print(f"{mail.mail_to} 宛にメールを送信中...")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = mail.mail_from
    msg["To"] = mail.mail_to

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(mail.smtp_server, mail.smtp_port) as server:
            server.starttls(context=context)
            server.login(mail.smtp_user, mail.smtp_password)
            server.send_message(msg)
        print("メールが正常に送信されました。")
    except Exception as e:
        print(f"メール送信中にエラーが発生しました: {e}")


# --- メイン処理 ---


def main() -> None:
    """メイン処理"""
    config = load_config()
    http = HttpClient(timeout=config.arxiv.request_timeout_seconds)
    gemini = GeminiClient(config.gemini, config.mail_template, config.prompt_template)

    try:
        papers = search_arxiv(config.arxiv, config.timezone, http)
        if not papers:
            print("処理対象の論文はありませんでした。")
            return

        body = build_email_body(papers, config, http, gemini)
        send_email(config.mail.subject, body, config)
    finally:
        http.close()


if __name__ == "__main__":
    main()
