#!/usr/bin/env python3
"""commands.json を BTT の Touch Bar ボタンに同期する。

- commands.json のエントリごとにボタンを作成/更新/削除（差分はハッシュ比較）
- 状態は .cc-sync-state.json に command → {uuid, hash} で保持
- 変更は「削除して作り直し」で反映（BTT再起動不要のプレーンボタンのみ使用）
- cc-widget.sh が commands.json の mtime 変化を検知して自動起動する
"""
import fcntl
import hashlib
import json
import os
import subprocess
import sys

BASE = os.path.expanduser("~/.claude/btt")
sys.path.insert(0, BASE)
import cc_common as cc          # noqa: E402
CMDS = os.path.join(BASE, "commands.json")
STATE = os.path.join(BASE, ".cc-sync-state.json")
STAMP = os.path.join(BASE, ".commands.mtime")
LOCK = os.path.join(BASE, ".cc-sync.lock")

MARKER = "cc-touchbar-cmd"
MENU_MARKER = "cc-touchbar-menu"
GROUP_MARKER = "cc-touchbar-group"
# BTTOrder の割り当ては Touch Bar を共有する 3 プロジェクトの合意事項。
# 重複すると別プロジェクトのボタンが自分のボタンの間に割り込む(実際に起きた)。
#   0〜55    macenv (ステータス / 許可応答 / コマンド / 番号メニュー)
#   100〜    domdom-inspector
#   200〜    local-ui-builder
# 共有スロット方式に移行すればスロットは macenv が一元管理するので不要になるが、
# 移行が終わるまでは 3 者が独立に BTTOrder を振る期間が続く。
SCHEMA = 10  # trigger_json の構造を変えたらインクリメント(全ボタン再作成)
ORDER_BASE = 10
MENU_ORDER_BASE = 50   # コマンドボタンより後ろに並べる
PY = "/usr/bin/python3"
DEFAULT_COLOR = "58.000000, 58.000000, 60.000000, 255.000000"
RETURN_KEYCODE = "36"


def osa(script, *args):
    r = subprocess.run(
        ["/usr/bin/osascript", "-e",
         'on run argv\ntell application "BetterTouchTool" to ' + script + "\nend run",
         *args],
        capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or r.stderr).strip()


# ハッシュの basis には **実際に保存する値** を入れること。
# index だけを入れて order (ORDER_BASE + index) を入れていないと、
# ORDER_BASE を動かしたときにハッシュが変わらず作り直されない
# = 並び順だけを変えたつもりが古い並びのまま静かに残る。
def entry_hash(b, order, parent=""):
    # parent(グループのUUID)も basis に入れる。グループが作り直されて UUID が
    # 変わったのに子がそのままだと、子は消えたグループにぶら下がり続ける
    basis = json.dumps([b.get("label"), b.get("command"),
                        b.get("enter", False), b.get("color"),
                        bool(b.get("menu")), order, parent, SCHEMA],
                       ensure_ascii=False)
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def group_hash(name, order):
    return hashlib.sha1(("group:%s.%d.%d" % (name, order, SCHEMA))
                        .encode()).hexdigest()[:12]


def group_trigger_json(name, order):
    """フォルダ。押すと中身が開く BTT のグループ(type 630)。
    子は add_new_trigger の parent_uuid で結びつける。"""
    return {
        "BTTTouchBarButtonName": name,
        "BTTTriggerType": 630,
        "BTTTriggerTypeDescription": "Touch Bar group",
        "BTTTriggerClass": "BTTTriggerTypeTouchBar",
        "BTTNotes": GROUP_MARKER,
        "BTTEnabled": 1,
        "BTTEnabled2": 1,
        "BTTOrder": order,
        "BTTTriggerConfig": {
            "BTTTouchBarButtonName": name,
            "BTTTouchBarItemPlacement": 0,
            "BTTTouchBarButtonFontSize": 12,
            "BTTTouchBarButtonCornerRadius": 6,
            "BTTTouchBarFreeSpaceAfterButton": 4,
            "BTTTBWidgetWidth": widget_width(name),
            "BTTTouchBarButtonColor": DEFAULT_COLOR,
        },
    }


def menu_hash(n, order):
    return hashlib.sha1(("menu%d.%d.%d" % (n, order, SCHEMA))
                        .encode()).hexdigest()[:12]


