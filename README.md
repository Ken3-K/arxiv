# arXiv Keyword Alerter

arXivの新着論文をキーワード検索し、Geminiで日本語解説を生成してメール通知するPythonスクリプトです。

- キーワードはカンマ区切りで複数指定（OR検索）
- カテゴリは複数指定可（`all`指定で全カテゴリ）
- 直近24時間に投稿された論文を対象に通知

---

## これは何？

毎日のarXivチェックを自動化するためのツールです。  
`arxiv_alerter.py` は次の流れで動作します。

1. arXiv APIで論文を検索
2. 24時間以内の投稿を抽出
3. 論文本文（取得失敗時はabstract）を取得
4. Geminiで日本語解説を生成
5. タイトル・著者・リンク・解説・abstractをメール送信

---

## 設定方法

### 1. 前提

- Python 3.x
- SMTP送信可能なメールアカウント
- （推奨）Gemini APIキー

### 2. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 3. `config.env` の設定

まずテンプレートをコピーして設定ファイルを作成します。

```bash
cp config.env.example config.env
```

作成した `config.env` に必要な設定を記述します。

#### 必須

- `SEARCH_KEYWORDS`
- `SEARCH_CATEGORY`
- `SMTP_SERVER`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `MAIL_FROM`
- `MAIL_TO`
- `MAIL_SUBJECT`

#### Gemini APIキーについて

- `GEMINI_API_KEY` は**スクリプト実行自体には必須ではありません**。
- ただし未設定の場合、Geminiによる解説は生成されず、メール内に「スキップされた」旨の文言が入ります。
- 解説機能を使いたい場合は、`GEMINI_API_KEY` を設定してください。

---

## 使い方

```bash
python arxiv_alerter.py
```

### 実行結果

- 新着論文がある場合: 指定メールアドレスに通知されます。
- 新着論文がない場合: 「処理対象の論文はありませんでした。」と表示して終了します。

---

## 自動実行（任意）

GitHub Actionsで定期実行できます。ワークフロー定義は以下です。

- `.github/workflows/arxiv_alerter.yml`

詳細な設定手順（Secrets設定・運用方針など）は参考記事を参照してください。

- https://qiita.com/Ken3_K/items/ff18cae48aed928a7309

---

## 注意事項

- APIキー・SMTPパスワード・メールアドレスなどの機密情報は公開しないでください。
- 公開リポジトリでは、実値を含む設定ファイルのコミットを避けてください。
- 論文ごとに待機（`sleep`）が入るため、件数が多いと実行時間が長くなります。
