# mail-transporter

iCloud メールの受信トレイに届いたメールを **確実に** Gmail へ転送するシステムです。

iCloud 標準の転送機能（SMTP 経由）はヘッダの内容によって Gmail 側で受信拒否され、
メールが消失することがあります。本システムは iCloud から IMAP でメールをそのまま読み取り、
**Gmail API の `messages.insert` で直接投入**するため、SMTP の受信判定を一切通りません。

## アーキテクチャ

```
 iCloud Mail (IMAP)                     Google Cloud (すべて無料枠内)
 ┌──────────────┐   IMAP IDLE / poll   ┌───────────────────────────┐
 │    INBOX     │◄─────────────────────│ GCE e2-micro  "watcher"   │◄── MIG 自動修復
 │  (= キュー)  │                      │  mailtransporter.watcher  │    (/healthz 監視)
 └──────┬───────┘                      └────────────┬──────────────┘
        │                                           │ POST /sync (OIDC)
        │  IMAP fetch / move                        ▼
        │                              ┌───────────────────────────┐
        └─────────────────────────────►│ Cloud Run  "forwarder"    │
                                       │  mailtransporter.server   │
                                       └────────────┬──────────────┘
                                                    │ Gmail API insert
                                                    ▼
                                             ┌────────────┐
                                             │   Gmail    │
                                             └────────────┘
```

外部のデータストアは使いません。「Gmail に投入済み」という進捗は iCloud 側のメール自身に
IMAP キーワード（`$GmailInserted`）として記録します。

| コンポーネント | 役割 | 無料枠 |
|---|---|---|
| **watcher** (GCE e2-micro, サイズ 1 の MIG) | iCloud の INBOX を IMAP IDLE（非対応時はポーリング）で監視し、新着を検出したら Cloud Run を呼ぶ。状態は持たない。MIG の自動修復が `/healthz` を監視し、停止・削除・ハング時に VM を作り直す。 | e2-micro 1台 / 月（us-west1, us-central1, us-east1）。MIG とヘルスチェックは無料 |
| **forwarder** (Cloud Run) | INBOX の全メールを IMAP で取得 → Gmail API で insert → iCloud 側をゴミ箱へ移動。 | 200万リクエスト / 月 |
| **Secret Manager** | iCloud のアプリ用パスワード、Gmail OAuth のリフレッシュトークン。 | 6 アクティブバージョン、1万アクセス / 月 |
| **Artifact Registry / Cloud Build** | コンテナイメージ（forwarder と watcher は同一イメージ）。 | 0.5 GB / 120 ビルド分 / 日 |

### 「確実に転送する」ための設計

1. **iCloud の INBOX 自体をキューとして使う。**
   メールは「Gmail に insert 済み」かつ「iCloud でゴミ箱へ移動済み」になるまで INBOX に残ります。
   Gmail API や Cloud Run が落ちていてもメールは INBOX に残るだけで、復旧後に自動的に再処理されます。
   メール本文を別のストレージにコピーする必要はありません。
2. **IMAP キーワードで冪等性を担保する。** 1 通ごとの処理は次の 3 ステップです。

   ```
   INBOX のメール ─Gmail に insert─► キーワード $GmailInserted を付与 ─MOVE─► ゴミ箱
                      │ 一時的エラー(5xx/429/ネットワーク) → INBOX に残して次回再試行
                      │ 恒久的エラー(400 等)               → Forward-Failed フォルダへ退避
   ```
   * キーワード付与後・ゴミ箱移動前にクラッシュしても、次回はキーワードを見て **insert をスキップ** します。
   * insert 後・キーワード付与前のクラッシュ（ごく短い窓）には、Message-ID による `rfc822msgid:` 検索で
     Gmail 側の存在確認を行って備えます。
   * 接続時に `PERMANENTFLAGS` に `\*` が含まれるか確認し、カスタムキーワードを保存できないサーバでは
     転送を行いません（フェイルクローズ）。キーワード付与の応答も検証します。
   * 多重実行は Cloud Run の `max-instances=1, concurrency=1` とプロセス内ロックで直列化しています。
     IMAP には compare-and-set が無いため、デプロイ直後にリビジョンが一瞬重なった場合の二重投入は
     Message-ID 検索だけが防波堤になります。