def color_of(b):
    c = b.get("color")
    if not c:
        return DEFAULT_COLOR
    # "R,G,B,A" を BTT 形式 "R.000000, G.000000, ..." に整える
    try:
        parts = [float(x) for x in c.split(",")]
        return ", ".join("%f" % p for p in parts)
    except ValueError:
        return DEFAULT_COLOR


def widget_width(label):
    """ラベルから概算の幅(px)。CJK は 2 文字ぶんとして数える。
    細いボタンは Touch Bar だと物理的な当たり判定が無いぶん押しづらいので
    最小幅を持たせる（隣のボタンの誤爆にも直結する）。"""
    cells = sum(2 if ord(c) > 0x2E80 else 1 for c in label)
    return max(70, 22 + 9 * cells)


def trigger_json(b, order, index):
    """コマンドボタン。**642(シェルスクリプトウィジェット)であることが重要**。

    629 のプレーンボタンは表示条件を持てないため、ターミナル以外を操作して
    いるときも居座っていた（Touch Bar の幅を食う）。BTT のアプリ限定は
    AppleScript から設定できないことが実測で判明しているので、ウィジェット側で
    自前に隠すしかない。表示内容は cc-render.py が render/cmd-<index>.json に書く。

    なお **642 にしただけでは誤爆は消えない**。表示判定は毎秒なので、ボタンが
    見えている状態で切り替えられると押下が新しいフロントに飛ぶ（窓が常時から
    1秒に縮むだけ）。塞いでいるのは主アクションを 137 にして cc-menu.py 側で
    送信直前に対象を確認しているからで、642 化はその前提条件にすぎない。
    """
    name = b["label"]
    t = {
        "BTTWidgetName": name,
        "BTTTriggerType": 642,
        "BTTTriggerTypeDescription": name,
        "BTTTriggerClass": "BTTTriggerTypeTouchBar",
        # **グローバルな「カスタムテキストを入力」(193)にしないこと。**
        # 193 は押した時点でフロントにいるアプリへ打ち込むため、表示判定
        # (毎秒)と押下の間に切り替えられると別アプリに入る。さらに、印を
        # 付ける相手(pick_for_front が選ぶセッション)と文字が入る相手
        # (フロントのアプリ)が一致する保証が無く、「フォーカス中のタブは
        # ただのシェル」の場合に **コマンドはシェルへ、印は別セッションへ**
        # という2系統のズレが起きる。送信も印も cc-menu.py 側で一本化する
        "BTTPredefinedActionType": 137,
        "BTTTerminalCommand": '%s "$HOME/.claude/btt/cc-menu.py" run %d'
                              % (PY, index),
        "BTTNotes": MARKER,
        "BTTEnabled": 1,
        "BTTEnabled2": 1,
        "BTTOrder": order,
        # 実行設定は**トリガーのトップレベル**に置くこと。BTTTriggerConfig の
        # 中に入れるとスクリプトが一切実行されず、BTT を再起動しても直らない
        "BTTShellScriptWidgetGestureConfig": "/bin/bash:::-c",
        "BTTTriggerConfig": {
            "BTTTouchBarButtonName": name,
            "BTTTouchBarItemPlacement": 0,
            "BTTTouchBarButtonFontSize": 12,
            "BTTTouchBarButtonCornerRadius": 6,
            "BTTTouchBarFreeSpaceAfterButton": 4,
            "BTTTBWidgetWidth": widget_width(name),
            "BTTTouchBarButtonColor": color_of(b),
            "BTTTouchBarShellScriptString":
                '. "$HOME/.claude/btt/cc-widget.sh" cmd-%d' % index,
            "BTTTouchBarAlwaysShowButton": False,
            "BTTTouchBarScriptUpdateInterval": 1,
        },
    }
    # 追加アクションは持たない。押下記録・一拍の待ち・Return 送信・メニュー印は
    # すべて cc-menu.py run が行う（BTT のアクションでは条件分岐が書けず、
    # 「送ってよい状態か」を送信直前に確認できないため）
    return t


# 追加アクションに BTTTriggerClass を入れてはいけない。
# 保存はされるが**実行されず**、BTTのUIには
# "This action might be shown due to a database error. Please delete." と出る。
# 実測: BTTTriggerClass ありだと本体アクションしか走らない。外すと全部走る。


