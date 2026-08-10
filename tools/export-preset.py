#!/usr/bin/env python3
"""稼働中の BTT から、このツールが所有するトリガーだけを .bttpreset に書き出す。

`export_preset` は**プリセット丸ごと**を出すので、利用者個人のトリガーまで
含んでしまう(公開リポジトリに置くには不適)。ここでは `BTTNotes` の印
(cc-touchbar-*) が付いたものだけを拾い、配布用のプリセットを組み立てる。

  python3 tools/export-preset.py [出力先]

既定の出力先は presets/claude-touchbar.bttpreset。
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(ROOT, "presets", "claude-touchbar.bttpreset")
# 配布するのはコアウィジェット(ステータス + 許可応答3種)のみ。
# コマンド/番号ボタンは cc-sync.py が commands.json から生成するので、
# プリセットに焼き込むと二重に作られる
OWNED_PREFIX = ("cc-touchbar-core", "cc-touchbar-perm")
HOME = os.path.expanduser("~")

# get_triggers にだけ現れるメタデータ。add_new_trigger / import では使わない
STRIP_KEYS = {"BTTUUID", "BTTLastUpdatedAt", "BTTTriggerBelongsToPreset",
              "BTTTriggerParentUUID"}


def osa(script):
    r = subprocess.run(
        ["/usr/bin/osascript", "-e",
         'tell application "BetterTouchTool" to ' + script],
        capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or r.stderr).strip()


def walk(obj, fn):
    if isinstance(obj, dict):
        return {k: walk(v, fn) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(v, fn) for v in obj]
    if isinstance(obj, str):
        return fn(obj)
    return obj


def sanitize(t):
    out = {k: v for k, v in t.items() if k not in STRIP_KEYS}
    if "BTTActionsToExecute" in out:
        acts = out.pop("BTTActionsToExecute")
        for i, a in enumerate(acts):
            for k in list(a.keys()):
                if k in STRIP_KEYS:
                    del a[k]
            a.setdefault("BTTOrder", i)
        out["BTTAdditionalActions"] = acts
    # 実ホームパスは配布物に入れない。取り込み側で展開する
    return walk(out, lambda s: s.replace(HOME, "$HOME"))


def main():
    out_path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_OUT
    rc, out = osa("get_triggers")
    if rc != 0:
        print("BTT に接続できません(未起動?):", out)
        return 1
    try:
        triggers = json.loads(out)
    except ValueError as e:
        print("get_triggers の JSON が壊れています:", e)
        return 1

    owned = [t for t in triggers
             if (t.get("BTTNotes") or "").startswith(OWNED_PREFIX)
             and t.get("BTTTriggerType") is not None]
    if not owned:
        print("印 %s の付いたトリガーが見つかりません" % (OWNED_PREFIX,))
        return 1
    owned.sort(key=lambda t: t.get("BTTOrder", 0))

    preset = {
        "BTTPresetName": "claude-touchbar",
        "BTTPresetUUID": "5F0E1D2C-3B4A-4959-8687-75645342310F",
        "BTTPresetContent": [
            {"BTTAppBundleIdentifier": "BT.G", "BTTAppName": "Global",
             "BTTTriggers": [sanitize(t) for t in owned]}
        ],
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(preset, f, ensure_ascii=False, indent=1)
    print("exported %d triggers -> %s" % (len(owned), out_path))

    leaked = [l for l in json.dumps(preset, ensure_ascii=False).split('"')
              if l.startswith("/Users/") or l.startswith("/home/")]
    if leaked:
        print("! 実パスが残っています:", leaked[:3])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
