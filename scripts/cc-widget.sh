#!/bin/bash
# BTT の Shell Script Widget から毎秒呼ばれる読み取り専用ラッパー。
#
#   . cc-widget.sh status | perm-allow | perm-always | perm-reject | menu-N
#
# BTT からは `bash -c` の中で **source** される（`/bin/bash cc-widget.sh` だと
# bash が 2 プロセスになり実測 3.7ms → 7.0ms に倍増する）。末尾の exit 0 は
# 呼び出し元シェルをそのまま終わらせるためのもので、意図的。
#
# 表示内容は常駐する cc-render.py が render/<name>.json に書く。ここでは
# 外部プロセスを一切起動しない — read / printf / kill / : はすべて bash の
# 組み込みで、これがウィジェット数 × 毎秒だけ走る。
# **重い処理をここに足さないこと。** 以前は毎秒 python3 を起動していて、
# 9 ウィジェットで 576ms/s = コア1個の 58% を消費していた。
BASE="$HOME/.claude/btt"
NAME="${1:-status}"

# デーモンの面倒を見るのはステータスだけ。全員にやらせるとデーモン停止中に
# 毎秒 9 個の python3 が湧く
if [ "$NAME" = "status" ]; then
  # 「まだ読まれている」印。これが止まると cc-render.py は自分で終了する
  # (BTT を終了したあとも常駐し続けてバッテリーを食うのを防ぐ)
  : > "$BASE/render/.seen" 2>/dev/null
  # 判定は kill -0 だけで行う。read の戻り値を条件に入れると、改行で
  # 終わらない pid ファイルを毎回「読めなかった」と誤判定して毎秒起動を試みる
  # 2>/dev/null は入力リダイレクトより**先**に置く。後ろだと、ファイルが
  # 無いときのリダイレクト失敗メッセージが素の stderr に出てしまう
  IFS= read -r pid 2>/dev/null < "$BASE/render/daemon.pid"
  if ! kill -0 "$pid" 2>/dev/null; then
    /usr/bin/python3 "$BASE/cc-render.py" >> "$BASE/render.log" 2>&1 &
  fi
fi

# read の戻り値は見ない。改行で終わらないファイルでも変数には入っているため、
# 中身が空かどうかだけで判定する
IFS= read -r line 2>/dev/null < "$BASE/render/$NAME.json"
if [ -n "$line" ]; then
  printf '%s' "$line"
elif [ "$NAME" = "status" ]; then
  # デーモン起動直後の1〜2ティックはここに来る
  printf '{"text":"CC …","background_color":"0,0,0,0","font_color":"120,120,125,255"}'
else
  printf '{"text":"","hidden":true}'
fi
exit 0