def menu_trigger_json(n, order):
    """番号ボタン。表示は cc-menu.py show が毎秒決め、押すと数字を送る。

    構造は実際に動いている許可ボタン(CC allow)に合わせてある。特に
    BTTShellScriptWidgetGestureConfig は **トップレベル** に置くこと。
    BTTTriggerConfig の中に入れるとスクリプトが一切実行されない
    (BTT を再起動しても動かず、原因が分かりにくい)。
    """
    name = "CC menu %d" % n
    return {
        "BTTWidgetName": name,
        "BTTTriggerType": 642,
        "BTTTriggerTypeDescription": name,
        "BTTTriggerClass": "BTTTriggerTypeTouchBar",
        # **グローバルな「カスタムテキストを入力」(193)にしないこと。**
        # 193 は押した時点でフロントにいるアプリへ数字を打ち込むため、
        # (a) メニューが既に閉じていればプロンプトに裸の数字が入り、
        # (b) 表示判定(毎秒)と押下の間に Cmd+Tab されると別アプリに入る。
        # cc-menu.py 側で送信直前に対象を再確認し、記録済みの iTerm2
        # セッションへ write text する(許可応答ボタンと同じ経路)
        "BTTPredefinedActionType": 137,
        "BTTTerminalCommand": '%s "$HOME/.claude/btt/cc-menu.py" send %d' % (PY, n),
        "BTTNotes": MENU_MARKER,
        "BTTEnabled": 1,
        "BTTEnabled2": 1,
        "BTTOrder": order,
        "BTTShellScriptWidgetGestureConfig": "/bin/bash:::-c",
        "BTTTriggerConfig": {
            "BTTTouchBarButtonName": name,
            "BTTTouchBarItemPlacement": 0,
            "BTTTouchBarButtonFontSize": 12,
            "BTTTouchBarButtonCornerRadius": 6,
            "BTTTouchBarFreeSpaceAfterButton": 4,
            "BTTTBWidgetWidth": 90,
            # source で読む。`/bin/bash script` だと bash が 2 プロセスになり
            # 実測で 3.7ms → 7.0ms に倍増する(毎秒 × ウィジェット数ぶん効く)
            "BTTTouchBarShellScriptString":
                '. "$HOME/.claude/btt/cc-widget.sh" menu-%d' % n,
            "BTTTouchBarAlwaysShowButton": False,
            "BTTTouchBarScriptUpdateInterval": 1,
        },
    }


def sweep_orphans(new_state):
    """自分の印を持つのに state に無いボタンを消す。

    state ファイルが失われたり、途中で失敗した回があると、BTT 側にだけ
    ボタンが残る（実際に SCHEMA 変更をまたいで古い「続けて」が残っていた）。
    印(BTTNotes)は自分が付けたものだけなので、他人のボタンは巻き込まない。
    """
    rc, out = osa("get_triggers")
    if rc != 0:
        # BTT がクラッシュ中だと -609(接続が無効) を返す。掃除は次回に回す
        print("! 掃除スキップ: get_triggers 失敗 (%s)" % out[:80])
        return
    try:
        triggers = json.loads(out)
    except Exception as e:
        # **黙って return しないこと。** get_triggers の出力が壊れることが
        # あり(他プロジェクトのシェル文字列に由来する制御文字/不正エスケープ)、
        # 握り潰すと迷子のボタンが溜まり続けるのに誰も気づかない
        print("! 掃除スキップ: get_triggers の出力が JSON として不正 (%s)" % e)
        return
    known = {v.get("uuid") for v in new_state.values()}
    for t in triggers:
        uuid = t.get("BTTUUID")
        if t.get("BTTNotes") in (MARKER, MENU_MARKER, GROUP_MARKER) and uuid not in known:
            osa("delete_trigger (item 1 of argv)", uuid)
            print("removed orphan: %s (%s)"
                  % (t.get("BTTTouchBarButtonName") or t.get("BTTWidgetName"),
                     uuid))


