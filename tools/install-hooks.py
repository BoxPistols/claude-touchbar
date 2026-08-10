#!/usr/bin/env python3
"""~/.claude/settings.json に claude-touchbar の hooks を**足す**。

**丸ごと置換はしない。** settings.json は他のツールやユーザー自身も書き込む
共有ファイルで、置換すると利用者が足した permissions.deny などが無警告で
消える(セキュリティ制御の無言解除)。ここでは「存在の保証」だけを行う:

  dict   … 再帰的にマージ
  list   … 和集合(同じコマンドの重複登録を作らない)
  scalar … 触らない

裏返しとして「このツール側から hooks を消しても利用者の設定からは消えない」。
アンインストールは --uninstall で明示的に行う。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser("~/.claude/settings.json")
HOOKS_SRC = os.path.join(ROOT, "hooks", "hooks.json")
# hooks.json はプラグイン用に ${CLAUDE_PLUGIN_ROOT} を使う。
# 手動インストールでは実際の配置先を指す必要がある
DEST = os.path.expanduser("~/.claude/btt")
MARK = "cc-hook.py"


def load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def merge(dst, src):
    """存在の保証。dst を壊さずに src の内容が「ある」状態にする。"""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list):
            for item in v:
                if item not in dst[k]:
                    dst[k].append(item)
        elif k not in dst:
            dst[k] = v
    return dst


def localized_hooks():
    src = load(HOOKS_SRC, {}).get("hooks", {})
    text = json.dumps(src, ensure_ascii=False)
    text = text.replace("${CLAUDE_PLUGIN_ROOT}/scripts", DEST)
    return json.loads(text)


def strip_ours(hooks):
    """このツールが入れた項目だけを取り除く(他ツールの hooks は残す)。"""
    out = {}
    for event, groups in hooks.items():
        kept = []
        for g in groups:
            hs = [h for h in g.get("hooks", [])
                  if MARK not in str(h.get("command", ""))]
            if hs:
                kept.append({**g, "hooks": hs})
            elif "hooks" not in g:
                kept.append(g)
        if kept:
            out[event] = kept
    return out


def write(settings):
    os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, SETTINGS)


def main():
    uninstall = "--uninstall" in sys.argv[1:]
    settings = load(SETTINGS, {})
    if not isinstance(settings, dict):
        print("settings.json の形が想定外です。中止")
        return 1

    if uninstall:
        settings["hooks"] = strip_ours(settings.get("hooks", {}))
        write(settings)
        print("hooks から claude-touchbar の項目を外しました")
        return 0

    ours = localized_hooks()
    if not ours:
        print("hooks/hooks.json が読めません")
        return 1
    settings.setdefault("hooks", {})
    merge(settings["hooks"], ours)
    write(settings)
    print("✔ hooks を settings.json に追加(既存の設定は保持)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
