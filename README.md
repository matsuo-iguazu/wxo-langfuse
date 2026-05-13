# wxO Langfuse セッション集計ツール

## 目的と機能

IBM watsonx Orchestrate（wxO）のエージェントチャットから呼び出せる Python ツール。  
Langfuse API からトレースデータを取得し、**セッション単位に集計した Excel ファイル**をダウンロードできる形で返す。

### 主な機能

| 機能 | 内容 |
|------|------|
| トレース取得 | Langfuse API をページネーションで全件取得 |
| セッション集計 | sessionId ごとに開始時刻・ターン数・合計レイテンシ等を集計 |
| JST変換 | タイムスタンプを UTC → JST（+9h）に変換 |
| Excel出力 | openpyxl で .xlsx を生成（游ゴシック・ゼブラ縞・オートフィルター付き） |
| ダウンロード | エージェントチャット上にダウンロードボタンとして表示 |

---

## コード解説

### ファイル構成

```
wxo-langfuse/
├── langfuse_sessions_tool.py   # ADK ツール本体（Excel ダウンロード）
├── export_traces.py            # ローカル実行用 CSV 出力スクリプト
├── requirements.txt            # Python 依存パッケージ（ADK ツール用）
├── .env                        # Langfuse 認証情報（ローカル用）
├── .env.sample                 # 認証情報のテンプレート
└── mcp_server/                 # FastMCP サーバー（wxO MCP ツール）
    ├── server.py
    └── requirements.txt
```

### `langfuse_sessions_tool.py`

```
@tool デコレーター
  └─ export_langfuse_sessions() -> bytes   ← ツールのエントリーポイント
       ├─ connections.key_value() で認証情報を取得
       ├─ _fetch_all_traces()              ← Langfuse API からページネーション取得
       ├─ _aggregate_sessions()            ← セッション単位に集計
       └─ _build_xlsx()                   ← openpyxl で Excel を生成し bytes で返す
```

**ポイント：返り値が `bytes` であることが重要。**  
wxO はツールの戻り値が `bytes` の場合、自動的にファイルとして扱い、チャット画面にダウンロードボタンを表示する。

#### openpyxl のインポートについて

`orchestrate tools import` 実行時にツールのモジュールが解析されるため、`openpyxl` をトップレベルで `import` するとエラーになる。  
そのため `_build_xlsx()` 関数の**内部で `import`（遅延ロード）** している。実行時には `requirements.txt` で `openpyxl` がインストールされるため問題ない。

### `requirements.txt`

```
requests==2.33.1
openpyxl==3.1.5
```

---

## ツールと Connection の関係

wxO の Python ツールは**コンテナ上で実行**されるため、ローカルの `.env` や環境変数を参照できない。  
認証情報は **Connection**（wxO の資格情報管理機能）を経由して実行時にコンテナへ注入される。

```
ツールコード
  └─ @tool(expected_credentials=[{"app_id": "m-langfuse", "type": KEY_VALUE}])
       ↓ 実行時
  connections.key_value("m-langfuse").get("LANGFUSE_PUBLIC_KEY")
  connections.key_value("m-langfuse").get("LANGFUSE_SECRET_KEY")
  connections.key_value("m-langfuse").get("LANGFUSE_HOST")
```

Connection 名：**`m-langfuse`**（共有環境のため `m-` 接頭辞付き）  
種別：`key_value` / `team`（全ユーザー共有）/ `draft` 環境

---

## Connection にシークレットを置いていること

`.env` に記載されている Langfuse の API キーは、**セットアップ時に一度だけ** 以下のコマンドで wxO の Connection に転送した。

```bash
orchestrate connections set-credentials -a m-langfuse --env draft \
  -e "LANGFUSE_PUBLIC_KEY=<公開鍵>" \
  -e "LANGFUSE_SECRET_KEY=<秘密鍵>" \
  -e "LANGFUSE_HOST=https://cloud.langfuse.com"
```

転送後は `.env` はツールから参照されない。キーの実体は **wxO 内部のシークレットストア（Connection）に保管**されており、ツールコードにはハードコードされていない。

キーを差し替える場合は、上記コマンドを新しいキーで再実行するだけでよい（ツールの再インポート不要）。

---

## MCP サーバー（`mcp_server/server.py`）

### 目的

