#!/usr/bin/env python3
"""ブラウザ Web アプリ向け Touch Bar ボタンを BTT へ投入する。

    web-sync.py sync     web-shortcuts.json の内容を BTT へ反映（冪等）
    web-sync.py send N   N 番のボタンを押したときの処理（BTT から呼ばれる）
    web-sync.py status   投入済みボタンの一覧

cc-sync.py と同じ流儀（MARKER で自分のボタンだけを管理・state で差分更新・
SCHEMA を上げると全部作り直し）だが、対象がターミナルではなくブラウザなので
機構は別。cc-*.py には一切触らない。

■ なぜ押下時にアプリを照合するのか（実測に基づく設計）

BTT の AppleScript ではボタンをアプリ別スコープに置けない。add_new_trigger の
sdef に app 指定のパラメータが無く、JSON に "BTTBelongsToApp" を入れても
**保存時に破棄される**（BTT 6.687 で実測: 追加後 get_trigger に出てこない。
get_triggers trigger_app_bundle_identifier での件数も 0 のまま）。

つまりボタンは必ず Global に作られ、そのまま素の「ショートカット送信」
(BTTPredefinedActionType 264) にすると**最前面のアプリが何であれキーが飛ぶ**。
Cmd+Z が別アプリに飛ぶと実際の作業が巻き戻るので、これは許容できない。

そこで本体アクションはシェル実行 (137) にして、送信直前にこのスクリプトが
最前面の bundle id を照合する。外れていれば何も送らない。cc-menu.py が
「送ってよい状態か」を送信直前に確認しているのと同じ考え方。

キー送信そのものは BTT に投げ返す (trigger_action)。osascript から
System Events で叩くと呼び出し元プロセスに Accessibility 権限が要るが、
BTT 経由なら BTT の権限で送られるため追加の許可が要らない。

■ 常駐コストについて

表示判定はウィジェット側の**インライン bash 1 行**で行う（lsappinfo 2 回・実測 16ms）。
更新間隔は 2 秒なので 2 ボタンで約 17ms/s。**ここで python3 を起動しないこと** —
`python3 -c pass` だけで 47.5ms かかり、macenv が 9 ウィジェットでコア 1 個の
58% を燃やした前例がある。重い判定を足したくなったら、常駐プロセス 1 本に集約して
ウィジェットはその結果を読むだけにする（cc-render.py と同じ形）。

このスクリプト自体（python）が動くのは**押したときだけ**。

■ 動作確認のしかた（Touch Bar を押さずに検証できる）

    osascript -e 'tell application "BetterTouchTool" to \\
      execute_assigned_actions_for_trigger "<UUID>"'

が実際の押下と同じ経路。**trigger_action <UUID> では実行されない**
（どちらも "missing value" を返すので戻り値では区別できない。実測）。
結果は web-sync.trace に残るので、
  ファイル無し   → BTT がスクリプトを実行していない
  skip [...]    → 実行され、対象アプリでないので送らなかった
  sent [...]    → 送った
を切り分けられる。UUID は `web-sync.py status` で出る。
"""
import fcntl
import hashlib
import json
import os
import subprocess
import sys

BASE = os.path.expanduser("~/.claude/btt")
CONF = os.path.join(BASE, "web-shortcuts.json")
STATE = os.path.join(BASE, "web-sync.state.json")
LOCK = os.path.join(BASE, "web-sync.lock")
TRACE = os.path.join(BASE, "web-sync.trace")
MARKER = "web-touchbar"
SCHEMA = 2   # 629→642 へ変更。上げると全ボタン作り直し
# Touch Bar は複数プロジェクトが同居する共有資源。BTTOrder が重複すると
# 並びが混ざる（実測: domdom-inspector も 100 起点で、UIB 入力 と Inspect が
# 同じ 100 になり "UIB 入力 | Inspect | UIB テーマ | ▲▼esc⚙" と割り込まれた）。
# 現在の割り当て:
#   0〜55    macenv (cc-*: status/許可/コマンド/番号メニュー)
#   100〜105 domdom-inspector
#   200〜    ここ (web-touchbar)
ORDER_BASE = 200
DEFAULT_COLOR = "58.000000, 58.000000, 60.000000, 255.000000"
PY = "/usr/bin/python3"


def osa(script, *args):
    r = subprocess.run(
        ["/usr/bin/osascript", "-e",
         'on run argv\ntell application "BetterTouchTool" to ' + script + "\nend run",
         *args],
        capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or r.stderr).strip()


def front_bundle():
    """最前面アプリの bundle id。lsappinfo は Accessibility 権限が要らない。"""
    try:
        out = subprocess.run(
            "/usr/bin/lsappinfo info -only bundleid `/usr/bin/lsappinfo front`",
            shell=True, capture_output=True, text=True, timeout=2).stdout
        parts = out.split('"')
        return parts[3] if len(parts) >= 4 else ""
    except Exception:
        return ""


