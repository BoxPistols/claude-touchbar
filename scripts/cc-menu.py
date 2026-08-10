#!/usr/bin/env python3
"""コマンドボタン／番号選択メニューの表示と送信。

  cc-menu.py run <i>     i番目のコマンドボタンを実行（押下時に BTT から呼ばれる）
  cc-menu.py send <n>    n番の選択肢を送る（番号ボタンの押下）
  cc-menu.py show <n>    n番ボタンの表示内容を出す（デバッグ用。通常は
                         cc-render.py が render_show を直接呼ぶ）
  cc-menu.py close       印を消す

**送信は必ずセッションへの直接書き込みで行う。** BTT の「カスタムテキストを
入力」(193)はフロントにいるアプリへ打ち込むため、(a) 対象が変わっていれば
別アプリ・別セッションに入り、(b) 表示判定(毎秒)と押下の間のレースを塞げない。
文字を送る相手と印を付ける相手は cc_common.target_session() で一本化する。

番号メニューの検出方式:
  Claude Code の hooks はセッション/ツールのライフサイクルにしか発火せず、
  「TUI にメニューが出た」を知る手段が無い。画面の読み取り(iTerm2 Python API)は
  可能だが、APIの有効化が必要で表示書式の変更に弱い。
  そこで「メニューを開いたボタン自身は、何のメニューを開いたか知っている」
  という事実を使い、選択肢を commands.json に宣言しておく。
  割り切り: キーボードで /model と打った場合には出ない。
"""
import json
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import cc_common as cc          # noqa: E402

MARK = os.path.join(BASE, "menu.json")
PRESS = os.path.join(BASE, "press.json")
LOG = os.path.join(BASE, "menu.log")

# メニューは開いてすぐ選ぶもの。放置された印でボタンが出続けないよう短めに切る
TTL_SEC = 45

# 押下フィードバックを出す時間。Return までの一拍より少し長くする
PRESS_FEEDBACK_SEC = 0.6

# 入力→Returnの間に置く一拍。スラッシュメニューの絞り込みが追いつく前に
# 確定して別コマンドが選ばれる誤爆を防ぐ
ENTER_DELAY_SEC = 0.35

TERMINALS = ("com.googlecode.iterm2", "com.apple.Terminal")

DEFAULT_COLOR = "58,58,60,255"

HIDDEN = {"text": "", "hidden": True}

LOG_MAX = 64 * 1024


def log(msg):
    """押下の結果を残す。

    ボタンは BTT の 137(非同期ターミナルコマンド)から呼ばれるため、
    **stdout はどこにも表示されない**。「誤爆より無反応を選ぶ」設計にした以上、
    無反応の理由が残らないとユーザーには「壊れている」としか見えない。
    """
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.remove(LOG)
        with open(LOG, "a") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except OSError:
        pass
    print(msg)


