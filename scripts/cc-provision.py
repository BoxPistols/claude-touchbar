#!/usr/bin/env python3
"""BTT の Claude Code コアウィジェットを core-widgets.json と往復させる。

  cc-provision.py            復元(新規マシン用)。既存の同名ウィジェットはスキップ(冪等)
  cc-provision.py --export   現在の BTT の状態を core-widgets.json へ書き出す
  cc-provision.py --update   既存ウィジェットを core-widgets.json の内容で上書きする
                             (スクリプトの呼び出し方を変えたときなど。復元はスキップ
                              するだけなので、稼働中のマシンにはこちらが必要)

対象はコアウィジェット4種(ステータス表示 / 許可 / 常に許可 / 中断)のみ。
コマンドボタンは cc-sync.py が commands.json から自動生成するため、ここでは扱わない
(両方で作ると cc-sync の state に無いボタンが孤児として残る)。

移植性: ホームパスは JSON 内では "$HOME/..." で保持し、復元時に実パスへ展開する。
        ユーザー名の違うマシンでもそのまま動く。

注意: Shell Script Widget は投入後 BTT を再起動しないと実行が始まらない
      (BTTShellScriptWidgetGestureConfig が反映されない既知の挙動)。
"""
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.join(BASE, "core-widgets.json")
HOME = os.path.expanduser("~")

# 復元対象の識別: (ウィジェット名, トリガー種別)
CORE_NAMES = {"Claude Code", "CC allow", "CC always", "CC reject"}

# get_triggers にだけ現れるメタデータ。add_new_trigger では使えないので落とす
STRIP_KEYS = {"BTTUUID", "BTTLastUpdatedAt",
              "BTTTriggerBelongsToPreset", "BTTTriggerParentUUID"}


def osa(script, *args):
    cmd = ["/usr/bin/osascript", "-e",
           'on run argv\ntell application "BetterTouchTool" to ' + script + "\nend run"]
    cmd.extend(a for a in args if a is not None)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or r.stderr).strip()


def widget_name(t):
    return t.get("BTTTouchBarButtonName") or t.get("BTTWidgetName") or ""


def walk_strings(obj, fn):
    """入れ子の dict/list を辿って文字列だけ変換する"""
    if isinstance(obj, dict):
        return {k: walk_strings(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk_strings(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def portable(t):
    """実ホームパス → "$HOME"(保存用)"""
    return walk_strings(t, lambda s: s.replace(HOME, "$HOME"))


def localize(t):
    """"$HOME" → 実ホームパス(復元用)"""
    return walk_strings(t, lambda s: s.replace("$HOME", HOME))


def sanitize(t):
    out = {k: v for k, v in t.items() if k not in STRIP_KEYS}
    # get_triggers は追加アクションを BTTActionsToExecute で返すが、
    # add_new_trigger が受け取るのは BTTAdditionalActions。ここで変換する
    if "BTTActionsToExecute" in out:
        acts = out.pop("BTTActionsToExecute")
        for i, a in enumerate(acts):
            for k in list(a.keys()):
                if k in STRIP_KEYS:
                    del a[k]
            # BTTOrder を省略すると逆順で保存される(BTTの既知の挙動)
            a.setdefault("BTTOrder", i)
        out["BTTAdditionalActions"] = acts
    return out


def load_triggers():
    rc, out = osa("get_triggers")
    if rc != 0:
        return None
    try:
        return json.loads(out)
    except Exception:
        return None


def do_export():
    triggers = load_triggers()
    if triggers is None:
        print("BTT に接続できません(未起動?)")
        return 1

    core = [portable(sanitize(t)) for t in triggers
            if widget_name(t) in CORE_NAMES]
    core.sort(key=lambda t: t.get("BTTOrder", 0))

    if len(core) != len(CORE_NAMES):
        found = sorted(widget_name(t) for t in core)
        print("! 期待 %d 件に対し %d 件しか見つかりません: %s"
              % (len(CORE_NAMES), len(core), found))
        print("  BTT 側が意図した状態か確認してください(書き出しは中止)")
        return 1

    tmp = CORE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(core, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CORE)
    print("exported %d widgets -> %s" % (len(core), CORE))
    return 0


def do_update():
    """既存のコアウィジェットを core-widgets.json の内容で上書きする。
    do_restore() は「無ければ作る」しかしないので、稼働中のマシンで
    スクリプトの呼び出し方を変えたときはこちらを使う。"""
    if not os.path.exists(CORE):
        print("core-widgets.json が見つかりません。スキップ")
        return 0

    triggers = load_triggers()
    if triggers is None:
        print("BTT に接続できません(未起動?)")
        return 1
    live = {(widget_name(t), t.get("BTTTriggerType")): t.get("BTTUUID")
            for t in triggers}

    with open(CORE) as f:
        widgets = json.load(f)

    updated = missing = 0
    for w in widgets:
        key = (widget_name(w), w.get("BTTTriggerType"))
        uuid = live.get(key)
        if not uuid:
            print("! 未作成のためスキップ: %s (先に引数なしで実行)" % key[0])
            missing += 1
            continue
        rc, out = osa("update_trigger (item 1 of argv) json (item 2 of argv)",
                      uuid, json.dumps(localize(w), ensure_ascii=False))
        if rc == 0:
            print("updated: %s" % key[0])
            updated += 1
        else:
            print("ERROR updating %s: %s" % (key[0], out))

    if updated:
        print("※ Shell Script Widget の変更を反映するには BTT の再起動が必要です")
    return 1 if missing else 0


def do_restore():
    if not os.path.exists(CORE):
        print("core-widgets.json が見つかりません。スキップ")
        return 0

    triggers = load_triggers()
    if triggers is None:
        print("BTT に接続できません(未起動?)。BTT を起動してから再実行してください")
        return 0
    existing = {(widget_name(t), t.get("BTTTriggerType")) for t in triggers}

    with open(CORE) as f:
        widgets = json.load(f)

    created = 0
    for w in widgets:
        key = (widget_name(w), w.get("BTTTriggerType"))
        if key in existing:
            print("skip(既存): %s" % key[0])
            continue
        rc, out = osa("add_new_trigger (item 1 of argv)",
                      json.dumps(localize(w), ensure_ascii=False))
        if rc == 0 and len(out) == 36:
            print("created: %s -> %s" % (key[0], out))
            created += 1
        else:
            print("ERROR creating %s: %s" % (key[0], out))

    if created:
        print("※ Shell Script Widget を有効にするため BTT の再起動が必要です")
    return 0


if __name__ == "__main__":
    try:
        if "--export" in sys.argv[1:]:
            sys.exit(do_export())
        if "--update" in sys.argv[1:]:
            sys.exit(do_update())
        sys.exit(do_restore())
    except Exception as e:
        print("provision failed:", e)
        # chezmoi apply / menv save を止めない
        sys.exit(1 if "--export" in sys.argv[1:] else 0)