def load_buttons():
    """enabled でないものは落とす。落としたぶん index は詰める。

    描画側と投入側で別々にフィルタすると、条件が片方だけ変わったときに
    index がずれて「別のボタンを送る」形で静かに壊れる。読み取りはここ一本。
    """
    with open(CONF) as f:
        conf = json.load(f)
    out = []
    for b in conf.get("buttons", []):
        if not b.get("enabled", True):
            continue
        if not b.get("keys") or not b.get("bundle"):
            print("skip (keys/bundle 未指定): %s" % b.get("label"), file=sys.stderr)
            continue
        out.append(b)
    return out


def color_of(b):
    c = b.get("color")
    if not c:
        return DEFAULT_COLOR
    try:
        parts = [float(x.strip()) for x in c.split(",")]
        if len(parts) == 3:
            parts.append(255.0)
        return ", ".join("%f" % v for v in parts[:4])
    except Exception:
        return DEFAULT_COLOR


def entry_hash(b, order):
    basis = json.dumps([b.get("label"), b.get("keys"), b.get("bundle"),
                        b.get("color"), order, SCHEMA], ensure_ascii=False)
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def width_of(label):
    # 日本語は約2倍幅。余裕を持たせないと文字が切れる
    w = sum(2 if ord(ch) > 0x2000 else 1 for ch in label)
    return max(70, min(150, 22 + w * 8))


def visibility_script(b):
    """毎更新ごとに走る表示判定。**bash の組み込みだけで完結させること。**

    python3 を起動すると `python3 -c pass` だけで 47.5ms かかり、ウィジェット数 ×
    毎更新ぶん効く（macenv が 9 ウィジェットでコア 1 個の 58% を燃やした前例あり）。
    ここは lsappinfo 2 回のみで実測 16ms。

    大文字小文字を無視するのは、lsappinfo が "com.google.Chrome"（C が大文字）を
    返すのに対し LaunchServices の既定ブラウザ設定は "com.google.chrome" だから。
    完全一致にすると Chrome だけ静かに外れて原因が分からなくなる。
    shopt -s nocasematch は組み込みなのでプロセスを増やさない。
    """
    want = b["bundle"].lower()
    shown = json.dumps({"text": b["label"],
                        "background_color": b.get("color", "58,58,60,255"),
                        "hidden": False}, ensure_ascii=False)
    hidden = json.dumps({"text": "", "hidden": True})
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    return (
        'b=$(/usr/bin/lsappinfo info -only bundleid '
        '"$(/usr/bin/lsappinfo front)" 2>/dev/null); shopt -s nocasematch; '
        'case "$b" in *%s*) printf "%s";; *) printf "%s";; esac'
        % (want, esc(shown), esc(hidden))
    )


def trigger_json(b, order, index):
    """Touch Bar ウィジェット(642) + 押下時シェル実行(137)。

    629 のプレーンボタンだと**常時表示**になり、対象アプリ以外でも Touch Bar の
    幅を占有する（実機で右端が切れた）。BTT はアプリ別スコープを AppleScript から
    設定できないため、表示制御はウィジェット側の自前判定が唯一の手段。

    素の「ショートカット送信」(264)にしないこと。264 は最前面のアプリへ
    無条件に飛ぶ（このファイル冒頭の理由）。ここでは send 側で照合する。
    表示が消えていても押下経路は別なので、ガードは送信側にも必要。
    """
    name = b["label"]
    return {
        "BTTWidgetName": name,
        "BTTTriggerType": 642,
        "BTTTriggerClass": "BTTTriggerTypeTouchBar",
        "BTTTriggerTypeDescription": name,
        "BTTPredefinedActionType": 137,
        "BTTTerminalCommand": '%s "$HOME/.claude/btt/web-sync.py" send %d'
                              % (PY, index),
        "BTTNotes": MARKER,
        "BTTEnabled": 1,
        "BTTEnabled2": 1,
        "BTTOrder": order,
        # 642 では**トリガーのトップレベル**に置くこと。BTTTriggerConfig の中に
        # 入れるとスクリプトが一切実行されず、BTT を再起動しても直らない。
        # （629 + 137 のときは逆に不要で、渡しても BTT が保存時に捨てていた）
        "BTTShellScriptWidgetGestureConfig": "/bin/bash:::-c",
        "BTTTriggerConfig": {
            "BTTTouchBarButtonName": name,
            "BTTTouchBarItemPlacement": 0,
            "BTTTouchBarButtonFontSize": 12,
            "BTTTouchBarButtonCornerRadius": 6,
            "BTTTouchBarFreeSpaceAfterButton": 4,
            # 642 は BTTTBWidgetWidth。629 の BTTTouchBarButtonWidth とは別キー
            "BTTTBWidgetWidth": width_of(name),
            "BTTTouchBarButtonColor": color_of(b),
            "BTTTouchBarShellScriptString": visibility_script(b),
            "BTTTouchBarAlwaysShowButton": False,
            # 表示制御だけなので 2 秒で十分。1 秒にするとコストが倍になる
            "BTTTouchBarScriptUpdateInterval": 2,
        },
    }