def write_json(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


OSA = '''
on run argv
  set theGuid to item 1 of argv
  set payload to item 2 of argv
  set doEnter to (item 3 of argv) is "1"
  set waitFor to (item 4 of argv) as real
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if id of s is theGuid then
            tell s to write text payload newline NO
            if doEnter then
              delay waitFor
              tell s to write text (character id 13) newline NO
            end if
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
  return "notfound"
end run
'''


def send_to(guid, payload, enter=False):
    """iTerm2 のセッションへ直接書き込む。どのアプリがフロントでも影響しない。"""
    r = subprocess.run(
        ["/usr/bin/osascript", "-e", OSA, guid, payload,
         "1" if enter else "0", str(ENTER_DELAY_SEC)],
        capture_output=True, text=True, timeout=20)
    return (r.stdout or r.stderr).strip()


def mark_pressed(label):
    """押下フィードバック用の印。入力から Return までの間が無反応だと
    二度押しされ、`/usage/usage` のように 2 回入力される（実際に起きた）。"""
    try:
        write_json(PRESS, {"label": label, "at": time.time()})
    except OSError:
        pass


def pressed_label():
    try:
        with open(PRESS) as f:
            p = json.load(f)
    except Exception:
        return None
    if time.time() - p.get("at", 0) > PRESS_FEEDBACK_SEC:
        return None
    return p.get("label")


def find_session(sessions, guid):
    for r in sessions:
        if r.get("term_guid") == guid:
            return r
    return None


def load_marker(sessions=None):
    """有効なメニュー印。無効なら None。

    失効条件は「印を書いたあとに、そのセッションで**実際に何かが起きたか**」。
    状態(busy/idle)の分類はしない — メニューを開いた後に一往復して idle に
    戻っていると「今 busy か」では素通りするため。判定には transcript の
    (size, mtime) を使う。`updated_at` は hook のたびに動き、60秒アイドルの
    Notification でも動いてしまうので使えない（メニューが開いたままなのに
    番号ボタンが消える）。
    """
    try:
        with open(MARK) as f:
            m = json.load(f)
    except Exception:
        return None
    if time.time() - m.get("at", 0) > TTL_SEC:
        return None

    guid = m.get("term_guid")
    if not guid:
        return None
    if sessions is None:
        sessions = cc.load_sessions()
    rec = find_session(sessions, guid)
    if rec is None:
        return None      # 送り先が消えた

    if m.get("tr") is not None and cc.transcript_stamp(rec) != m["tr"]:
        return None      # 印のあとに実際のやり取りがあった＝メニューは閉じている
    # transcript が取れないセッション向けの二段目
    if rec.get("state") == "busy" and rec.get("updated_at", 0) > m.get("at", 0):
        return None
    return m


def permission_marker(sessions=None, fb=None):
    """許可プロンプト由来の「開いているメニュー」。無ければ None。

    印(menu.json)は「コマンドボタンが何を開いたか申告する」宣言だが、こちらは
    **CLI 自身が PermissionRequest で出した選択肢**なので、宣言より確かな情報源。
    TTL も transcript 照合も要らない — セッション状態が `waiting/permission` で
    あること自体が「いまプロンプトが出ている」を意味し、答えれば PostToolUse が
    busy に戻すため、印のように放置で腐らない。

    3つ以下は色分けできる専用ボタン(cc-btn.py)が担当する。ここが引き受けるのは
    4つ以上、つまり AskUserQuestion のように選択肢が任意の N 択になる場合。
    """
    rec = cc.permission_session(sessions, fb)
    if rec is None:
        return None
    labels = cc.permission_labels(rec)
    if len(labels) < 4:
        return None
    return {"labels": labels[:cc.menu_slots()], "term_guid": rec["term_guid"],
            "session_id": rec.get("session_id"), "from": rec.get("tool") or "?",
            "source": "permission"}


def active_menu(sessions=None, fb=None):
    """いま番号ボタンが表すべきメニュー。コマンド由来の印を優先する。"""
    m = load_marker(sessions)
    if m:
        return m
    return permission_marker(sessions, fb)


def do_run(i):
    """コマンドボタンの押下。文字列を対象セッションへ直接送る。"""
    buttons = cc.command_buttons()
    if i < 0 or i >= len(buttons):
        log("run %d: out of range (%d buttons)" % (i, len(buttons)))
        return
    b = buttons[i]
    mark_pressed(b["label"])     # 送信の成否によらず、押されたことは即座に見せる

    sessions = cc.load_sessions()
    # **send_target。target_session ではない。** 後者は表示用で「最も注意すべき
    # セッション」を返すため、送信に使うとユーザーが見ていないタブに文字が飛ぶ
    # (実際に別プロジェクトのセッションへ /review が入った)
    rec = cc.send_target(sessions)
    if rec is None:
        log("run %s: focused session not resolved; nothing sent" % b["label"])
        return

    guid = rec["term_guid"]
    # enter の既定は **false**（入力するだけ、実行はユーザーがEnterを押す）。
    # Touch Bar は目視せず触れる場所なので、押した瞬間に確定実行されるのは
    # 驚きが大きい。とくに /clear や /review のように取り消せない・重いものが
    # 無反応の直後に突然走ると「勝手に submit された」体験になる。
    # ピッカーを開くだけのもの(/model, /effort)や読み取り専用(/usage)だけ
    # 明示的に true にする
    out = send_to(guid, b["command"], enter=b.get("enter", False))
    log("run %s -> %s (%s)" % (b["label"], guid[:8], out))
    if out != "ok":
        return

    labels = b.get("menu") or []
    if not labels:
        return
    # 印は**送信のあと**に書く。コマンド自体が transcript を動かす場合に、
    # 直後の判定で自分の書き込みを「活動」と見て自滅しないようにするため
    write_json(MARK, {"labels": labels, "at": time.time(), "from": b["label"],
                      "term_guid": guid, "session_id": rec.get("session_id"),
                      "tr": cc.transcript_stamp(rec)})


def do_send(n):
    """番号ボタンの押下。選択肢の番号を対象セッションへ直接送る。"""
    sessions = cc.load_sessions()
    m = active_menu(sessions)
    if not m:
        log("send %d: marker invalid; nothing sent" % n)
        return
    labels = m.get("labels") or []
    if n < 1 or n > len(labels):
        log("send %d: out of range (%d labels)" % (n, len(labels)))
        return
    guid = m["term_guid"]
    # 印を書いたあとにタブを移っていたら、そのメニューはもう見えていない。
    # 表示判定と押下の間に切り替えられる隙は実機で普通に起きる
    focused = cc.focused_iterm_guid()
    if focused != guid:
        log("send %d: focus moved (%s -> %s); nothing sent"
            % (n, guid[:8], (focused or "none")[:8]))
        do_close()
        return
    try:
        out = send_to(guid, str(n))
        log("send %d (%s) -> %s (%s)" % (n, labels[n - 1], guid[:8], out))
    finally:
        # 送信が失敗しても印は消す。残すと次の押下で同じ失敗を繰り返す
        do_close()


def render_cmd(i, fb=None, buttons=None, pressed=None, target=None):
    """コマンドボタン1個ぶんの出力 dict。

    629 のプレーンボタンだった頃は表示条件を持てず、ターミナル以外を操作して
    いるときも居座っていた。642 ウィジェットにして自前で隠す。
    **送り先が解決できないときも隠す** — フロントがターミナルでも、Claude が
    動いていないタブなら押しても意味が無く、「押せるのに何も起きない」は
    「出ない」より悪い（ユーザーは壊れたと判断する）。
    """
    if buttons is None:
        buttons = cc.command_buttons()
    if i < 0 or i >= len(buttons):
        return dict(HIDDEN)
    if (fb if fb is not None else cc.front_bundle()) not in TERMINALS:
        return dict(HIDDEN)
    t = target if target is not None else cc.target_session()
    if t is None:
        return dict(HIDDEN)
    # 許可プロンプトが出ている間は隠す。(a) 応答ボタン／番号ボタンに幅を譲る
    # （Touch Bar は既に右端が切れており、増やす前に減らす必要がある）
    # (b) プロンプトに答える前に /clear のような取り消せないものを押す事故を防ぐ
    if t.get("state") == "waiting" and t.get("waiting_kind") == "permission":
        return dict(HIDDEN)

    b = buttons[i]
    color = b.get("color") or DEFAULT_COLOR
    if (pressed if pressed is not None else pressed_label()) == b.get("label"):
        color = brighten(color)
    return {"text": b["label"], "background_color": color,
            "font_color": "255,255,255,255", "hidden": False}


def render_show(n, fb=None, sessions=None):
    """番号ボタン1個ぶんの出力 dict。

    load_marker() が送り先の生存まで見ているので、ここを通れば押したときに
    必ず送れる（「出ているのに無反応」を作らない）。
    """
    fb = fb if fb is not None else cc.front_bundle()
    if fb not in TERMINALS:
        return dict(HIDDEN)
    m = active_menu(sessions, fb)
    if not m:
        return dict(HIDDEN)
    labels = m.get("labels") or []
    if n < 1 or n > len(labels):
        return dict(HIDDEN)
    # 由来で色を分ける。コマンドで開いたメニューは「自分が開いたもの」、
    # 許可プロンプト由来は「CLI が答えを待っているもの」で緊急度が違う
    perm = m.get("source") == "permission"
    bg = "48,64,88,255" if perm else "72,72,78,255"
    # 許可プロンプト由来はコマンドボタンが隠れているぶん広く使える
    budget = cc.PERM_MENU_LABEL_CELLS if perm else cc.MENU_LABEL_CELLS
    prefix = "%d " % n
    return {"text": prefix + cc.clip(labels[n - 1],
                                     cc.label_cells(budget, prefix)),
            "background_color": bg,
            "font_color": "255,255,255,255",
            "hidden": False}


def brighten(color, factor=1.9):
    """押下フィードバック用に明るくする。解釈できない色はそのまま返す。"""
    try:
        r, g, b, a = [int(float(x)) for x in color.split(",")]
    except Exception:
        return color
    return "%d,%d,%d,%d" % (min(255, int(r * factor)), min(255, int(g * factor)),
                            min(255, int(b * factor)), a)


def do_close():
    try:
        os.remove(MARK)
    except OSError:
        pass


def main():
    argv = sys.argv[1:]
    cmd = argv[0] if argv else ""
    if cmd == "run" and len(argv) > 1:
        do_run(int(argv[1]))
    elif cmd == "send" and len(argv) > 1:
        do_send(int(argv[1]))
    elif cmd == "show" and len(argv) > 1:
        print(json.dumps(render_show(int(argv[1])), ensure_ascii=False))
    elif cmd == "close":
        do_close()
    else:
        print(json.dumps(HIDDEN))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # ウィジェット経路(show)は何があっても JSON を返す。
        # 操作経路(run/send)は握り潰さずログに残す — 137 の stdout は
        # 誰にも見えないので、ここで消すと失敗が完全に不可視になる
        if len(sys.argv) > 1 and sys.argv[1] == "show":
            print(json.dumps(HIDDEN))
        else:
            log("%s failed: %s" % (" ".join(sys.argv[1:]) or "?", e))
