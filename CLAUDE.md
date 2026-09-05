# CLAUDE.md

このリポジトリの指示は [AGENTS.md](AGENTS.md) に集約しています。二重管理して
内容がずれるのを防ぐため、こちらには Claude Code 固有の事項だけを書きます。

@AGENTS.md

## Claude Code 固有の注意

### 要約に使うモデル

`src/dd_agent/summarize.py` は Claude API を呼びます。既定は `claude-opus-5`
（`services.yaml` の `summary.model`、環境変数 `DD_CLAUDE_MODEL` で上書き可）。

このファイルや API 呼び出しに触るときは、**先に `claude-api` スキルを読んで
から**編集してください。モデル ID・パラメータの仕様は変わっており、訓練データ
の記憶で書くと動かないものが混ざります。特に:

- モデル ID に日付サフィックスを付けない（`claude-opus-5-20260401` などは無効）
- `budget_tokens` は現行モデルでは 400 になる。深さの制御は
  `output_config={"effort": ...}` を使う（このリポジトリでは `low`）
- assistant prefill は現行モデルでは 400 になる

コスト面: 要約 API を呼ぶのは**障害を検知して通知するときだけ**なので、平常時の
コストはゼロです。10分間隔の実行そのものは課金対象になりません。

### スキル

`.claude/skills/dd-agent-source-repair/` に、`diagnose` で失敗したソースを直す
手順と監視対象を追加する手順のスキルがあります。ソースが壊れた・サービスを
追加したいときはこれを使ってください。

### ネットワークが出られない環境での作業

このプロジェクトは外部ネットワークが遮断された環境で作られました。同じ状況に
なった場合:

- `dd-agent diagnose` や `dd-agent check` は全滅します。それは**コードのバグ
  ではありません**。エンドポイントが壊れていると誤診しないでください
- 検証は `pytest -q` と `dd-agent list` で行います（どちらもネットワーク不要）
- 実際のレスポンス形状を確認できないまま推測でセレクタを書き換えないこと。
  代わりに `services.yaml` の `# verified:` コメントに根拠の強さを残します
