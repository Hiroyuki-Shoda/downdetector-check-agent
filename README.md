# dd-agent — 障害検知 Slack 通知エージェント

Downdetector と各サービスの公式ステータスページを定期的に確認し、障害または
障害の兆候を検知したら、障害情報を日本語で要約して Slack に通知します。

```
🔴 障害検知: ChatGPT

ChatGPT の API で応答エラーが増加しています。
・公式ステータスは major outage として調査中
・利用者報告はログイン(62%)が最多、平常時の18倍

Downdetector                          公式ステータス
報告数 4,231件 (平常時 230件 / 比 18.4倍)   [major] Elevated error rates on API
最多報告: ログイン(62%) / アプリ(24%)

判定: 障害 ｜ 2026-09-05 12:30 JST ｜ Downdetector / 公式ステータス
```

---

## ⚠️ 最初に読んでください（未検証事項）

**このエージェントは実際の Downdetector / 公式ステータスに一度も接続していない
環境で作成されました。** 開発環境から外部ネットワークへの接続が全面的に
遮断されていたためです。したがって:

- ロジック（判定・重複除去・通知組み立て）は **160件のテストで検証済み**
- **URL とレスポンス形状は未検証**。公開ドキュメントと実例調査にもとづく実装です

そのため、稼働前に必ず次を実行してください:

```bash
dd-agent diagnose
```

