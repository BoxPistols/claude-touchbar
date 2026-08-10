# claude-touchbar

Claude Code CLI のセッション状態を **Touch Bar** に表示し、そこから許可応答と
スラッシュコマンドを送るための道具一式。

```
Fable5 xhigh · S85 W80      ← 平常(グレー): 使っているモデルとプラン残量
🔐 許可待ち   [1 許可][2 常に許可][esc 拒否]   ← 許可プロンプトが出たときだけ
```

- **状態は色で表す** — 青=実行中 / オレンジ=許可待ち / グレー=平常
- **許可応答ボタンは実際の選択肢に追随する**（`AskUserQuestion` の N 択にも対応）
- **送信はフロントアプリへのキー送信ではなく、記録済みの iTerm2 セッションへ直接書き込む**
  ので、別アプリを触っている最中に押しても誤爆しない
- 複数セッションを **注意度順**（許可待ち > 実行中 > 待機）で 1 枠に集約。タップでそのタブへ移動

## 動作環境

| | |
|---|---|
| ハード | Touch Bar 搭載 Mac（2016〜2020 の MacBook Pro） |
| OS | macOS（26.6 で検証） |
| 必須 | [BetterTouchTool](https://folivora.ai/)（有料・ライセンス要） |
| 推奨 | iTerm2（セッション直接送信に使う。無くても表示は動く） |

Touch Bar が無い Mac では何も起きません。

## 導入

```sh
git clone https://github.com/BoxPistols/claude-touchbar.git
cd claude-touchbar
./install.sh
```

インストーラがやること: スクリプトを `~/.claude/btt/` へ配置 → 設定ファイルを
（**無ければ**）置く → Touch Bar を `appWithControlStrip` に設定 → hooks を
`~/.claude/settings.json` へ**マージ**（既存設定は壊しません）→ BTT にウィジェット投入。

> **BTT の再起動が要ります。** Shell Script Widget は投入しただけでは実行が始まりません
> （インストーラが最後に聞きます）。

### Claude Code プラグインとして使う場合

hooks だけをプラグイン経由で有効にできます（`settings.json` を編集しません）。

```
/plugin marketplace add BoxPistols/claude-touchbar
/plugin install claude-touchbar
```

この場合は `CLAUDE_TOUCHBAR_SKIP_HOOKS=1 ./install.sh` で BTT 側だけ入れてください。

## 使う

**ボタンを増やす／減らす** — BTT の UI は触りません。JSON を保存すると 1〜2 秒で反映されます。

```sh
$EDITOR ~/.claude/btt/commands.json
```

```jsonc
{
  "buttons": [
    { "label": "review", "command": "/review", "color": "38,102,168,255" },
    { "label": "model",  "command": "/model", "enter": true,
      "menu": ["Def", "Opus", "Fable", "Sonn", "Haiku"] }   // 番号ボタンが出る
  ]
}
```

`enter` の既定は `false`（入力するだけ）です。Touch Bar は画面を見ずに触る場所なので、
`/clear` のような取り消せないものが即実行されないようにしています。

**他の設定ファイル**

| ファイル | 用途 |
|---|---|
| `commands.json` | コマンドボタンの定義 |
| `permissions.json` | 許可応答ボタンのラベル |
| `web-shortcuts.json` | ブラウザ Web アプリ向けボタン |
| `native-apps.json` | **BTT を出さずアプリ自身の Touch Bar を通す**アプリ |

## 設計と実測記録

`docs/DESIGN.md` に、BTT の非公開仕様を潰していった実測記録がそのまま入っています。
本体のコードより再利用価値があるかもしれません。抜粋:

- **アプリ限定トリガーは AppleScript から作れない**（5 通りの独立した確認で確定）。
  条件表示はウィジェット側の自前判定でやるしかない
- **`BTTShellScriptWidgetGestureConfig` はトリガーのトップレベルに置く。**
  `BTTTriggerConfig` の中だとスクリプトが一切実行されず、エラーも出ず再起動でも直らない
- **アプリ別「Show App Default Touch Bar」のキーは `BTTTouchBarMode`、値 3。**
  UI 名の `TouchBarBehavior` で書くと import 時に黙って破棄される（§17）
- **毎秒 python3 を起動すると CPU をコア 1 個の 58% 使う。** 計算を常駐プロセスに
  1 本化し、ウィジェットは bash の組み込みだけで JSON を読む形にして 6% 台まで落とした（§12.1）
- **押下の検証に実タップは要らない。** `execute_assigned_actions_for_trigger` で
  再現できる（`trigger_action` では再現できない。しかも両方 `missing value` を返す）(§13.6)

## アンインストール

```sh
python3 tools/install-hooks.py --uninstall   # hooks を外す(他ツールの設定は残す)
rm -rf ~/.claude/btt                         # スクリプトと状態ファイル
```

BTT のウィジェットは BTT の UI から削除してください（印 `cc-touchbar-*` が付いています）。

## ライセンス

MIT
