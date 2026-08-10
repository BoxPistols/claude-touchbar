#!/usr/bin/env python3
"""cc-render.py から呼ばれ、Claude Code の現在状態をコンパクトに返す。

常時出すのは**設定と残量の2群だけ**:

    Opus5 1M xhigh · S17 W10
    └──── 設定 ────┘   └─ 残量 ─┘

  設定  モデルと effort。**/model /effort ボタンで切り替えた結果はここにしか出ない**
  残量  S=5hセッション / W=週間。/usage と同じ値
  例外  許可待ちのときだけ「🔐 許可待ち」で割り込む（行動を要求している状態）
  例外  コンテキストが打ち切り間近のときだけ `ctx91%` を足す

**状態は文字ではなく色で表す** — 青=実行中 / オレンジ=許可待ち / グレー=平常。
砂時計や ✓ は背景色と同じことを言っているだけで、常時2文字を消費していた。
同じ理由でツール名・経過時間・+N も落とした（TUI 側で見えるものは出さない）。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_common as cc

COLORS = {
    "busy":    ("38,102,168,255",  "255,255,255,255"),
    "waiting": ("214,138,30,255",  "255,255,255,255"),
    "idle":    ("58,58,60,255",    "190,190,195,255"),
    "none":    ("0,0,0,0",         "120,120,125,255"),
}

# コンテキスト警告を出す閾値。低くすると常時表示に近づき、設定と残量を
# 読む妨げになる。「打ち切りが現実味を帯びた」ところまで上げてある
CTX_WARN_PCT = 85


def build(sessions=None, fb=None):
    """sessions / fb は cc-render.py がティックごとに1回だけ計算して渡す
    （単独実行時は従来どおり自分で取得する）。"""
    if sessions is None:
        sessions = cc.load_sessions()
    rec, _others, _fb = cc.pick_for_front(sessions, fb)
    if not rec:
        return "none", "CC —"

    state = rec.get("state", "idle")

    # 許可待ちだけは「行動を要求している状態」なので文字で割り込む。
    # 隣に応答ボタン(1/2/esc)が出る唯一の場面でもある
    if state == "waiting" and rec.get("waiting_kind") == "permission":
        return "waiting", ("🔐 App許可待ち" if cc.is_peers(rec) else "🔐 許可待ち")

    # 実行中/平常は**色だけ**で表す。砂時計や ✓ は背景色と同じことを
    # 言っているだけで、常時2文字を消費する
    color = "busy" if state == "busy" else "idle"

    left = []
    # 設定: いま何で動いているか。/model /effort ボタンで切り替えた結果は
    # ここにしか出ない(TUI にも hook にも現れない)
    model, effort = cc.session_model_effort(rec)
    if model:
        left.append(model)
    if effort:
        left.append(effort)

    right = []
    # 残量: S=5hセッション / W=週間(all models)。/usage と同じ値
    s, w = cc.usage_limits()
    if s is not None or w is not None:
        right.append("S%s W%s" % (s if s is not None else "-",
                                  w if w is not None else "-"))
    # コンテキストは**打ち切りが現実味を帯びてから**だけ出す。消してしまうと
    # 「あと少しで会話が続かない」唯一の予告が無くなる
    ctx_pct = cc.context_pct(rec)
    if ctx_pct is not None and ctx_pct >= CTX_WARN_PCT:
        right.append("ctx%d%%" % round(ctx_pct))

    groups = [" ".join(g) for g in (left, right) if g]
    return color, " · ".join(groups) if groups else "CC —"


def render(sessions=None, fb=None):
    """ウィジェット1個ぶんの出力 dict。例外時も必ず表示可能な形を返す。"""
    try:
        color, text = build(sessions, fb)
    except Exception:
        color, text = "none", "CC —"
    bg, fg = COLORS.get(color, COLORS["none"])
    return {"text": text, "background_color": bg, "font_color": fg}


def main():
    print(json.dumps(render(), ensure_ascii=False))


if __name__ == "__main__":
    main()
