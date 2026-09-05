# AGENTS.md

コーディングエージェント向けのリポジトリ指示書です。人間向けの導入・運用手順は
[README.md](README.md) にあります。

## このリポジトリ

`dd-agent` — Downdetector と各サービスの公式ステータスページを定期的に確認し、
障害または障害の兆候を検知したら日本語で要約して Slack に通知する CLI。
GitHub Actions で10分ごとに実行されます。

Python 3.10+ / 依存は `PyYAML` `requests` `beautifulsoup4` `curl_cffi` `anthropic`。

## コマンド

```bash
pip install -e ".[dev]"

pytest -q                      # 全テスト（ネットワークアクセスなし・約0.3秒）
dd-agent list                  # 設定の妥当性を目視確認
dd-agent diagnose              # 各ソースの取得・パース可否を確認（要ネットワーク）
dd-agent check --dry-run       # 通知せず判定だけ実行（要ネットワーク）
```

変更後は `pytest -q` と `dd-agent list` を必ず通してください。両方ネットワーク
不要です。

## アーキテクチャ

```
services.yaml            監視対象・しきい値・通知設定（コードより先にここを見る）
src/dd_agent/
  models.py              Level / Incident / SourceResult / ServiceReport
  config.py              services.yaml の読み込みと検証
  http.py                curl_cffi 優先の HTTP 層 + アンチボット検出
  sources/downdetector.py  Downdetector スクレイパ
  sources/official.py      公式ステータス 8種のアダプタ（ADAPTERS で dispatch）
  state.py               重複除去の状態機械
  summarize.py           Claude API による日本語要約
  notify.py              Slack Block Kit 組み立てと送信
  runner.py              1サイクルの実行（collect → decide → notify → record）
  cli.py                 check / diagnose / test-slack / list
```

データの流れは一方向です。

```
sources/* → SourceResult → ServiceReport → state.decide → summarize → notify → state.record
```

## 変えてはいけない不変条件

以下はすべて「一見冗長だが意図的」なものです。壊すと**障害を検知できなくなる**か
**誤報する**かのどちらかになります。変更する場合は理由をコミットメッセージに書いて
ください。

### 1. ソースは例外を投げない

`sources/*` の関数は必ず `SourceResult` を返します。取得に失敗したら
`error` を設定して `level` は `UNKNOWN` のままにします。1つのソースの失敗が
他サービスのチェックを止めてはいけません。`official.check` の dispatch には
アダプタのバグを封じ込める `try/except` があります。これを外さないでください。

### 2. `UNKNOWN` は `OK` ではない

- `ServiceReport.level` は `UNKNOWN` のソースを**無視**して残りの最悪値を採る。
  Downdetector がブロックされても公式の major 障害が隠れてはいけないため
- 全ソースが `UNKNOWN` のとき `state.decide` は必ず沈黙する。エージェント自身の
  障害をサービス障害として誤報しないため
- `state.record` は `UNKNOWN` のとき**状態を更新せず return** する。継続中の
  障害を「復旧」に見せかけないため

### 3. `ServiceReport.fingerprint()` に判定レベルと報告数を含めない

含めると、しきい値付近で揺れるサービスが10分ごとに通知を出します。レベル遷移は
`decide` の escalation / recovery 分岐が明示的に扱っています。
（`tests/test_state.py::TestFingerprint::test_independent_of_level` が固定しています）

### 4. Slack 送信に失敗したら状態を進めない

`runner.run` は `if not decision.notify or outcome.delivered:` のときだけ
`state.record` を呼びます。Slack 障害で唯一重要な通知が飲み込まれるのを防ぐため、
次サイクルで再試行されます。

### 5. 要約は常に任意

`summarize.summarize` はどんな失敗でも `None` を返し、`notify.template_summary`
にフォールバックします。**要約が作れなくても通知自体は必ず送信される**こと。
Claude API を必須の依存にしないでください。

### 6. 報告数の絶対下限（`min_reports`）を外さない

