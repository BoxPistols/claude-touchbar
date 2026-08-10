#!/usr/bin/env python3
"""BTT にアプリ別の「Show App Default Touch Bar」を投入する。

native-apps.json に列挙したアプリが前面のとき、BTT の Touch Bar を出さず
アプリ自身(macOS ネイティブ)の Touch Bar を通す。アプリが自前の NSTouchBar を
持つ場合(The Boosters 等)はそのボタン列が、持たない場合は空きバー + Control
Strip が出る。

仕組み(全て実測。詳細は DESIGN.md §17):
- BTT のアプリ別設定 TouchBarBehavior は AppleScript API から直接設定できない。
  唯一の投入経路は .bttpreset の import(アプリコンテナの BTTAppSpecificSettings)
- JSON キーは BTTTouchBarMode。値 3 が「Show App Default Touch Bar」
  (UI バインディング名 TouchBarBehavior で書くと import 時に黙って破棄される)
- 保存先は data store の AppSpecificSettings エンティティ(ZTOUCHBARMODE1)。
  export には出てこないため、設定済みかの確認は sqlite の読み取りで行う
- import は確認ダイアログを 1 回出す(ユーザーが Import をクリック)
- 反映には BTT の再起動が必要

冪等: 設定済み(ZTOUCHBARMODE1=3)のアプリはスキップ。DB が読めないときは
import しない(重複 preset を作らないため。同名 preset はマージされず増える)。
"""
import glob
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

BASE = os.path.dirname(os.path.abspath(__file__))
CONF = os.path.join(BASE, "native-apps.json")
MODE_APP_DEFAULT = 3
WAIT_DIALOG_SEC = 90


def osa(script, *args):
    cmd = ["/usr/bin/osascript", "-e",
           'on run argv\ntell application "BetterTouchTool" to ' + script + "\nend run"]
    cmd.extend(a for a in args if a is not None)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or r.stderr).strip()


def store_path():
    pat = os.path.expanduser(
        "~/Library/Application Support/BetterTouchTool/btt_data_store.version_*")
    cands = [p for p in glob.glob(pat) if not p.endswith(("-wal", "-shm"))]
    return max(cands, key=os.path.getmtime) if cands else None


def mode_of(db, bundle):
    """None=読めない(判定不能) / -1=行なし / それ以外=ZTOUCHBARMODE1 の最大値。
    重複 preset で複数行あり得るので「3 が 1 行でもあれば設定済み」と読む。"""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
        rows = con.execute(
            "SELECT s.ZTOUCHBARMODE1 FROM ZBTTBASEENTITY a "
            "JOIN ZBTTBASEENTITY s ON s.ZBELONGSTOAPP=a.Z_PK AND s.Z_ENT=3 "
            "WHERE a.ZBUNDLEIDENTIFIER=?", (bundle,)).fetchall()
        con.close()
    except sqlite3.Error:
        return None
    vals = [r[0] for r in rows if r[0] is not None]
    return max(vals) if vals else -1


def unique_preset_name():
    base = "macenv-nativebar"
    rc, out = osa("get_preset_details")
    names = set()
    if rc == 0:
        try:
            names = {p.get("name") for p in json.loads(out)}
        except ValueError:
            pass
    name, n = base, 1
    while name in names:
        n += 1
        name = "%s-%d" % (base, n)
    return name


def main():
    if sys.platform != "darwin":
        return 0
    try:
        with open(CONF) as f:
            apps = json.load(f).get("apps", [])
    except (OSError, ValueError) as e:
        print("native-apps.json が読めません:", e)
        return 0
    if not apps:
        return 0

    db = store_path()
    if not db:
        print("BTT の data store が見つかりません。BTT 未導入ならスキップで正常")
        return 0

    missing = []
    for a in apps:
        m = mode_of(db, a["bundle_id"])
        if m == MODE_APP_DEFAULT:
            print("skip(設定済み): %s" % a["bundle_id"])
        elif m is None:
            print("! DB が読めないため %s は判定不能。import は行いません" % a["bundle_id"])
        else:
            missing.append(a)
    if not missing:
        return 0

    if subprocess.run(["/usr/bin/pgrep", "-x", "BetterTouchTool"],
                      capture_output=True).returncode != 0:
        print("! BTT が起動していません。起動後に再実行してください:")
        print("    python3 ~/.claude/btt/cc-nativebar.py")
        return 0

    preset = {
        "BTTPresetName": unique_preset_name(),
        "BTTPresetUUID": str(uuid.uuid4()).upper(),
        "BTTPresetContent": [
            {
                "BTTAppBundleIdentifier": a["bundle_id"],
                "BTTAppName": a.get("name", a["bundle_id"]),
                "BTTAppSpecificSettings": {"BTTTouchBarMode": MODE_APP_DEFAULT},
                "BTTTriggers": [],
            }
            for a in missing
        ],
    }
    # BTT は import 元パスをダイアログに表示する。分かりやすい名前で置く
    tmp = os.path.join(tempfile.gettempdir(), preset["BTTPresetName"] + ".bttpreset")
    with open(tmp, "w") as f:
        json.dump(preset, f, ensure_ascii=False)

    rc, out = osa("import_preset (item 1 of argv)", tmp)
    if rc != 0:
        print("ERROR: import_preset 失敗:", out)
        return 0
    print("BTT が確認ダイアログを出しています。**Import をクリックしてください**")
    print("(対象: %s)" % ", ".join(a["bundle_id"] for a in missing))

    deadline = time.time() + WAIT_DIALOG_SEC
    pending = {a["bundle_id"] for a in missing}
    while pending and time.time() < deadline:
        time.sleep(2)
        pending = {b for b in pending if mode_of(db, b) != MODE_APP_DEFAULT}
    try:
        os.remove(tmp)
    except OSError:
        pass

    if pending:
        print("! 未反映のまま終了: %s" % ", ".join(sorted(pending)))
        print("  ダイアログをキャンセルした場合は再実行してください")
    else:
        print("投入完了。※ 反映には BTT の再起動が必要です")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # chezmoi apply を止めない
        print("cc-nativebar failed:", e)
        sys.exit(0)
