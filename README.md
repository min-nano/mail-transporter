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
| **Artifact Registry / Cloud Build** | コンテナイメージ（forwarder と watcher は同一イメージ）。直近 2 世代だけ保持。 | 0.5 GB / 120 ビルド分 / 日 |

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
   * insert 後・キーワード付与前（IMAP 往復 1 回分の窓）にクラッシュすると、次回もう一度 insert されて
     Gmail に **重複** が生じます。消失ではなく重複に倒す設計です。サーバがキーワード付与を拒否した場合は
     `Forward-Unverified` へ退避し、重複が毎回増え続けないようにします。Gmail 検索による重複排除は
     メール読み取りスコープが必要になるうえ、偽装した Message-ID で配送を抑止できる経路になるため行いません。
   * 接続時に `PERMANENTFLAGS` に `\*` が含まれるか確認し、カスタムキーワードを保存できないサーバでは
     転送を行いません（フェイルクローズ）。キーワード付与はサーバの応答（無ければ再取得）で確認します。
   * 多重実行は Cloud Run の `max-instances=1, concurrency=1` とプロセス内ロックで直列化しています。
     デプロイ直後にリビジョンが一瞬重なった場合も、起こり得るのは重複であって消失ではありません。
3. **消失させない。** Gmail に恒久的に拒否されたメール（サイズ超過など）は削除せず、
   iCloud 上の `Forward-Failed` フォルダへ退避します。ゴミ箱に入るのは Gmail への投入が確認できたメールだけです。
   一時的エラーは回数制限なく再試行します（メールは INBOX に残るだけなので安全です）。
   1 回の実行で `REJECTION_THRESHOLD`（既定 3）件以上が拒否され、かつ 1 通も投入できなかった場合は
   「メールではなくアカウントや設定の問題」とみなし、何も退避せずにエラーを返します（INBOX が丸ごと空になる事故を防ぎます）。
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

   要求するスコープは `gmail.insert` と `gmail.labels` だけです。どちらも既存メールの読み取りを含まないため、
   リフレッシュトークンが漏れても Gmail の中身は読めません。スコープを変更した場合はトークンの取り直しが必要です。

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
`deploy/env.sh` を置かずに `PROJECT_ID` と `ICLOUD_USER` を環境変数で渡しても動きます（残りは `deploy/_common.sh` の既定値）。

### 3b. main への push で自動デプロイする（任意）

`.github/workflows/deploy.yml` が main への push ごとにテスト → Cloud Build → Cloud Run と watcher MIG のロールアウトを行います。
認証はサービスアカウントの鍵を保存しない Workload Identity 連携です。

1. GCP 側で連携用のプールとデプロイ専用サービスアカウントを一度だけ作ります（プロジェクトオーナーで実行）。

   ```bash
   GITHUB_REPO=owner/mail-transporter ./deploy/07_github_deployer.sh
   ```

   デプロイ用サービスアカウントに与えるのはロールアウトに必要な権限だけです（Cloud Build の投入、Cloud Run の管理、
   インスタンステンプレートと MIG の管理、イメージの push、実行用サービスアカウントの利用）。
   ヘルスチェックやファイアウォールなど一度きりのインフラは `01_infra.sh` 側にあり、CI からは触りません。
2. スクリプトが表示する値を GitHub リポジトリの **Variables**（Settings → Secrets and variables → Actions → Variables）に登録します。

   | 変数 | 必須 | 内容 |
   |---|---|---|
   | `GCP_WORKLOAD_IDENTITY_PROVIDER` | ○ | `projects/<番号>/locations/global/workloadIdentityPools/github/providers/github` |
   | `GCP_DEPLOYER_SA` | ○ | `mail-deployer@<project>.iam.gserviceaccount.com` |
   | `GCP_PROJECT_ID` | ○ | GCP プロジェクト ID |
   | `ICLOUD_USER` | ○ | iCloud メールアドレス |
   | `GCP_REGION` / `GCP_ZONE` | | 既定 `us-central1` / `us-central1-a` |
   | `GMAIL_LABEL` | | 既定 `iCloud`。`none` でラベルなし |

3. 以後は main への push（PR のマージ）で自動的にデプロイされます。Actions の **Deploy** ワークフローから手動実行もできます。
   同時に 2 つのデプロイが走らないよう直列化され、テストが失敗した場合はデプロイしません。

### 3c. PR を Claude に自動レビューさせる（任意）

