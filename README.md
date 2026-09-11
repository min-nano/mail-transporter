# mail-transporter

iCloud メールの受信トレイに届いたメールを **確実に** Gmail へ転送するシステムです。

iCloud 標準の転送機能（SMTP 経由）はヘッダの内容によって Gmail 側で受信拒否され、
メールが消失することがあります。本システムは iCloud から IMAP でメールをそのまま読み取り、
**Gmail API の `messages.insert` で直接投入**するため、SMTP の受信判定を一切通りません。

## アーキテクチャ

```
 iCloud Mail (IMAP)                     Google Cloud (すべて無料枠内)
 ┌──────────────┐   IMAP IDLE / poll   ┌───────────────────────────┐
 │    INBOX     │◄─────────────────────│ GCE e2-micro  "watcher"   │
 │  (= キュー)  │                      │  mailtransporter.watcher  │
 └──────┬───────┘                      └────────────┬──────────────┘
        │                                           │ POST /sync (OIDC)
        │  IMAP fetch / move                        ▼
        │                              ┌───────────────────────────┐
        └─────────────────────────────►│ Cloud Run  "forwarder"    │◄── Cloud Scheduler
                                       │  mailtransporter.server   │    (30分毎の保険)
                                       └──────┬─────────────┬──────┘
                                              │             │
                             Gmail API insert │             │ 冪等性・進捗の記録
                                              ▼             ▼
                                       ┌────────────┐  ┌────────────┐
                                       │   Gmail    │  │ Firestore  │
                                       └────────────┘  └────────────┘
```

| コンポーネント | 役割 | 無料枠 |
|---|---|---|
| **watcher** (GCE e2-micro) | iCloud の INBOX を IMAP IDLE（非対応時はポーリング）で監視し、新着を検出したら Cloud Run を呼ぶ。状態は持たない。 | e2-micro 1台 / 月（us-west1, us-central1, us-east1） |
| **forwarder** (Cloud Run) | INBOX の全メールを IMAP で取得 → Gmail API で insert → iCloud 側をゴミ箱へ移動。 | 200万リクエスト / 月 |
| **Firestore** (Native, `(default)`) | メール単位の処理状態（claim / inserted / done …）を記録し、二重転送を防ぐ。 | 1 GiB, 書込 2万 / 日 |
| **Secret Manager** | iCloud のアプリ用パスワード、Gmail OAuth のリフレッシュトークン。 | 6 アクティブバージョン、1万アクセス / 月 |
| **Cloud Scheduler** | 30 分毎に `/sync` を叩く保険。watcher が落ちていても取りこぼさない。 | 3 ジョブ |
| **Artifact Registry / Cloud Build** | コンテナイメージ（forwarder と watcher は同一イメージ）。 | 0.5 GB / 120 ビルド分 / 日 |

### 「確実に転送する」ための設計

1. **iCloud の INBOX 自体をキューとして使う。**
   メールは「Gmail に insert 済み」かつ「iCloud でゴミ箱へ移動済み」になるまで INBOX に残ります。
   Gmail API や Cloud Run が落ちていてもメールは INBOX に残るだけで、復旧後に自動的に再処理されます。
   メール本文を別のストレージにコピーする必要はありません。
2. **Firestore で冪等性を担保する。** メールは `UIDVALIDITY-UID` をキーに次の状態機械で管理します。

   ```
   pending ─claim─► processing ─insert OK─► inserted ─trash OK─► done
                        │ 一時的エラー(5xx/429/ネットワーク) → pending (attempts+1)
                        │ 恒久的エラー(400 等) / attempts 上限 → rejected → quarantined
   ```
   * insert 後・ゴミ箱移動前にクラッシュしても、次回は `inserted` 状態を見て **insert をスキップ** します。
   * さらに Message-ID による `rfc822msgid:` 検索で Gmail 側の存在確認も行い、Firestore 書込前のクラッシュにも耐えます。
   * 処理中はリース（既定 15 分）を取り、多重実行しても同じメールを同時に扱いません。
     Cloud Run 自体も `max-instances=1, concurrency=1` で直列化しています。
3. **消失させない。** Gmail に恒久的に拒否されたメール（サイズ超過など）は削除せず、
   iCloud 上の `Forward-Failed` フォルダへ退避します。ゴミ箱に入るのは Gmail への投入が確認できたメールだけです。
