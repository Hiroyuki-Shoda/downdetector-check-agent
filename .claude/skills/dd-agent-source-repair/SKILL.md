---
name: dd-agent-source-repair
description: dd-agent の監視ソースを修復・追加する。`dd-agent diagnose` が FAILED を返した、Slack 通知が来なくなった / 内容がおかしい、Downdetector の HTML 構造が変わった、監視対象サービスを追加・削除したい、新しい種類のステータスページ（Statuspage / Statuspal / RSS など）に対応したい、しきい値を調整して誤検知や通知過多を直したい、といったときに使う。診断→原因特定→最小修正→テストの手順と、壊してはいけない不変条件を含む。
---

# dd-agent 監視ソースの修復・追加

このリポジトリで実際に発生する保守作業はほぼ2種類です。どちらも
**`dd-agent diagnose` から始めます**。

1. 外部サイト側の変更でソースが壊れた（想定される故障モード）
2. 監視対象を追加・削除したい

## 大前提: 推測でコードを書き換えない

このリポジトリのエンドポイントとセレクタは**未検証**です（外部ネットワークが
遮断された環境で書かれました）。したがって:

- **`diagnose` を実行できない環境では、セレクタやエンドポイントを変更しない。**
  実物のレスポンスを見ずに直すと、動いていたものを壊す可能性の方が高い
- そういう環境では、できることは「テストで固定されたロジックの修正」と
  「調査結果の記録」だけ。ユーザーにその旨を伝える

## Step 1: 診断する

```bash
dd-agent diagnose                          # 全サービス
dd-agent diagnose --only chatgpt           # 1サービス
dd-agent diagnose --only x --dump-dir /tmp/dd   # 取得した HTML を保存
```

出力は1サービスあたり2行（Downdetector / 公式）です。**失敗した行だけを見て
ください。** 他は触らないこと。

## Step 2: 症状から原因を引く

| `diagnose` の出力 | 原因 | 対処 |
| --- | --- | --- |
| `Downdetector: FAILED blocked: ...` | Cloudflare。IP が原因 | コードの問題ではない。Step 5 へ |
| `Downdetector: FAILED HTTP 404` | slug が違う / そのサービスが Downdetector に無い | Step 3-A |
| `Downdetector: OK` + `⚠️ 報告数の時系列を抽出できませんでした` | ページの HTML 構造が変わった | Step 3-B |
| `could not parse status text or report series` | 文言も時系列も取れず判定不能 | Step 3-B と 3-C の両方 |
| `公式(...): FAILED HTTP 404` | エンドポイントが変わった / 廃止された | Step 3-D |
| `公式(...): FAILED unexpected ... payload` | レスポンス形状が変わった | Step 3-D |
| `公式(salesforce): FAILED none of the configured instances ...` | `instances` のキーが誤り | `services.yaml` を修正するだけ |
| 通知は来るが内容が薄い / 誤っている | 判定は生きているが要約材料が不足 | Step 4 |

## Step 3-A: Downdetector の slug を直す

slug は直感と一致しません。既知の例: `openai`（`chatgpt` ではない）、
`claude-ai`、`twitter`（`x` ではない）、`googlegemini`（ハイフンなし）、
`aws-amazon-web-services`、`ntt-docomo`。

1. ブラウザで `https://downdetector.jp/shougai/<候補>/` を開いて確認する
2. `services.yaml` の `downdetector_url` を修正し、`# verified:` コメントを更新
3. **そのサービスが Downdetector に存在しない場合は `downdetector_url` の行を
   削除する。** 公式ステータスのみで動作を継続します（推測の URL を残すより
   良い）。ただし公式ステータスも無いサービス（X など）は監視できなくなるので、
   その場合はユーザーに確認する

コード変更は不要です。

## Step 3-B: 報告数の時系列の抽出を直す

`src/dd_agent/sources/downdetector.py` の `extract_series` は複数の戦略を順に
試し、4点以上取れた最初の戦略を採用します。

1. `--dump-dir` で保存した HTML から、実際のチャートデータの表記を探す
2. **既存の戦略を書き換えず、新しい戦略関数を追加**して
   `extract_series` のタプルに加える。旧表記のページが残っている可能性があり、
   フォールバックは多い方が安全
3. `tests/test_downdetector.py::TestExtractSeries` に、その表記のテストを追加

```python
def _series_from_新表記(html: str) -> list[float]:
    ...

# extract_series 内
for strategy in (_series_from_xy_objects, _series_from_pairs,
                 _series_from_json_blob, _series_from_新表記):
```

時系列が取れなくても、ページ文言が読めていれば判定は継続します（判定が
`障害の兆候` レベルまで落ちるだけ）。**慌てて他を壊さないこと。**

## Step 3-C: ページ文言のパターンを直す

同ファイルの `_OK_PATTERNS` / `_WARNING_PATTERNS` / `_OUTAGE_PATTERNS`。

重要な設計: **OK パターンを最初に評価**し、判定はページ先頭 1200 文字だけを
見ます。ページ下部の利用者コメントには正常時でも「障害」という語が頻出する
ためです。この順序と範囲を変えないでください
（`test_ok_sentence_wins_over_later_mentions_of_shougai` が固定しています）。

追加したら `tests/test_downdetector.py::TestLevelFromText` に文言を追加します。

## Step 3-D: 公式ステータスのアダプタを直す / 追加する

`src/dd_agent/sources/official.py`。`ADAPTERS` dict で `kind` から dispatch します。
既存: `statuspage`（Atlassian） / `statuspal`（Nulab） / `slack` / `google` /
`aws` / `salesforce` / `feed`（RSS·Atom） / `none`。