`.github/workflows/claude-review.yml` が、この リポジトリ内のブランチから開かれた PR に対して
[Claude Code Action](https://github.com/anthropics/claude-code-action) を走らせ、セキュリティと
コストを重点にレビューします。指摘は可能な限りインラインコメント、行に紐づかないものだけを
レビュー本文にまとめ、最後に **承認 / 非承認** の判定を付けます。

Claude Pro / Max のサブスクリプションをそのまま使うので、API キー（従量課金）は不要です。

1. 手元の Claude Code で長期 OAuth トークンを発行します。

   ```bash
   claude setup-token
   ```

2. 出力されたトークンを GitHub リポジトリの **Secrets**（Settings → Secrets and variables → Actions →
   Secrets）に `CLAUDE_CODE_OAUTH_TOKEN` という名前で登録します。未登録のときはワークフローが
   その旨のエラーで即座に止まります。

このワークフローが満たしている前提:

* トリガは `pull_request` だけで、`pull_request_target` は使いません。fork からの PR には
  シークレットが渡らないうえ、ジョブの `if` で明示的に除外しているので、書き込み権限のない
  第三者が PR 経由でトークンやリポジトリの権限を引き出すことはできません。
* ワークフロー既定の権限は空で、レビュージョブにだけ `contents: read` と `pull-requests: write` を
  与えます。チェックアウトは `persist-credentials: false` です。
* Action には `github_token` としてジョブ既定の `GITHUB_TOKEN` を明示的に渡します。これを省くと
  Action は OIDC トークンを Claude GitHub App のトークンに交換しようとして `id-token: write` を
  要求します。明示的に渡すことでその経路を使わず、権限はこのジョブに与えた 2 つだけに収まり、
  Claude GitHub App のインストールも不要になります。
* Claude に許可するのは読み取りと `gh pr` のコメント／レビュー投稿だけで、`Write` / `Edit` や
  任意の `Bash` は渡しません。PR の本文や差分に書かれた文言は「指示」ではなく「データ」として
  扱うようプロンプトで明示しています。
* 同じ PR への連続 push は `concurrency` で古い実行を打ち切り、ジョブには `timeout-minutes: 20` を
  置いているので、ハングやリトライで実行時間とサブスクリプションの利用枠を浪費しません。
  draft の PR と Dependabot の PR はレビューしません（後者はシークレットを受け取れないため）。

> **注意**: Claude の `--approve` は GitHub 上では通常の承認レビューです。ブランチ保護で必須承認数を
> 設けている場合、Claude の承認だけでマージできてしまわないよう、Code Owners のレビューを必須にするなど
> 人の承認が別途必要な設定にしてください。
>
> なお `GITHUB_TOKEN` による承認は、Settings → Actions → General の
> **Allow GitHub Actions to create and approve pull requests** が無効だと拒否されます（既定は無効）。
> その場合 Claude は `gh pr comment` にフォールバックし、判定はコメント本文の先頭行に出ます。

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
| `GMAIL_LABEL` | `iCloud` | 付与するラベル。空文字で無効（デプロイスクリプトでは `none` を指定） |
| `FAILED_FOLDER` | `Forward-Failed` | Gmail に恒久拒否されたメールの退避先（iCloud 上）。**Gmail には入っていない** |
| `UNVERIFIED_FOLDER` | `Forward-Unverified` | Gmail への投入は成功したがキーワードを記録できなかったメールの退避先。**Gmail には入っている**ので INBOX に戻すと重複する |
| `INSERTED_KEYWORD` | `$GmailInserted` | Gmail 投入済みを示す IMAP キーワード（ASCII のアトム）。このツール専用の未使用の名前にすること。`$Forwarded` や `$Junk` など Apple Mail が使うものは拒否されます |
| `REJECTION_THRESHOLD` | `3` | 1 回の実行でこの件数以上が拒否され、かつ 1 通も投入できなければ退避せずエラーにする（0 で無効）。この状態が続く間は毎回同じメールを Gmail に再送信するため、原因（ラベル ID 不正やサイズ超過の連続など）は早めに解消すること |
| `ALLOWED_INVOKER_SA` | – | (forwarder) `/sync` を呼べるサービスアカウント。Cloud Run の IAM に加えてアプリ側でも ID トークンを検証する。未設定なら検証しない（ローカル用） |
| `EXPECTED_AUDIENCE` | – | (forwarder) ID トークンの `aud` に要求する値（Cloud Run のサービス URL）。デプロイスクリプトが自動設定 |
| `TIME_BUDGET_SECONDS` | `480` | 1 回の `/sync` で処理に使う時間。超えた分は次回へ（`remaining` で報告） |
| `FORWARDER_URL` | – | (watcher) Cloud Run の URL |
| `RETRIGGER_INTERVAL` | `600` | (watcher) INBOX にメールが残っている場合の再トリガー間隔（秒） |
| `IDLE_TIMEOUT` | `240` | (watcher) IMAP IDLE の 1 回の待機時間（秒） |
| `POLL_INTERVAL` | `60` | (watcher) IDLE 非対応時のポーリング間隔（秒） |
| `HEALTH_PORT` | `8080` | (watcher) `/healthz` を待ち受けるポート。MIG のヘルスチェックが叩く |
| `HEALTH_STARTUP_GRACE` | `300` | (watcher) 起動直後に healthy とみなす猶予（秒） |

## 運用メモ

* **`Forward-Failed` フォルダ** は定期的に確認してください。ここに入るのは Gmail が受け付けなかったメール
  （50 MB 超など）で、Gmail には入っていません。原因を解消して INBOX に戻せば再処理されます。
* **`Forward-Unverified` フォルダ** には、Gmail への投入は成功したのに iCloud 側へキーワードを記録できなかった
  メールが入ります。こちらは Gmail に届いているので、INBOX に戻すと重複します。Gmail 側を確認したうえで
  ゴミ箱へ移すか、そのまま残してください。
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
* **外部 IP と SSH**: watcher VM は IMAP へ接続するため外部 IP を持ちます（Cloud NAT は有料）。
  e2-micro の無料枠には使用中の外部 IP 1 つが含まれますが、請求レポートで確認してください。
  VM はメモリ上に iCloud のパスワードを持つため、ポート 22 はインターネットから閉じ、OS Login を強制し、
  プロジェクト共通の SSH 鍵をブロックしています。ログインが必要なときは IAP 経由で接続してください。

  ```bash
  gcloud compute ssh <instance> --zone "$ZONE" --tunnel-through-iap
  ```
* **自動デプロイの信頼範囲**: Workload Identity プロバイダは `main` ブランチ上のワークフロー実行にしか
  デプロイ用サービスアカウントを渡しません。他ブランチから `workflow_dispatch` してもトークン交換で拒否されます。
  さらに Deploy ワークフローは `production` 環境に紐づいているので、リポジトリ設定で必須レビュアーを付ければ
  すべてのロールアウトに人の承認を挟めます。
* **ビルドの再現性**: ベースイメージはダイジェスト固定、依存はハッシュ付きの `requirements.txt` から
  `--require-hashes` でインストールします。更新は Dependabot が PR を出します。依存を変えたときは
  Python 3.12 で `pip-compile --no-header --generate-hashes --strip-extras -o requirements.txt pyproject.toml` を
  実行してください（CI が `pyproject.toml` との乖離を検出します）。
* **リフレッシュトークンの失効** (`GmailAuthError`): OAuth 同意画面がテスト状態だと 7 日で失効します。
  再取得して `./deploy/02_secrets.sh gmail gmail-oauth.json` で更新し、Cloud Run を再デプロイしてください。

## 課金の目安

すべて Always Free 枠で動く設計ですが、枠は「使用量」で決まるので、運用量によっては超え得ます。
超えた場合も単価は小さいものの、気付かないうちに課金されないよう **予算アラート** の設定を勧めます
（`gcloud billing budgets create --billing-account=<ID> --display-name=mail-transporter --budget-amount=1USD --threshold-rule=percent=0.5`。
請求先アカウントの権限が必要です）。

| 項目 | 無料枠 | この構成での消費 | 超えやすい条件 |
|---|---|---|---|
| Cloud Run の外向き通信（北米） | 1 GiB / 月 | Gmail API への insert で **転送するメールの総バイト数**がそのまま消費される。iCloud からの取得は内向きで無料 | 大きな添付付きメールが多い月。超過分は 1 GB あたり約 $0.12 |
| Cloud Run のリクエスト / CPU | 200 万リクエスト、18 万 vCPU 秒 / 月 | 1 通あたり数秒 | 実質到達しない |
| Cloud Build | 120 ビルド分 / 日 | 1 回 1〜3 分（`cloudbuild.yaml` で前回イメージをレイヤーキャッシュに使う） | 1 日に数十回 main へ push する場合 |
| Artifact Registry | 0.5 GB | イメージは直近 2 世代のみ保持。ベースと依存のレイヤーは世代間で共有される | 依存を頻繁に変える場合。`gcloud artifacts docker images list --format='value(package,version)'` でサイズ確認 |
| GCE e2-micro | 1 台 / 月（us-west1, us-central1, us-east1） | 常時 1 台。IMAP の通信は IDLE と UID 一覧だけで本文は取得しない | リージョンを変えた場合 |
| Secret Manager | 6 バージョン、1 万アクセス / 月 | Cloud Run のコールドスタートと watcher 起動時のみ | 実質到達しない |
| Cloud Logging | 50 GiB / 月 | 1 通あたり数行 | 実質到達しない |

運用開始後は Cloud Run の「送信バイト数」（`run.googleapis.com/container/network/sent_bytes_count`）と
Artifact Registry の使用量を月に一度確認してください。

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
  gmail_client.py  Gmail API（insert / ラベル / エラー分類）
  forwarder.py     中核ロジック（1 回の同期パス）
  server.py        Cloud Run 用 Flask アプリ（/sync, /healthz）
  watcher.py       GCE 用デーモン（IDLE 監視 → Cloud Run 呼び出し）
  health.py        watcher のハートビートと /healthz（MIG 自動修復用）
  cli.py           ローカル実行用
deploy/            gcloud によるデプロイスクリプト（07 は GitHub Actions 用の Workload Identity 連携）
scripts/           Gmail OAuth リフレッシュトークン取得
.github/workflows/ ci.yml（PR でテスト）、deploy.yml（main への push で自動デプロイ）
```