3. **消失させない。** Gmail に恒久的に拒否されたメール（サイズ超過など）は削除せず、
   iCloud 上の `Forward-Failed` フォルダへ退避します。ゴミ箱に入るのは Gmail への投入が確認できたメールだけです。
   一時的エラーは回数制限なく再試行します（メールは INBOX に残るだけなので安全です）。
4. **watcher は自動修復する。** watcher は監視ループの各ブロッキング操作（IDLE、forwarder 呼び出し、スリープ）の
   直前に「この操作は最大 N 秒かかる」とハートビートを更新し、`/healthz` はその猶予内なら 200 を返します。
   サイズ 1 のマネージドインスタンスグループ（MIG）がこれを監視し、VM の停止・削除・プロセスのハングを検出すると
   VM を作り直します。復旧までの間、メールは INBOX に溜まるだけで失われません。
   Gmail 側の障害に対しては、watcher が INBOX にメールが残っている限り一定間隔（既定 10 分）で再トリガーします。

### Gmail 側の見え方

* `messages.insert` を使うため迷惑メール判定・フィルタは適用されず、そのまま受信トレイに入ります
  （`internalDateSource=dateHeader` で元の日時順に並びます）。
* iCloud 側の既読 (`\Seen`) / フラグ (`\Flagged`) を Gmail の未読 / スターに引き継ぎます。
* 転送されたメールには `iCloud` ラベル（`GMAIL_LABEL` で変更・無効化可）が付きます。

## セットアップ

前提: `gcloud` CLI、Python 3.11+、課金が有効な GCP プロジェクト（無料枠を使うにも課金アカウントの紐付けは必要です）。

### 1. iCloud 側