各サービスについて「取得できたか」「パースできたか」を1行ずつ報告します。
失敗した行の直し方は [トラブルシューティング](#トラブルシューティング) を参照してください。

さらに、次の2点は**特に疑ってください**:

| 項目 | 状況 |
| --- | --- |
| `backlog` / `salesforce` の Downdetector URL | 裏付けなし。Downdetector が扱っていない可能性あり（404 なら該当行を削除すれば公式ステータスのみで動作継続） |
| Gmail / Gemini / AWS の JSON エンドポイント | 公式にドキュメント化されていない。取得できない場合は RSS への切替が必要（下記参照） |

---

## しくみ

2種類のソースを組み合わせ、**悪いほうの判定を採用**します。

| ソース | 役割 | 特徴 |
| --- | --- | --- |
| **Downdetector** | 検知トリガー | 利用者の報告。公式が障害を認める**前**に兆候を捉えられる。Cloudflare 保護下でスクレイピングが必要 |
| **公式ステータスページ** | 要約の材料 | 提供元自身の発表。安定した JSON API が多く、要約に載せるべき正確な情報源 |

なぜ併用するのかは、どちらか片方では穴があるためです。

- 公式だけ: X には公式ステータスが存在しない。また公式は障害を認めるまでに時間が
  かかるため「兆候」の段階では検知できない
- Downdetector だけ: Cloudflare にブロックされた瞬間に全サービスの監視が止まる。
  また「何が起きているか」は分からず、報告数しか得られない

片方のソースが取得できなくても、もう片方で監視は継続します（判定 `不明` の
ソースは無視されます）。**両方失敗した場合は「障害」ではなく「不明」として扱い、
通知しません** — エージェント自身の障害を、サービスの障害として誤報しないためです。

### 判定ロジック

| 判定 | 意味 | 条件 |
| --- | --- | --- |
| 🟢 正常 | | 両ソースが正常 |
| 🟡 障害の兆候 | | 報告数が平常時の **2.5倍**以上 / 公式が `minor` / 未解決インシデントあり |
| 🔴 障害 | | 報告数が平常時の **5倍**以上 / 公式が `major` or `critical` |
| ⚪ 不明 | 通知しない | 全ソース取得失敗 |

報告数の「平常時」は、そのページ自身の時系列データの**中央値**です（平均ではなく
中央値なのは、継続中の障害でスパイク値が平常値を押し上げないようにするため）。

倍率だけでは誤検知するため、**絶対件数の下限**（既定 20件）も併用します。
平常時1件のサービスが4件になっても 4倍ですが、これは障害ではありません。

### 通知タイミング（重複除去）

10分ごとに実行しても、同じ障害を通知し続けません。通知するのは:

| 種別 | 条件 |
| --- | --- |
| `障害検知` | 正常 → 兆候/障害 になった |
| `障害レベル上昇` | 兆候 → 障害 になった |
| `障害情報 更新` | 新しいインシデントが出た / 既存インシデントの状態が変わった（`investigating` → `identified` 等） |
| `障害継続中` | まだ直っておらず、前回通知から6時間経過 |
| `復旧` | 兆候/障害 → 正常 になった |

障害 → 兆候（部分的な改善）は通知しません。しきい値付近で判定が揺れる
サービスが10分ごとに通知を出すのを防ぐためです。

---

## セットアップ

### 1. Slack Incoming Webhook を作る

1. https://api.slack.com/apps → **Create New App** → From scratch
2. **Incoming Webhooks** を On にする
3. **Add New Webhook to Workspace** → 通知先チャンネルを選択
4. 発行された URL（`https://hooks.slack.com/services/...`）をコピー

### 2. GitHub Secrets を登録

リポジトリの **Settings → Secrets and variables → Actions** で登録します。

| Secret 名 | 必須 | 内容 |
| --- | --- | --- |
| `SLACK_WEBHOOK_URL` | ✅ | 上で取得した Webhook URL |
| `ANTHROPIC_API_KEY` | 任意 | Claude API キー。無い場合はテンプレート整形にフォールバックします |

### 3. 定期実行を有効にする

`.github/workflows/monitor.yml` が10分ごとに実行します。

> **重要:** GitHub Actions の `schedule` は**デフォルトブランチのワークフローしか
> 実行しません**。このブランチをマージするまで定期実行は始まりません。
> マージ前の動作確認は **Actions タブ → Downdetector monitor → Run workflow**
> （`dry_run` を on にすると Slack に通知せず結果だけ確認できます）から行ってください。

### 4. Salesforce のインスタンスを設定（Salesforce を監視する場合）

`services.yaml` の `salesforce.official.instances` に自社インスタンスを入れてください。

```yaml
      instances: ["AP15"]   # Salesforce の [設定] → [会社の情報] で確認
```

空のままだと Salesforce の数百インスタンスのどこかで常に何か起きているため、
通知がノイズになります。

---

## ローカルでの実行

```bash
pip install -e ".[dev]"

dd-agent list                      # 監視対象と設定を表示
dd-agent diagnose                  # 各ソースが取得・パースできるか確認
dd-agent diagnose --dump-dir /tmp/dd   # 取得した HTML を保存して中身を確認
dd-agent check --dry-run           # 通知せず判定だけ実行
dd-agent check --dry-run --json    # 機械可読な結果を出力

export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
dd-agent test-slack                # サンプル通知を送って配線を確認
dd-agent check                     # 本番実行
dd-agent check --only chatgpt --force   # 1サービスだけ強制通知
```

### 主なオプション

| オプション | 説明 |
| --- | --- |
| `--only KEY` | 特定サービスのみ（複数指定可） |
| `--dry-run` | Slack に送らない |
| `--force` | 重複除去を無視して通知する（配線確認用） |
| `--no-summary` | Claude API を使わずテンプレート整形にする |
| `--no-state` | 状態ファイルを読み書きしない |
| `--json` | 結果を JSON で出力 |
| `-v` | デバッグログ |

終了コードは **0 = 正常、1 = Slack 送信失敗、2 = 設定エラー**です。
障害を検知しても 0 です（エージェントは正常に仕事をしたため）。

---

## 監視対象と裏付け状況

`services.yaml` で定義しています。`# verified:` コメントは調査時点の裏付けの
強さを示します。

| サービス | Downdetector slug | 裏付け | 公式ステータス |
| --- | --- | --- | --- |
| X (Twitter) | `twitter` | 確認済 | **なし**（Downdetector が唯一の検知手段） |
| ChatGPT | `openai` | 推定 | Statuspage |
| Claude | `claude-ai` | 推定 | Statuspage (`status.claude.com`) |
| Google Gemini | `googlegemini` | 推定 | Google Cloud incidents |
| Gmail | `gmail` | 確認済 | Workspace ダッシュボード |
| AWS | `aws-amazon-web-services` | 推定 | `data.json`（非公式）→ Health |
| Slack | `slack` | 推定 | Slack 独自 API |
| Backlog | `backlog` | **裏付けなし** | Statuspal (Nulab) |
| Salesforce | `salesforce` | **裏付けなし** | Salesforce Trust API |

調査で判明した注意点:

- Downdetector の slug は **`chatgpt` ではなく `openai`**、**`claude` ではなく
  `claude-ai`**、**`x` ではなく `twitter`**、**`google-gemini` ではなく
  `googlegemini`**（ハイフンなし）
- Backlog は Atlassian Statuspage ではなく **Statuspal**。`/api/v2/status.json` は存在しない
- Anthropic のステータスページは **`status.claude.com`** に改称済み
- Slack の API は `v2.0.0` の指定が必須（省略すると v1.0.0 になりインシデントが1件しか返らない）
- **9サービスのうち、ドキュメント化された JSON があるのは3つだけ**（OpenAI / Claude / Slack）。
  X は公式ステータスが存在しないため、実質 Downdetector が唯一の検知手段です

---

## Cloudflare について

Downdetector は Cloudflare のボット対策下にあり、**公開 API はありません**。
そのため次の対策を実装しています。

1. **`curl_cffi` による TLS フィンガープリント偽装。** `requests` の TLS ハンドシェイクは
   実ブラウザとは異なる JA3 署名を出すため、それだけで検知されます。`curl_cffi` は
   実際の Chrome のハンドシェイクを再現します（依存に含まれています）
2. **ブラウザ相当のヘッダ**と、サービス間のアクセス間隔（既定2秒）
3. **チャレンジページの検出。** ブロックされた場合、それを「障害」ではなく
   `blocked:` エラーとして報告します

それでも失敗する場合、原因はほぼ **IP アドレス**です。GitHub Actions の runner は
データセンター帯で、Cloudflare はこれを機械的に弾きます。対処は:

- **公式ステータスのみで運用する** — `services.yaml` から `downdetector_url` の行を
  削除すれば、公式ステータスだけで動作を継続します（X は検知不能になります）
- **self-hosted runner** で実行する（データセンター帯以外の IP から出る）
- 商用のスクレイピング API を経由する

なお Downdetector の利用規約はスクレイピングについて明確ではありません。業務で
継続利用する場合は、運営元（Ookla）へのライセンス確認を検討してください。

---

## トラブルシューティング

まず `dd-agent diagnose` を実行し、該当する行を探してください。

| diagnose の出力 | 原因と対処 |
| --- | --- |
| `Downdetector: FAILED blocked: anti-bot challenge` | Cloudflare にブロック。上記「Cloudflare について」を参照 |
| `Downdetector: FAILED HTTP 404` | slug が違う。ブラウザで URL を開いて正しい slug を確認し `services.yaml` を修正。存在しないサービスなら `downdetector_url` の行を削除 |
| `Downdetector: OK` だが `⚠️ 報告数の時系列を抽出できませんでした` | ページの HTML 構造が変わった。判定はページ文言のみで継続します。`--dump-dir` で HTML を保存し、`src/dd_agent/sources/downdetector.py` の `extract_series` に抽出パターンを追加してください |
| `could not parse status text or report series` | 上と同じ。ただし文言も取れておらず判定不能。`_OK_PATTERNS` / `_OUTAGE_PATTERNS` の更新が必要 |
| `公式(google): FAILED HTTP 404` | Gmail / Gemini の JSON パスが違う。ダッシュボード下部の **RSS Feed** リンクを使い、`kind: feed` に切り替えてください（Google 自身が障害通知には RSS を推奨） |
| `公式(aws): FAILED` | `data.json` は非公式のため停止した可能性。正式な手段は AWS Health API（Business/Enterprise Support 必須）か EventBridge |
| `公式(salesforce): FAILED none of the configured instances ... appear` | `instances` のインスタンスキーが間違っている（放置すると永久に「正常」と誤判定するため、あえてエラーにしています） |

### 通知が多すぎる / 少なすぎる

`services.yaml` の `defaults` を調整します（サービス単位でも上書き可能）。

```yaml
defaults:
  min_reports: 50        # 上げる = 小規模な報告を無視する
  warning_ratio: 3.0     # 上げる = 兆候の判定を厳しくする
  outage_ratio: 8.0

notify:
  level: OUTAGE          # 兆候では通知せず、確定障害のみ通知する
  reminder_hours: 12     # 継続障害の再通知を減らす
```

```yaml
# 特定サービスだけしきい値を変える例
  - key: x
    name: X (Twitter)
    downdetector_url: https://downdetector.jp/shougai/twitter/
    thresholds:
      min_reports: 500   # 利用者が多いサービスは平常時の報告数も多い
```

### 状態がリセットされて同じ通知が再送される

通知済みフラグは GitHub Actions のキャッシュに保存しています。キャッシュは
7日間アクセスがないと削除されますが、10分ごとに実行していれば問題になりません。
より確実に永続化したい場合は、状態ファイルを専用ブランチにコミットする方式に
変更してください（`DD_STATE_PATH` でパスを指定できます）。

---

## 構成

```
services.yaml                     監視対象・しきい値・通知設定
src/dd_agent/
  models.py                       Level / Incident / SourceResult / ServiceReport
  config.py                       services.yaml の読み込みと検証
  http.py                         curl_cffi 優先の HTTP 層 + チャレンジ検出
  sources/downdetector.py         Downdetector スクレイパ（多重フォールバック）
  sources/official.py             公式ステータス 8種のアダプタ
  state.py                        重複除去の状態機械
  summarize.py                    Claude API による日本語要約
  notify.py                       Slack Block Kit 組み立てと送信
  runner.py                       1サイクルの実行
  cli.py                          check / diagnose / test-slack / list
tests/                            160 tests
.github/workflows/monitor.yml     10分ごとの定期実行
.github/workflows/test.yml        テスト CI
```

### 監視対象を追加する

`services.yaml` に追記するだけでコード変更は不要です。

```yaml
  - key: github
    name: GitHub
    downdetector_url: https://downdetector.jp/shougai/github/
    official:
      kind: statuspage
      base: https://www.githubstatus.com
      url: https://www.githubstatus.com/
```

利用できる `kind` は `statuspage` / `statuspal` / `slack` / `google` / `aws` /
`salesforce` / `feed`（RSS/Atom）/ `none` です。

### 要約に使うモデルを変える

既定は `claude-opus-5` です。10分ごとに実行しても、要約 API を呼ぶのは
**障害を検知して通知するときだけ**なので、平常時のコストはゼロです。
変更する場合:

```yaml
summary:
  model: claude-sonnet-5   # より安価
```

`ANTHROPIC_API_KEY` が未設定・無効・API 障害のいずれの場合でも、
テンプレート整形にフォールバックして**通知自体は必ず送信されます**。
