#!/bin/bash
# claude-touchbar インストーラ。
# 何度実行しても安全(冪等)。利用者が編集した設定ファイルは上書きしない。
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/.claude/btt"

say() { printf '%s\n' "$*"; }
die() { printf '!! %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "macOS 専用です(Touch Bar + BetterTouchTool)"

BTT_APP=""
for p in "/Applications/BetterTouchTool.app" "$HOME/Applications/BetterTouchTool.app"; do
  [ -d "$p" ] && BTT_APP="$p" && break
done
[ -n "$BTT_APP" ] || die "BetterTouchTool が見つかりません。先に導入してください"

# ── 1. スクリプトを配置 ────────────────────────────────────────────
# 状態ファイルの置き場を固定パスにしているのは意図的。詳細は docs/DESIGN.md §18
mkdir -p "$DEST"
cp "$REPO"/scripts/*.py "$REPO"/scripts/*.sh "$REPO"/scripts/core-widgets.json "$DEST/"
chmod +x "$DEST"/*.py "$DEST"/*.sh
say "✔ スクリプトを $DEST に配置"

# ── 2. 設定ファイルは「無ければ置く」だけ ──────────────────────────
for f in "$REPO"/defaults/*.json; do
  base="$(basename "$f")"
  if [ -e "$DEST/$base" ]; then
    say "  skip(既存を保持): $base"
  else
    cp "$f" "$DEST/$base"
    say "  置いた: $base"
  fi
done

# ── 3. Touch Bar の表示枠を確保 ────────────────────────────────────
# fullControlStrip だと BTT の表示枠が無い(既知のハマり)
mode="$(defaults read com.apple.touchbar.agent PresentationModeGlobal 2>/dev/null || echo "")"
if [ "$mode" != "appWithControlStrip" ]; then
  defaults write com.apple.touchbar.agent PresentationModeGlobal -string appWithControlStrip
  killall ControlStrip 2>/dev/null || true
  say "✔ Touch Bar を appWithControlStrip に設定"
fi

# ── 4. hooks を settings.json へマージ ─────────────────────────────
# プラグインとして導入した場合は不要(hooks/hooks.json が使われる)
if [ "${CLAUDE_TOUCHBAR_SKIP_HOOKS:-0}" != "1" ]; then
  python3 "$REPO/tools/install-hooks.py" || say "! hooks の設定に失敗(手動で設定してください)"
fi

# ── 5. BTT にウィジェットを投入 ────────────────────────────────────
if pgrep -x BetterTouchTool >/dev/null; then
  python3 "$DEST/cc-provision.py" || true
  python3 "$DEST/cc-sync.py" || true
  say "✔ ウィジェットを投入"
  say ""
  say "※ Shell Script Widget は BTT を再起動するまで実行が始まりません。"
  printf '   BTT を再起動しますか? [y/N]: '
  read -r ans
  case "$ans" in
    [yY]*)
      osascript -e 'quit app "BetterTouchTool"' >/dev/null 2>&1
      for _ in $(seq 1 15); do pgrep -x BetterTouchTool >/dev/null || break; sleep 1; done
      open -a BetterTouchTool
      say "✔ BTT を再起動しました"
      ;;
    *) say "  あとで手動で再起動してください" ;;
  esac
else
  say "! BTT が起動していません。起動後に以下を実行してください:"
  say "    python3 $DEST/cc-provision.py && python3 $DEST/cc-sync.py"
fi

say ""
say "完了。Claude Code を起動すると Touch Bar に状態が出ます。"
say "ボタンを増やす: \$EDITOR $DEST/commands.json (保存で1〜2秒後に反映)"