def main():
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("sync already running, skip")
        return

    mtime = os.path.getmtime(CMDS)
    # commands.json の解釈は cc_common に一本化する。描画側(cc-menu.py)と
    # 別々にフィルタしていると、条件が片方だけ変わったときにインデックスが
    # ずれて「別のボタンを描画する」形で静かに壊れる
    buttons = cc.command_buttons()

    state = {}
    if os.path.exists(STATE):
        try:
            with open(STATE) as f:
                state = json.load(f)
        except Exception:
            state = {}

    new_state = {}
    seen = set()

    # フォルダを先に作る。子は parent_uuid で結びつけるので UUID が要る。
    # 並び順は「そのグループの最初のボタンが現れた位置」に置く
    group_uuid = {}
    order = ORDER_BASE
    slot = {}          # ボタンindex -> BTTOrder（グループ内は連番を振り直す）
    seq = {}
    for i, b in enumerate(buttons):
        g = b.get("group")
        if not g:
            slot[i] = order
            order += 1
            continue
        if g not in group_uuid:
            key = "group#" + g
            seen.add(key)
            h = group_hash(g, order)
            prev = state.get(key)
            if prev and prev.get("hash") == h:
                group_uuid[g] = prev["uuid"]
                new_state[key] = prev
            else:
                if prev and prev.get("uuid"):
                    osa('delete_trigger (item 1 of argv)', prev["uuid"])
                rc, out = osa('add_new_trigger (item 1 of argv)',
                              json.dumps(group_trigger_json(g, order),
                                         ensure_ascii=False))
                if rc == 0 and len(out) == 36:
                    group_uuid[g] = out
                    new_state[key] = {"uuid": out, "hash": h}
                    print("added group %s -> %s" % (g, out))
                else:
                    print("ERROR adding group %s: %s" % (g, out))
            order += 1
        slot[i] = seq[g] = seq.get(g, 0) + 1

    for i, b in enumerate(buttons):
        key = b["command"] + "#" + str(i)
        seen.add(key)
        parent = group_uuid.get(b.get("group") or "", "")
        h = entry_hash(b, slot[i], parent)
        prev = state.get(key)
        if prev and prev.get("hash") == h:
            new_state[key] = prev
            continue
        if prev and prev.get("uuid"):
            osa('delete_trigger (item 1 of argv)', prev["uuid"])
        payload = json.dumps(trigger_json(b, slot[i], i), ensure_ascii=False)
        if parent:
            rc, out = osa('add_new_trigger (item 1 of argv) '
                          'parent_uuid (item 2 of argv)', payload, parent)
        else:
            rc, out = osa('add_new_trigger (item 1 of argv)', payload)
        if rc == 0 and len(out) == 36:
            new_state[key] = {"uuid": out, "hash": h}
            print("added %s -> %s" % (b["label"], out))
        else:
            print("ERROR adding %s: %s" % (b["label"], out))

    # 番号ボタン: 宣言されたメニューの最大長ぶんだけ用意する。
    # 中身(ラベル)は cc-menu.py が実行時に決めるので、ボタン自体は汎用でよい
    menu_n = cc.menu_slots()
    for n in range(1, menu_n + 1):
        key = "menu#%d" % n
        seen.add(key)
        h = menu_hash(n, MENU_ORDER_BASE + n)
        prev = state.get(key)
        if prev and prev.get("hash") == h:
            new_state[key] = prev
            continue
        if prev and prev.get("uuid"):
            osa('delete_trigger (item 1 of argv)', prev["uuid"])
        rc, out = osa('add_new_trigger (item 1 of argv)',
                      json.dumps(menu_trigger_json(n, MENU_ORDER_BASE + n),
                                 ensure_ascii=False))
        if rc == 0 and len(out) == 36:
            new_state[key] = {"uuid": out, "hash": h}
            print("added menu %d -> %s" % (n, out))
        else:
            print("ERROR adding menu %d: %s" % (n, out))

    for key, prev in state.items():
        if key not in seen and prev.get("uuid"):
            osa('delete_trigger (item 1 of argv)', prev["uuid"])
            print("removed", key)

    # state の永続化は掃除より**先**。順序を逆にすると、掃除で落ちたときに
    # 作成済みボタンの uuid を見失い、次回それらが丸ごと孤児になる
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(new_state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)

    sweep_orphans(new_state)
    # 成功時のみ mtime を記録（失敗時は次のtickで再試行される）
    with open(STAMP, "w") as f:
        f.write("%d" % mtime)
    print("sync done: %d buttons, %d menu slots" % (len(buttons), menu_n))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("sync failed:", e)
        sys.exit(1)