**まず既存アダプタで足りないか確認してください。** 多くの SaaS は Atlassian
Statuspage を使っており、`kind: statuspage` + `base:` だけで済みます。

エンドポイントが変わっただけなら `services.yaml` の修正だけで完了です。
JSON が廃止された場合は `kind: feed` に切り替えて RSS を指定するのが第一候補
（Google は障害通知に RSS を公式に推奨しています）。

新しいアダプタを書く場合の契約:

```python
def check_なにか(cfg: dict, *, timeout: float = 15.0) -> SourceResult:
    result = SourceResult(source=SOURCE, url=cfg.get("url", ""))
    try:
        data = fetch_json(cfg["api"], timeout=timeout)
    except FetchError as exc:
        result.error = str(exc)      # level は UNKNOWN のまま
        return result
    ...
    result.detail = _describe(result, "報告されている障害はありません")
    return result
```

守ること:

- **例外を投げない。** 失敗は `error` + `level=UNKNOWN`
- **`null` を想定する。** `str(d.get("k", ""))` は値が `null` のとき文字列
  `"None"` を返す。必ず `str(d.get("k") or "")` と書く（Slack に `[None]` と
  表示された実バグ）
- 未解決のインシデントだけを拾う（`resolved` / `end` / `ends_at` は除外）
- メンテナンスは障害として扱わない
- 1つのステータスページが複数サービスを載せている場合、フィルタを用意する
  （Nulab は Backlog / Cacoo を同一ページに載せている）
- 公式が「全体は正常」と表示しつつ未解決インシデントがある場合は `WARNING` に
  格上げする（提供元はバナーの更新が遅れる）

`tests/test_official.py` に追加するテスト:

1. 正常系（インシデントなし → `OK`）
2. インシデントあり（`title` / `body` / `url` が取れること）
3. 取得失敗 → `not res.ok` かつ `level is UNKNOWN`
4. `TestNullTolerance.CASES` に `null` 尽くしのペイロードを1行追加

## Step 4: 要約の内容を直す

- 材料が足りない → `summarize.build_prompt` に渡す項目を増やす。
  `tests/test_config_and_runner.py::TestSummarizePrompt` で検証できます
- 文体・粒度を変えたい → `summarize.SYSTEM_PROMPT`
- Claude API を触るときは**先に `claude-api` スキルを読む**

`summarize` はどんな失敗でも `None` を返し、`notify.template_summary` に
フォールバックします。**この性質を壊さないこと**（要約が作れなくても通知は
必ず飛ぶ）。

## Step 5: Cloudflare にブロックされた場合

これはコードの不具合ではなく IP の問題です。`curl_cffi` による TLS
フィンガープリント偽装は既に実装済みなので、次の手はコードの外にあります。

1. `curl_cffi` が実際に入っているか確認する（未導入だと `requests` に
   フォールバックし、ほぼ確実に弾かれる）
2. `services.yaml` の `downdetector_delay` を上げる
3. self-hosted runner に移す（GitHub Actions の runner はデータセンター帯）
4. `downdetector_url` を外して公式ステータスのみで運用する

**アンチボット回避の手を勝手に増やさないでください。** 3 と 4 は運用方針の
選択なので、ユーザーに提示して選んでもらいます。

## 監視対象を追加する

`services.yaml` に追記するだけです。コード変更は不要です。

```yaml
  - key: github
    name: GitHub
    downdetector_url: https://downdetector.jp/shougai/github/
    official:
      kind: statuspage
      base: https://www.githubstatus.com
      url: https://www.githubstatus.com/
```

追加後:

```bash
dd-agent list                       # 設定が読めるか
dd-agent diagnose --only github     # 実際に取得・パースできるか
```

`tests/test_config_and_runner.py` の `REQUESTED_SERVICES` は
`services.yaml` と一致していることを検証しています。サービスを追加・削除したら
このリストも更新してください（監視対象が意図せず消えるのを防ぐためのテストです）。

## しきい値を調整する

誤検知・通知過多は**コードではなく `services.yaml`** で直します。

```yaml
defaults:
  min_reports: 50        # 上げる = 小規模な報告を無視
  warning_ratio: 3.0     # 上げる = 兆候の判定を厳しく
  outage_ratio: 8.0

notify:
  level: OUTAGE          # 兆候では通知しない
  reminder_hours: 12     # 継続障害の再通知を減らす
```

サービス単位の上書きもできます（利用者数が多いサービスは平常時の報告数も多い）。

```yaml
  - key: x
    thresholds:
      min_reports: 500
```

## 完了前に必ず実行する

```bash
pytest -q          # 160 tests / 約0.3秒・ネットワーク不要
dd-agent list      # 設定の妥当性
```

`diagnose` が実行できる環境なら、**修正前に失敗を再現し、修正後に同じ行が
OK になることを確認**してください。

## 絶対に壊さないもの

詳細は [AGENTS.md](../../../AGENTS.md) の「変えてはいけない不変条件」にあります。
このスキルの作業で特に触りやすいのは以下です。

- ソースは例外を投げない（`error` + `level=UNKNOWN` を返す）
- `UNKNOWN` を `OK` として扱わない。全ソース失敗時は通知しない
- `ServiceReport.fingerprint()` に判定レベルと報告数を含めない
- Slack 送信に失敗したら状態を進めない
- 要約は常に任意。失敗したらテンプレートにフォールバック
- `min_reports`（絶対件数の下限）を外さない
- ベースラインは平均ではなく中央値
- テストはネットワークアクセスをしない