wxO 上のエージェントが自然言語で利用状況を問い合わせられるようにする FastMCP サーバー。  
エージェントにアタッチしてチャットから「今日何件アクセスがあった？」「5/7 の質問一覧を見せて」などと問い合わせられる。

### ツール仕様

| ツール | 用途 | 返り値 |
|--------|------|--------|
| `get_session_counts(days, from_date, to_date)` | 日付別セッション件数 | `[{"date": "2026/05/01", "count": 31}, ...]`（昇順） |
| `get_session_list(days, from_date, to_date, limit=20)` | セッション内容一覧 | 各セッションに `first_user_message` / `last_assistant_message` を含む（昇順） |

**共通パラメータ：**
- `from_date` / `to_date`：JST 日付文字列（例: `"2026/05/07"`）。指定すると `days` より優先される
- `days`：今日から遡る日数
- 何も指定しない場合はサービス開始日（2026/05/01）以降の全データを取得

### 実装上のポイント

**observation 一括取得：**  
サービス開始後、root trace の `input`/`output` が `null` になり、ユーザー発言とエージェント応答は `LangGraph` 子スパン（observation）にのみ記録される。  
per-trace で個別 API 呼び出しすると件数分のリクエストが発生してレート制限（429）に当たるため、`fromTimestamp` フィルタで一括取得して `{traceId: [obs]}` に整理する方式を採用している。

**並列ページネーション：**  
ページ 1 を取得して `meta.totalPages` を確認し、残りページを `ThreadPoolExecutor`（max_workers=5）で並列取得する。  
`get_session_counts` は observation fetch をスキップするため高速（約10秒）。`get_session_list` は全件で約17秒。

**429 リトライ：**  
`_fetch_page` と `_fetch_all_pages` の両方に指数バックオフ付きリトライ（最大4回：0.5 / 1 / 2 / 4 秒）を実装。

### wxO 上の構成

- toolkit 名：`m-langfuse-sessions`（app_id: `m-langfuse`）
- エージェント内部名：`M_langfuse__7601Pp`

**デプロイ手順（3ステップ）：**
```
1. remove_toolkit("m-langfuse-sessions")
2. add_toolkit(package_root="mcp_server", command="python server.py", app_id=["m-langfuse"])
3. create_or_update_agent(tools=["m-langfuse-sessions:get_session_counts",
                                  "m-langfuse-sessions:get_session_list"])
```
`add_toolkit` だけではエージェントにアタッチされないため、ステップ3が必須。

---

## 参考にした元スクリプト（`export_traces.py`）

本ツールのロジックは同ディレクトリの `export_traces.py` をベースにしている。

| 処理 | 元スクリプト | 本ツール |
|------|-------------|---------|
| トレース取得 | `fetch_all_traces()` | `_fetch_all_traces()` |
| セッション集計 | `export_sessions_csv()` | `_aggregate_sessions()` |
| 出力形式 | CSV ファイルをローカルに書き出し | bytes（Excel）を wxO に返す |
| 認証 | `.env` から `os.getenv()` | wxO Connection から取得 |
| 文字コード | UTF-8（BOM なし） | — |

`export_traces.py` はローカル実行・動作確認用として引き続き利用可能。

---

## フローを使わずダウンロードボタンが表示された件

当初、wxO フローの「ファイルのダウンロード」ノードを使う構成を想定していた。  
しかし実際にエージェントにツールを直接アタッチしてチャットから実行したところ、**フローなしでもダウンロードボタンが自動表示された**。

**理由：** wxO のエージェントチャットは、ツールの戻り値が `bytes` の場合に自動的にファイルと判断し、ダウンロードボタンをレンダリングする。フローは不要。

```
エージェント（ツール直接アタッチ）
  ↓ チャットで「ログ出して」
  ↓ ツール実行 → bytes 返却
  ↓ wxO がファイルと認識
  → ダウンロードボタン表示  ✅
```

---

## フローを使うとボタン→リンクの2段階になった件

wxO フローの「ユーザー・アクティビティー」→「ファイルのダウンロード」ノードを使った場合、以下の挙動になった。

```
ツールが bytes を返す → ダウンロードボタン表示（ツール由来）
  ↓ ボタンをクリック
フローがファイルダウンロードノードを処理 → リンクを表示（フロー由来）
```

ツール自体がすでにボタンを出しており、さらにフローがリンクを出すという**二重表示**になってしまう。  
このためフローを使わずツール単体で運用することとした。
