# arXiv Keyword Alerter

arXivの新着論文をキーワード検索し、Geminiで日本語解説を生成してメール通知するPythonスクリプトです。

- キーワードはカンマ区切りで複数指定（OR検索）
- カテゴリは複数指定可（`all`指定で全カテゴリ）
- 前日（JST）に投稿された論文を対象に通知

---

## これは何？

毎日のarXivチェックを自動化するためのツールです。  
`arxiv_alerter.py` は次の流れで動作します。

1. arXiv APIで論文を検索
2. 前日（JST）投稿分を抽出
3. 論文本文（取得失敗時はabstract）を取得
4. Geminiで日本語解説を生成
5. タイトル・著者・リンク・解説・abstractをメール送信

---

## 設定の種類

本ツールは**公開設定**（YAML）と**機密設定**（環境変数）を分離しています。

| 種類 | ファイル/方法 | 内容 |
|------|--------------|------|
| **公開設定** | `settings.public.yaml` | 検索設定、Geminiモデル、メールテンプレート等 |
| **プロンプト** | `prompts/summary_ja.txt` | Gemini解説生成用プロンプト |
| **機密設定** | 環境変数 / `config.env` | APIキー、パスワード、メールアドレス |

> ⚠️ `config.env` はGitにコミットしないでください（`.gitignore` で除外済み）。

---

## ローカル環境でのセットアップ

### 1. 前提

- Python 3.9以上
- SMTP送信可能なメールアカウント
- （推奨）Gemini APIキー

### 2. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 3. 設定ファイルの準備

```bash
# 機密設定用ファイルを作成
cp config.env.example config.env
```

`config.env` を編集して機密情報を設定：

```bash
# [必須] メール認証（settings.public.yaml の runtime.test_mode=false なら必須）
SMTP_USER="your_email@gmail.com"
SMTP_PASSWORD="your_app_password"
MAIL_FROM="your_email@gmail.com"
MAIL_TO="your_email@gmail.com"

# [任意] Gemini API（未設定でも動作、解説はスキップ）
GEMINI_API_KEY="your_gemini_api_key"
```

### 4. 公開設定のカスタマイズ（任意）

`settings.public.yaml` で検索キーワードや各種設定を変更できます：

```yaml
runtime:
  test_mode: false
  gemini_prompt_path: "prompts/summary_ja.txt"

arxiv:
  search_keywords: "machine learning, neural network"
  search_category: "cs.AI, cs.LG"
```

---

## 使い方

### 通常実行

```bash
python arxiv_alerter.py
```

### テストモード

メール送信せず、生成されるメール本文を確認できます。  
**SMTP認証情報は不要**です。

`settings.public.yaml` の `runtime.test_mode: true` を設定して実行します。

### 実行結果

- 新着論文がある場合: 指定メールアドレスに通知
- 新着論文がない場合: 「処理対象の論文はありませんでした。」と表示

---

## GitHub Actions での自動実行

### 1. Repository Secrets の設定

リポジトリの **Settings > Secrets and variables > Actions > Secrets** で以下を設定：

| Secret名 | 内容 |
|----------|------|
| `GEMINI_API_KEY` | Gemini APIキー |
| `SMTP_USER` | SMTP認証ユーザー |
| `SMTP_PASSWORD` | SMTPパスワード（Gmailはアプリパスワード） |
| `MAIL_FROM` | 送信元メールアドレス |
| `MAIL_TO` | 送信先メールアドレス |

### 2. ワークフローの有効化

Fork後、**Actions** タブでワークフローを有効化してください。

- デフォルトでは毎日 JST 10:00 に自動実行
- 手動実行: Actions > arXiv Keyword Alerter > Run workflow

---

## 環境変数一覧

| 変数名 | 必須 | 説明 |
|--------|:----:|------|
| `SMTP_USER` | △ | SMTP認証ユーザー（`runtime.test_mode=false`時必須） |
| `SMTP_PASSWORD` | △ | SMTPパスワード（`runtime.test_mode=false`時必須） |
| `MAIL_FROM` | △ | 送信元アドレス（`runtime.test_mode=false`時必須） |
| `MAIL_TO` | △ | 送信先アドレス（`runtime.test_mode=false`時必須） |
| `GEMINI_API_KEY` | - | Gemini APIキー（未設定で解説スキップ） |
 
非機密設定（検索条件・SMTPサーバー/ポート・メール件名・テストモード・Geminiレート制限・プロンプトパス）は `settings.public.yaml` で管理します。

非機密の既定値は `settings.public.yaml` のみを参照します（コード側の固定既定値は持ちません）。

---

## 注意事項

- APIキー・パスワード・メールアドレスなどの**機密情報は公開しない**でください
- 公開リポジトリでは `config.env` をコミットしないでください
- Gemini無料枠はレート制限があります（`settings.public.yaml` の `gemini.max_requests_per_minute` で調整可）
- 論文ごとに待機が入るため、件数が多いと実行時間が長くなります

---

## 参考

- https://qiita.com/Ken3_K/items/ff18cae48aed928a7309