平常時1件のサービスが4件になれば「4倍」ですが障害ではありません。倍率だけの
判定は低トラフィックのサービスで誤検知します。ベースラインは平均ではなく
**中央値**です（継続中のスパイクが平常値を押し上げないため）。

### 7. 監視対象の追加は `services.yaml` だけで完結させる

新しいサービスを追加するためにコードを触る必要がある状態にしないでください。
新しい**種類**のステータスページに対応する場合のみ `official.ADAPTERS` に
アダプタを追加します。

### 8. GitHub Actions の `run:` に `${{ }}` を直接展開しない

入力値がシェルとして評価されます（スクリプトインジェクション）。必ず `env:`
経由で渡してください。また `set -e` 下で `[ cond ] && cmd` を使わないこと
（条件が偽のときステップ全体が落ちます）。`if` で書きます。

## テストの規約

- **テストは一切ネットワークアクセスをしない。** `fetch` / `fetch_json` を
  monkeypatch します。`official.py` の `fetch` はモジュールレベルで import
  されているので差し替え可能です（関数内 import に戻すとテストできなくなります）
- 新しいアダプタを追加したら `tests/test_official.py` に最低限これを追加:
  正常系 / インシデントあり / 取得失敗が `UNKNOWN` になること /
  `TestNullTolerance.CASES` への1行
- ペイロードの**キーが存在して値が `null`** のケースを想定してください。
  `str(d.get("k", ""))` は `null` に対して文字列 `"None"` を返します。
  必ず `str(d.get("k") or "")` と書きます（Slack に `[None]` と表示された実バグ）

現在 160 tests / 約0.3秒。内訳: official 56 / state 32 / config+runner 26 /
downdetector 25 / notify 21。

## コードの書き方

- **コード内のコメント・docstring は英語、ユーザーに見える文字列は日本語。**
  Slack 通知、CLI 出力、`services.yaml` のコメント、README、この文書は日本語
- コメントは「何をしているか」ではなく「なぜそうしているか」を書く。この
  リポジトリのコメントはほぼすべて、一見不要に見える処理の理由の説明です
- `Level` は `IntEnum`。複数ソースの合成は `max()` で済むように順序が付いています
- 型注釈は付ける。`from __future__ import annotations` を使っています

## 既知の未検証事項（重要）

**このリポジトリのコードは、実際の Downdetector / 公式ステータスに一度も接続
できていない環境で書かれました。** 開発環境の外部ネットワークが全面遮断されて
いたためです。

- 判定・重複除去・通知組み立てのロジックはテスト済み
- **URL とレスポンス形状は未検証**

したがって:

- エンドポイントやセレクタについて「動いている」と断定しないでください。
  確認できるのは `dd-agent diagnose` を実行できる環境だけです
- `services.yaml` の `# verified:` コメント（`confirmed` / `inferred` / `guess`）は
  裏付けの強さを表します。変更したら更新してください
- 特に `backlog` / `salesforce` の Downdetector URL は裏付けがなく、そもそも
  Downdetector が扱っていない可能性があります

## 落とし穴

| 事象 | 理由 |
| --- | --- |
| Downdetector の slug が直感と違う | `chatgpt` ではなく `openai`、`claude` ではなく `claude-ai`、`x` ではなく `twitter`、`googlegemini`（ハイフンなし） |
| Backlog に `/api/v2/status.json` がない | Atlassian Statuspage ではなく **Statuspal**。スキーマが別物 |
| Slack API のインシデントが1件しか返らない | URL に `v2.0.0` が必要。省略すると v1.0.0 になる |
| Salesforce が常に障害に見える | `instances` 未設定。数百インスタンスのどこかで常に何か起きている |
| 定期実行が始まらない | GitHub Actions の `schedule` はデフォルトブランチのワークフローしか実行しない |
| ローカルで `dd-agent diagnose` が全滅する | ネットワークが出られない環境。ロジックの確認は `pytest` で行う |

## ブランチ

開発は `claude/downdetector-slack-agent-owgzin` で行っています。PR は明示的に
依頼されたときだけ作成してください。
