#!/usr/bin/env python3
"""許可プロンプト応答ボタンの表示制御（BTT Shell Script Widget用）。

表示条件（すべて満たすときだけ表示、他は hidden:true）:
  1. フロントアプリがターミナル（iTerm2 / Terminal）
  2. そのアプリに属するセッションが許可プロンプト待ち（waiting_kind=permission）
  3. 送信先の term_guid が記録されている（cc-send.py で直接送信できる）
  4. 選択肢が 3 つ以下で、ラベルが確定していること（下記）

**ラベルは固定ではない。** 実際の選択肢はツールごとに違い、AskUserQuestion
（選択肢を出すツール）では任意の N 択になる。固定の `2 常に許可` を出したまま
押させると、表示と違う項目が選ばれる（実機で発生）。ラベルは
cc_common.permission_labels() が解決する。

選択肢が 4 つ以上のときはここでは出さず、番号ボタン（menu-N）が実ラベル付きで
引き受ける。色分け（承認=緑 / 常に=青 / 拒否=赤）を保てるのが 3 つまでのため。

usage: cc-btn.py <allow|always|reject>
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cc_common as cc

TERMINALS = ("com.googlecode.iterm2", "com.apple.Terminal")

# 色と、選択肢のどれを担当するか。reject は必ず**最後の選択肢**（＝拒否側）で、
# 送信は esc。選択肢が2つのプロンプトでは always が出ない
BUTTONS = {
    "allow":  (0,  "1 ",   "36,110,70,255"),
    "always": (1,  "2 ",   "30,92,110,255"),
    "reject": (-1, "esc ", "130,44,44,255"),
}

HIDDEN = {"text": "", "hidden": True}


def render(kind, sessions=None, fb=None):
    """ボタン1個ぶんの出力 dict。
    sessions / fb は cc-render.py がティックごとに1回だけ計算して渡す。"""
    idx, prefix, bg = BUTTONS.get(kind, BUTTONS["allow"])

    if fb is None:
        fb = cc.front_bundle()
    if fb not in TERMINALS:
        return dict(HIDDEN)

    # 「許可待ちが1つでもあるか」ではなく**どのセッションか**を確定させる。
    # 2つ同時に許可待ちだと、見て出したボタンと送る相手がずれる（§12.3）
    rec = cc.permission_session(sessions, fb)
    if rec is None:
        return dict(HIDDEN)

    labels = cc.permission_labels(rec)
    # 0 = 出さないと決めたツール / 4以上 = 番号ボタンが引き受ける
    if not 2 <= len(labels) <= 3:
        return dict(HIDDEN)
    if idx >= len(labels) - 1:      # 2択のとき always は最後の選択肢と重なる
        return dict(HIDDEN)

    text = prefix + cc.clip(labels[idx],
                            cc.label_cells(cc.PERM_LABEL_CELLS, prefix))
    return {"text": text, "background_color": bg,
            "font_color": "255,255,255,255", "hidden": False}


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "allow"
    print(json.dumps(render(kind), ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print(json.dumps(HIDDEN))