1. Apple ID で 2 ファクタ認証を有効にし、[appleid.apple.com](https://appleid.apple.com) で **アプリ用パスワード** を発行します。
2. IMAP サーバは `imap.mail.me.com:993`（SSL）。ユーザー名は iCloud のメールアドレスです。

### 2. Gmail API の OAuth クライアント

個人の Gmail アカウントにはサービスアカウントの権限委任が使えないため、OAuth のリフレッシュトークンを一度だけ取得します。

1. GCP コンソール → **API とサービス → OAuth 同意画面**: 外部 (External) で作成し、
   **「本番環境に公開」** にします（テスト状態のままだとリフレッシュトークンが 7 日で失効します。
   自分だけが使うアプリなら Google の審査は不要で、警告画面を「詳細 → 移動」で通過できます）。
2. **認証情報 → OAuth クライアント ID** をアプリの種類 **デスクトップ アプリ** で作成し、JSON をダウンロードします。
3. ローカルでリフレッシュトークンを取得します（ブラウザが開きます。転送先の Gmail アカウントでログインしてください）。

   ```bash
   pip install google-auth-oauthlib
   python scripts/gmail_oauth.py client_secret.json > gmail-oauth.json
   ```

### 3. GCP へのデプロイ

```bash
cp deploy/env.example.sh deploy/env.sh   # PROJECT_ID, ICLOUD_USER などを編集
./deploy/00_enable_apis.sh               # API 有効化
./deploy/01_infra.sh                     # SA / IAM / Artifact Registry
./deploy/02_secrets.sh icloud            # アプリ用パスワードを入力
./deploy/02_secrets.sh gmail gmail-oauth.json
./deploy/03_build.sh                     # Cloud Build でイメージをビルド
./deploy/04_deploy_forwarder.sh          # Cloud Run
./deploy/05_deploy_watcher.sh            # GCE e2-micro の MIG (Container-Optimized OS) + 自動修復
```

コード更新後は `./deploy/release.sh` でビルドと両方のロールアウトをまとめて行えます。

### 4. 動作確認

```bash
source deploy/env.sh
URL=$(gcloud run services describe "$SERVICE_NAME" --region "$REGION" --format 'value(status.url)')
curl -X POST -H "Authorization: Bearer $(gcloud auth print-identity-token)" "$URL/sync"
# => {"status":"ok","listed":3,"forwarded":3,"trashed":3,...}

# ログ
gcloud run services logs read "$SERVICE_NAME" --region "$REGION" --limit 50
gcloud logging read 'resource.type="gce_instance" AND jsonPayload.message:"Triggering"' --limit 20
```

ローカルで一度だけ実行することもできます。

```bash
export ICLOUD_USER=... ICLOUD_PASSWORD=... GMAIL_OAUTH_JSON="$(cat gmail-oauth.json)"
python -m mailtransporter.cli sync
```

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `ICLOUD_USER` | – | iCloud メールアドレス |
| `ICLOUD_PASSWORD` / `ICLOUD_PASSWORD_SECRET` | – | アプリ用パスワード。後者は Secret Manager のリソース名（watcher が使用） |
| `GMAIL_OAUTH_JSON` / `GMAIL_OAUTH_JSON_SECRET` | – | `{"client_id","client_secret","refresh_token"}` |
| `GMAIL_LABEL` | `iCloud` | 付与するラベル。空文字で無効 |
| `FAILED_FOLDER` | `Forward-Failed` | Gmail に恒久拒否されたメールの退避先（iCloud 上） |
| `INSERTED_KEYWORD` | `$GmailInserted` | Gmail 投入済みを示す IMAP キーワード（ASCII のアトム） |
| `TIME_BUDGET_SECONDS` | `480` | 1 回の `/sync` で処理に使う時間。超えた分は次回へ（`remaining` で報告） |
| `FORWARDER_URL` | – | (watcher) Cloud Run の URL |
| `RETRIGGER_INTERVAL` | `600` | (watcher) INBOX にメールが残っている場合の再トリガー間隔（秒） |
| `IDLE_TIMEOUT` | `240` | (watcher) IMAP IDLE の 1 回の待機時間（秒） |
| `POLL_INTERVAL` | `60` | (watcher) IDLE 非対応時のポーリング間隔（秒） |
| `HEALTH_PORT` | `8080` | (watcher) `/healthz` を待ち受けるポート。MIG のヘルスチェックが叩く |
| `HEALTH_STARTUP_GRACE` | `300` | (watcher) 起動直後に healthy とみなす猶予（秒） |

## 運用メモ

* **`Forward-Failed` フォルダ** は定期的に確認してください。ここに入るのは Gmail が受け付けなかったメール
  （50 MB 超など）です。原因を解消して INBOX に戻せば再処理されます。
* **INBOX に残り続けるメール**: 一時的エラーは無期限に再試行するため、特定のメールだけが Gmail に
  5xx を返され続けると INBOX に残り続けます。ログの `transient Gmail failure` で確認できます。
  手動で `Forward-Failed` などへ移せばキューから外れます。
* **カスタムキーワード非対応の場合**: ログに `does not advertise support for custom keywords` と出て
  転送は行われません。iCloud（`imap.mail.me.com`）は Apple Mail 用のキーワードを扱うため対応していますが、
  別の IMAP サーバに向ける場合は確認してください。
* **自動修復の確認**: `gcloud compute instance-groups managed list-instances mail-watcher --zone $ZONE`
  で `HEALTH_STATE` と `ACTION` が見えます。修復が繰り返される場合はコンテナのログ（`gcloud logging read`）で
  起動失敗の原因を確認してください。ヘルスチェックはコンテナ起動後 `HEALTH_INITIAL_DELAY`（既定 5 分）は判定しません。
* **Secret Manager のアクセス回数**: Cloud Run はコールドスタート時、watcher は起動時にシークレットを読みます。
  通常のメール量では月 1 万回の無料枠に遠く及びません。
* **外部 IP**: watcher VM は IMAP へ接続するため外部 IP を持ちます（Cloud NAT は有料）。
  e2-micro の無料枠には使用中の外部 IP 1 つが含まれますが、請求レポートで確認してください。
* **リフレッシュトークンの失効** (`GmailAuthError`): OAuth 同意画面がテスト状態だと 7 日で失効します。
  再取得して `./deploy/02_secrets.sh gmail gmail-oauth.json` で更新し、Cloud Run を再デプロイしてください。

## 開発

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
python -m pytest -q
```

```
mailtransporter/
  config.py        環境変数 → 設定オブジェクト
  secrets.py       Secret Manager からの読み込み
  imap_client.py   iCloud IMAP（fetch / keyword / move / IDLE）
  gmail_client.py  Gmail API（insert / ラベル / Message-ID 検索 / エラー分類）
  forwarder.py     中核ロジック（1 回の同期パス）
  server.py        Cloud Run 用 Flask アプリ（/sync, /healthz）
  watcher.py       GCE 用デーモン（IDLE 監視 → Cloud Run 呼び出し）
  health.py        watcher のハートビートと /healthz（MIG 自動修復用）
  cli.py           ローカル実行用
deploy/            gcloud によるデプロイスクリプト
scripts/           Gmail OAuth リフレッシュトークン取得
```
