<div align="center">

<img src="docs/assets/nh-mark.png" alt="" width="140" height="140">

# no_human

**チケットから、レビュー済みのプルリクエストへ。**<br>***無料・オープンソース。手元のマシンで動きます。***

[English](README.md) · [简体中文](README.zh-CN.md) · **日本語** · [한국어](README.ko.md)

[![latest release](https://img.shields.io/github/v/release/no-human-ai/no_human?label=release&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases/latest) [![CI](https://img.shields.io/github/actions/workflow/status/no-human-ai/no_human/ci.yml?branch=main&label=CI)](https://github.com/no-human-ai/no_human/actions/workflows/ci.yml) [![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/) [![license MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE) [![downloads](https://img.shields.io/github/downloads/no-human-ai/no_human/total?label=downloads&color=4C9AFF)](https://github.com/no-human-ai/no_human/releases) [![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/mSARvj6yW6)

[getnohuman.com](https://getnohuman.com) · [クイックスタート](docs/quickstart.md) · [ドキュメント](docs/README.md) · [スプリントをこなす様子を見る](https://getnohuman.com/demo) · [Discord](https://discord.gg/mSARvj6yW6)

[![macOS版をダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Windows版をダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Linux版をダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

<a href="https://getnohuman.com/"><img src="docs/assets/hero-loop-poster.jpg" alt="no_humanのボード：1件のタスクが「要回答」で待機、4件が並列で進行中、1件のプルリクエストがレビュー待ち。" width="880"></a>

<sub>▶ <a href="https://getnohuman.com/">動作を見る</a> — チケットが入り、レビュー済みのプルリクエストが出てくる。ループ全体で57秒。</sub>

</div>

> この文書は英語版READMEの翻訳です。内容が食い違う場合は[英語版](README.md)が優先されます。リンク先のドキュメントは現時点ではすべて英語です。

<ins>**信頼して任せられる**</ins>、AIによるコーディングの生産ライン：

- **コードより先に、まず計画** — チケットの内容と、リポジトリを実際に調べて分かったことから計画を立てます。計画の生成に失敗した場合、コーダーには「計画なしで作業している」と明示的に伝えます。ごく小さな変更だと判定された場合は計画を省略し、コーダーには知らせません。これは意図した設計で、省略した事実はその実行のイベントストリームには記録が残ります。
- **「完了」を疑ってかかるレビュー** — 別のモデルが、コーダーの対話ログを一切見ていないセッションで、「完了」という主張を突き崩すよう指示されてレビューします。返ってくるのはファイルと行番号を挙げた合否チェックリストです。数値による自己採点は一切ありません。
- **改ざんガード** — 削除されたテスト、新たに追加されたskip、トートロジーに書き換えられたアサーション。これらはレビューのゲートが動く前に機械的に数え上げられ、受け入れ基準に照らして正当化できなければ、その試行はそこで停止します。
- **修正が本当にバグを直した証明** — 証拠として提出されるテストは、マージベースで失敗し、新しいツリーで成功しなければなりません。再現ゲートが両方を実行します。デフォルトではPythonのバグ修正に適用され、`repro_gate.mode: required`であらゆるタスク種別・あらゆる変更に適用されます。
- **手元のテストを実行** — ローカルで、任意でCI経由でも実行します。テストコマンドが見つからなかったPRには、そのまま**NOT RUN**と明記されます。
- **正直な停止** — 完了できないときは止まって理由を述べます。回答があれば先に進める状況なら具体的な質問を投げ、単に予算が尽きたのなら構造化された記録を残します。もっともらしいdiffをでっち上げることはありません。

## インストール

どのインストール方法でも**Claudeの認証情報**が必要です。`claude setup-token`で生成するOAuthトークン（個人サブスクリプション・エンタープライズのどちらでも可）を使うため、先にClaude Code CLIをインストールしてください。`npm install -g @anthropic-ai/claude-code`、または`curl -fsSL https://claude.ai/install.sh | bash`で入ります。デスクトップアプリもタスクごとにこのCLIを呼び出します。Anthropicに直接支払う場合は、`llm.auth_mode: "api_key"`を設定し、`ANTHROPIC_API_KEY`を`~/.no_human/.env`に置いてください。

### 1行で（CLI + ボード）

```bash
uv tool install no-human   # または pipx install no-human — wheelにボードが同梱されています
nh init && nh doctor       # トークン、設定、最初のリポジトリ。その後、インストールが実際に動くか検証
```

### デスクトップアプリ

[![macOS版をダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-macOS-4C9AFF?style=for-the-badge)](https://github.com/no-human-ai/no_human/releases/latest) [![Windows版をダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-Windows-4C9AFF?style=for-the-badge)](https://getnohuman.com/) [![Linux版をダウンロード](https://img.shields.io/badge/%E3%83%80%E3%82%A6%E3%83%B3%E3%83%AD%E3%83%BC%E3%83%89-Linux-4C9AFF?style=for-the-badge)](https://getnohuman.com/)

各リリースでは成果物とあわせてSHA-256を配布しています。プラットフォーム別の注意点と初回起動の手順は[docs/quickstart.md](docs/quickstart.md)へ。

### ソースから

```bash
git clone https://github.com/no-human-ai/no_human.git && cd no_human
uv sync                 # `nh` エントリポイントを .venv にインストール
(cd web && npm install && npm run build)   # ボードのビルド(初回は数分かかることがあります)
uv run nh init          # トークン、設定、最初のリポジトリ(2分ほど)
uv run nh doctor        # 頼る前に、インストールが実際に動くか検証
```

ボードを使うなら`web`のビルドは省略できません。ソースのチェックアウトに`web/dist`は含まれないため、ビルドしないと`nh start`はAPIだけを提供し、UIは何も表示されません。Python 3.12以上、[uv](https://github.com/astral-sh/uv)、gitが必要です。ボードのビルドにはnpmを含むNodeも必要です。

## 主な機能

<table>
  <tr>
    <td width="36%" valign="middle">
      <h3>コードより先に、まず計画</h3>
      <p>チケットの内容とリポジトリを調べて分かったことから、確認できる受け入れ基準を書き出します。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-plan.png" alt="タスクの計画：理解した内容としての受け入れ基準3件、変更する2ファイル、アプローチ、テスト計画、対象外の事項、検証コマンド。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>独立したレビュアー</h3>
      <p>コーダーのセッションを一切見ていない別のモデルが、「完了」を突き崩すよう指示されてレビューします。合否を判定し、ブロッキングの指摘はすべてファイルと行番号を挙げます。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-verdict.png" alt="レビュアーの判定：PASSED。受け入れ基準それぞれに、それを満たすファイルと行番号を添えてチェックが付き、非ブロッキングの指摘が1件、該当diff付き。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>手元のテストを、PRの表に</h3>
      <p>ローカルで、あるいはCI経由で実行します。テストコマンドが見つからなければ<b>NOT RUN</b>と明記され、空欄にはなりません。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tests.png" alt="タスクのTest resultsパネル：CLEAN、5件中5件が成功、その下にpytestの出力。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>改ざんガード</h3>
      <p>削除されたテスト、新たなskip、トートロジーになったアサーションを、レビューの前に数え上げます。正当化できなければ、その試行はそこで停止します。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-tamper.png" alt="停止した試行：赤いTAMPER DETECTEDバナー、レビュアー判定FAILED、そして「テスト3件が削除され、正当化できる受け入れ基準がない」というブロッキングの指摘。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>修正が本当にバグを直した証明</h3>
      <p>証拠として出されるテストは、古いコードで失敗し、新しいコードで成功しなければなりません。ゲートは両方を実行し、判定はイベントログに残ります。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-repro.png" alt="タスクのイベントログ：テスト成功、ステータスはreviewing、レビュアーの改ざんチェックはnone、再現ゲートはpass（required）、続いてlint、コミット、プルリクエスト作成。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>正直な停止</h3>
      <p>あなたの判断が必要なときは、推測せずに具体的な質問をひとつ添えて止まります。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-question.png" alt="ボードのNeeds answerレーン：「Dedupe by user, or by digest id?」という質問を抱えて止まっているタスクが1件と、Answer questionボタン。隣にWorkingレーンとReview PRレーン。" width="100%" />
    </td>
  </tr>
  <tr>
    <td width="36%" valign="middle">
      <h3>使っているトラッカーのチケットを、そのままボードへ</h3>
      <p>バックログからJiraまたはLinearのチケットを選びます（monday.comのボードはポーリングで取り込みます）。着手前に一件ずつあなたと一緒にスコープを決めます。</p>
    </td>
    <td width="64%">
      <img src="docs/assets/readme/highlight-backlog.png" alt="Jiraから同期されたBacklog：一致するチケット4件が選択され、Start 4 tasksボタン。" width="100%" />
    </td>
  </tr>
</table>

<sub>画面はデモ用ワークロードを載せた実際のボードです。</sub>

## タスクを1つ動かす

引数なしで`nh`を実行するとシェルが開きます。レーンの一覧、イベントのライブ表示、自然な言葉でタスクを説明できる入力欄がそろっています。以下のコマンドはすべてそのまま使えます。

```bash
nh                                   # シェル
nh start                             # ボード + ワーカーを 127.0.0.1:8420 で起動
nh task add https://github.com/org/repo/issues/42 --repo ~/git/repo
nh status                            # needs-you / working / waiting / done
nh review <id>                       # レビュアーの証拠チェックリスト
nh diff <id>                         # 提出しようとしている差分
nh approve <id>                      # 承認すると PR を squash マージ(git.approve_identity)
nh reject <id> --reason "..."        # フィードバックを付けて差し戻し
```

## インテグレーション

普段使っているトラッカーとno_humanを連携させれば、チケットがボードに入ってきます。トラッカーのフィルタは設定ファイルに書くもので、タスク自身のテキストには決して入りません。通信エラーはログに残して次のポーリングで再試行し、プールを落としません。

| トラッカー | チケットの取り込み方 | 設定するフィルタ |
|---|---|---|
| **Jira Cloud** | REST `search/jql`をポーリング（HTTP Basic `email:token`） | `integrations.jira.jql` |
| **Linear** | GraphQL APIをポーリング | `integrations.linear.team_key` + `state_types` + `label` |
| **monday.com** | GraphQL v2をポーリング | `integrations.monday.board_id` + `status_column` + `todo_labels` |

書き戻し（`write_back`、デフォルトはオフ）を有効にすると、チケットはタスクと一緒に遷移します。照合はステータスのカテゴリ、種別、または指定したラベルで行い、遷移IDのハードコードはしません。PRリンクも付きます。人の対応が必要なタスクにはコメントが付くだけで、遷移させることはありません。GitHubとGitLabのIssueはURLでタスクとして取り込め、PR/MRは自分のホスト上に開きます。対応が必要なタスクが出るとSlackやTeamsに通知が届き、JenkinsとCircleCIでテストレイヤーを実行してループのゲートにできます。各設定は[docs/adapters.md](docs/adapters.md)へ。

**Jiraの流れを最初から最後まで見る** — Jiraボードから同期されたチケットが、スコープを切られ、実装され、レビューを通過したプルリクエストとして届くまで。クリックで全ステップ入りの動画が開きます：

[![Jiraフローのデモ](https://getnohuman.com/assets/demo-jira.gif)](https://getnohuman.com/assets/demo-jira.mp4)

<p align="center">▶️&nbsp;&nbsp;<strong><a href="https://getnohuman.com/assets/demo-jira.mp4">フルデモを再生</a></strong> — 1:33、JiraボードからレビューをパスしたPRまで</p>

## MCPサーバー — いま使っているエージェントから仕事を渡す

no_humanは**MCP（Model Context Protocol）サーバー**を同梱しています。公式のPython MCP SDKで作られたstdioブリッジで、Claude CodeやCursor、任意のMCPクライアントから、ローカルのno_humanに仕事を渡し、進み具合を確認できます。

```bash
nh mcp-serve        # MCPサーバー(stdio)
```

ツールはこの2つだけです。

| ツール | 何をするか |
|---|---|
| `task_add(title, description, repo_path)` | タスクを登録します。no_humanが計画を立て、変更を書き、テストを走らせ、別モデルにレビューさせて、プルリクエストを開きます。 |
| `task_status(task_id_or_external_id)` | そのタスクの現在の状態を返します。ステータス、試行回数、そして（できていれば）PRリンク。 |

通信先は手元のno_human（`http://127.0.0.1:8420`）だけで、それ以外とは通信しません。認証はありません。宛先がlocalhostだからです。また、間にこちらのサービスが挟まることもありません。Claude Codeでは同じサーバーがプラグインとしても提供されます。このリポジトリ自体がプラグインマーケットプレイスなので、次の2行でこの2つのツールがセッションに現れます。

```
/plugin marketplace add no-human-ai/no_human
/plugin install no-human@no-human-ai
```

その他のMCPクライアントは、通常どおりstdioエントリを設定します。

```jsonc
// .mcp.json
{ "mcpServers": { "no_human": { "command": "nh", "args": ["mcp-serve"] } } }
```

## ドキュメント

| | |
|---|---|
| [quickstart.md](docs/quickstart.md) | ゼロから最初のタスクまで、プラットフォーム別 |
| [configuration.md](docs/configuration.md) | すべての設定項目とデフォルト |
| [verification.md](docs/verification.md) | 各ゲート、回数制限付きループ、その限界 |
| [security.md](docs/security.md) | 認証の境界、「マージしない」ルール、各ガード |
| [blockers.md](docs/blockers.md) | エスカレーション、wake watcher、`nh reply` |
| [adapters.md](docs/adapters.md) | 取り込み、コンテキスト、VCSとCIのバックエンド |
| [eval.md](docs/eval.md) | ゴールデンセット、リプレイ採点、シャドウモード |
| [CHANGELOG.md](CHANGELOG.md) | リリースごとの変更点 |

## 開発

```bash
uv sync
uv run pytest -q
uv run nh --help
```

Issueとプルリクエストを歓迎します。提出前に`uv run pytest -q`を実行してください。

no_humanがレビューを1周分でも省いてくれたなら、スターを付けていただけると、他の人がこのプロジェクトを見つけやすくなります：
[![GitHub stars](https://img.shields.io/github/stars/no-human-ai/no_human?style=social)](https://github.com/no-human-ai/no_human/stargazers)

## コミュニティ

質問、バグ報告、見せたい実行結果は [Discord](https://discord.gg/mSARvj6yW6) へ。
[r/no_human](https://www.reddit.com/r/no_human/) への投稿や
[GitHub issue](https://github.com/no-human-ai/no_human/issues) でも歓迎です。

## ライセンス

MIT — [LICENSE](LICENSE)を参照。ライセンスが対象とするのはコードであって名前ではありません。「no_human」の名称とロゴの使用に関するポリシーは[TRADEMARK.md](TRADEMARK.md)にあります。バイナリとしてパッケージングする場合には、ソースツリーにはない義務が伴います。一覧は[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)にあります。