4. **二重の検出経路。** watcher の IDLE/ポーリングに加え、Cloud Scheduler が定期的に `/sync` を呼びます。
   watcher 側も INBOX にメールが残っている限り一定間隔（既定 10 分）で再トリガーします。

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
./deploy/01_infra.sh                     # SA / IAM / Firestore / Artifact Registry
./deploy/02_secrets.sh icloud            # アプリ用パスワードを入力
./deploy/02_secrets.sh gmail gmail-oauth.json
./deploy/03_build.sh                     # Cloud Build でイメージをビルド
./deploy/04_deploy_forwarder.sh          # Cloud Run
./deploy/05_deploy_watcher.sh            # GCE e2-micro (Container-Optimized OS)
./deploy/06_scheduler.sh                 # Cloud Scheduler の保険ジョブ
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

ローカルで一度だけ実行することもできます（Firestore の代わりにメモリを使う場合は `STORE_BACKEND=memory`）。

```bash
export ICLOUD_USER=... ICLOUD_PASSWORD=... GMAIL_OAUTH_JSON="$(cat gmail-oauth.json)"
STORE_BACKEND=memory python -m mailtransporter.cli sync
```

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `ICLOUD_USER` | – | iCloud メールアドレス |
| `ICLOUD_PASSWORD` / `ICLOUD_PASSWORD_SECRET` | – | アプリ用パスワード。後者は Secret Manager のリソース名（watcher が使用） |
| `GMAIL_OAUTH_JSON` / `GMAIL_OAUTH_JSON_SECRET` | – | `{"client_id","client_secret","refresh_token"}` |
| `GMAIL_LABEL` | `iCloud` | 付与するラベル。空文字で無効 |
| `FAILED_FOLDER` | `Forward-Failed` | Gmail に恒久拒否されたメールの退避先（iCloud 上） |
| `MAX_ATTEMPTS` | `50` | 一時的エラーの再試行上限。超えると退避フォルダへ（0 で無制限） |
| `LEASE_SECONDS` | `600` | 処理中リースの長さ |
| `TIME_BUDGET_SECONDS` | `480` | 1 回の `/sync` で処理に使う時間。超えた分は次回へ（`remaining` で報告） |
| `RETENTION_DAYS` | `30` | Firestore ドキュメントの TTL |
| `STORE_BACKEND` | `firestore` | `memory` にするとローカル検証用（再起動で重複の可能性あり） |
| `FORWARDER_URL` | – | (watcher) Cloud Run の URL |
| `RETRIGGER_INTERVAL` | `600` | (watcher) INBOX にメールが残っている場合の再トリガー間隔（秒） |
| `IDLE_TIMEOUT` | `240` | (watcher) IMAP IDLE の 1 回の待機時間（秒） |
| `POLL_INTERVAL` | `60` | (watcher) IDLE 非対応時のポーリング間隔（秒） |

## 運用メモ

* **`Forward-Failed` フォルダ** は定期的に確認してください。ここに入るのは Gmail が受け付けなかったメール
  （50 MB 超など）と、再試行上限に達したメールです。原因を解消して INBOX に戻せば再処理されます。
* **Firestore の中身**: コレクション `forwarded_messages`、ドキュメント ID `UIDVALIDITY-UID`。
  `status`, `attempts`, `last_error`, `gmail_id` で状況が分かります。30 日で自動削除されます。
* **Secret Manager のアクセス回数**: Cloud Run はコールドスタート時にシークレットを読みます。
  Scheduler の間隔を極端に短くすると月 1 万回の無料枠を超え得るため、既定は 30 分にしています。
* **外部 IP**: watcher VM は IMAP へ接続するため外部 IP を持ちます（Cloud NAT は有料）。
  e2-micro の無料枠には使用中の外部 IP 1 つが含まれますが、請求レポートで確認してください。
* **UIDVALIDITY の変化**: iCloud 側でメールボックスが再構築されると UID が振り直されますが、
  Message-ID による Gmail 側の存在確認で二重投入を防ぎます。
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
  imap_client.py   iCloud IMAP（fetch / move / IDLE）
  gmail_client.py  Gmail API（insert / ラベル / Message-ID 検索 / エラー分類）
  store.py         処理状態ストア（Firestore / インメモリ）
  forwarder.py     中核ロジック（1 回の同期パス）
  server.py        Cloud Run 用 Flask アプリ（/sync, /healthz）
  watcher.py       GCE 用デーモン（IDLE 監視 → Cloud Run 呼び出し）
  cli.py           ローカル実行用
deploy/            gcloud によるデプロイスクリプト
scripts/           Gmail OAuth リフレッシュトークン取得
```