def trace(msg):
    """押下の痕跡。BTT はスクリプトの標準出力を捨てるので、これが無いと
    「押しても何も起きない」ときに *実行されていない* のか *実行されて
    送信を見送った* のかを切り分けられない。1 行だけ保持する。"""
    try:
        with open(TRACE, "w") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def cmd_send(index):
    """押下時: 対象アプリが最前面のときだけキーを送る。"""
    buttons = load_buttons()
    if index < 0 or index >= len(buttons):
        trace("no such button: %d" % index)
        print("no such button: %d" % index, file=sys.stderr)
        return 1
    b = buttons[index]
    front = front_bundle()
    want = b["bundle"].lower()
    if front.lower() != want:
        # 黙って落とす。ここで送るとブラウザ以外へキーが飛ぶ
        trace("skip [%s] front=%s want=%s" % (b["label"], front or "(unknown)", want))
        print("skip: front=%s want=%s" % (front or "(unknown)", want))
        return 0
    # キー送信は BTT 自身に行わせる（BTT の Accessibility 権限で送られるため
    # このスクリプトに追加の許可が要らない）
    rc, out = osa("trigger_action (item 1 of argv)",
                  json.dumps({"BTTPredefinedActionType": 264,
                              "BTTShortcutToSend": b["keys"]}))
    if rc != 0:
        trace("send failed [%s]: %s" % (b["label"], out))
        print("send failed: %s" % out, file=sys.stderr)
        return 1
    trace("sent [%s] %s -> %s" % (b["label"], b["keys"], front))
    print("sent %s -> %s" % (b["keys"], front))
    return 0


def cmd_status():
    rc, out = osa("get_triggers")
    if rc != 0:
        print("BTT に問い合わせできません: %s" % out, file=sys.stderr)
        return 1
    try:
        triggers = json.loads(out)
    except Exception:
        print("BTT の応答を解釈できません", file=sys.stderr)
        return 1
    mine = [t for t in triggers if t.get("BTTNotes") == MARKER]
    print("投入済み: %d 件" % len(mine))
    for t in mine:
        # 642 は BTTWidgetName、629 は BTTTouchBarButtonName に名前が入る
        name = t.get("BTTWidgetName") or t.get("BTTTouchBarButtonName") or "(no name)"
        print("  %-14s %s" % (name, t.get("BTTUUID")))
    return 0


def cmd_sync():
    buttons = load_buttons()
    state = {}
    if os.path.exists(STATE):
        try:
            with open(STATE) as f:
                state = json.load(f)
        except Exception:
            state = {}

    new_state = {}
    seen = set()
    for i, b in enumerate(buttons):
        key = "%s#%d" % (b["label"], i)
        seen.add(key)
        h = entry_hash(b, ORDER_BASE + i)
        prev = state.get(key)
        if prev and prev.get("hash") == h:
            new_state[key] = prev
            continue
        if prev and prev.get("uuid"):
            osa("delete_trigger (item 1 of argv)", prev["uuid"])
        rc, out = osa("add_new_trigger (item 1 of argv)",
                      json.dumps(trigger_json(b, ORDER_BASE + i, i),
                                 ensure_ascii=False))
        if rc == 0 and len(out) == 36:
            new_state[key] = {"uuid": out, "hash": h}
            print("added %s -> %s" % (b["label"], out))
        else:
            print("ERROR adding %s: %s" % (b["label"], out), file=sys.stderr)

    for key, prev in state.items():
        if key not in seen and prev.get("uuid"):
            osa("delete_trigger (item 1 of argv)", prev["uuid"])
            print("removed", key)

    # state の永続化は掃除より先。逆にすると掃除中に落ちたとき UUID を見失う
    with open(STATE, "w") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=1)

    # 印だけ残って state から消えた迷子（SCHEMA 変更や手動削除の跡）を回収する
    rc, out = osa("get_triggers")
    if rc == 0:
        try:
            known = {v["uuid"] for v in new_state.values()}
            for t in json.loads(out):
                if t.get("BTTNotes") == MARKER and t.get("BTTUUID") not in known:
                    osa("delete_trigger (item 1 of argv)", t["BTTUUID"])
                    print("removed orphan: %s" % t.get("BTTTouchBarButtonName"))
        except Exception:
            pass
    print("反映には BTT の再起動が必要な場合があります")
    return 0


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "send":
        # send は押すたびに走るのでロックを取らない（取り合いで落とすより
        # 素通りさせた方がよい。送信自体は冪等ではないが競合しない）
        return cmd_send(int(args[1]))
    if args[0] == "status":
        return cmd_status()
    if args[0] == "sync":
        lock = open(LOCK, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("sync already running, skip")
            return 0
        return cmd_sync()
    print("unknown command: %s" % args[0], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
