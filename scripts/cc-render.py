#!/usr/bin/env python3
"""Touch Bar ウィジェットの表示内容をまとめて計算する常駐プロセス。

なぜ常駐させるか（実測）:
  9 個のウィジェットがそれぞれ毎秒 python3 を起動していたとき、消費は
  576ms/s = コア1個の 58% だった。内訳の 66% は「インタプリタが起動する
  だけ」で表示内容の計算ではない:

    python3 -c pass          47.5 ms   ← これ × 8 プロセス = 380 ms/s
    lsappinfo × 2            16.9 ms   ← front_bundle() の中身
    cc-widget.sh (実物)     103.6 ms
    cc-btn.py    (実物)      84.6 ms
    cc-menu.py   (実物)      43.8 ms
    cat          (参考)       7.4 ms

  そこで計算はこのプロセスに1本化し、各ウィジェットは cc-widget.sh が
  組み込みコマンドで render/<name>.json を読むだけにする。
  front_bundle() と load_sessions() もティックあたり1回で済むようになる
  （以前はウィジェットごとに独立して実行していた）。

生存管理:
  launchd は使わず、ステータスウィジェットが「落ちていたら起動し直す」方式
  （cc-widget.sh）。二重起動は flock で弾く。逆に BTT 側が読まなくなったら
  （BTT 終了・ウィジェット削除）自分で終了する — 誰も見ていないプロセスが
  バッテリーを食い続けるのを防ぐため。
"""
import fcntl
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

RENDER = os.path.join(BASE, "render")
PIDFILE = os.path.join(RENDER, "daemon.pid")
SEEN = os.path.join(RENDER, ".seen")
LOCK = os.path.join(BASE, ".cc-render.lock")
CMDS = os.path.join(BASE, "commands.json")
STAMP = os.path.join(BASE, ".commands.mtime")

TICK = 1.0
# 読み手が止まってからここまで待って終了する。BTT の再起動を挟んでも
# ウィジェットが読み始めれば .seen が更新されるので誤って落ちることはない
IDLE_EXIT_SEC = 15
# 内容が変わらなくてもたまに書き直す（外部から消された場合の復旧）
FORCE_WRITE_EVERY = 30

HIDDEN = {"text": "", "hidden": True}


def load(name):
    """ハイフン入りのファイル名は普通の import ができないので明示的に読む。"""
    path = os.path.join(BASE, name)
    spec = importlib.util.spec_from_file_location(
        name.replace("-", "_")[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


import cc_common as cc          # noqa: E402

status = load("cc-status.py")
btn = load("cc-btn.py")
menu = load("cc-menu.py")


def render_all():
    """1ティックぶんの全ウィジェット出力。front_bundle と load_sessions は
    ここで1回だけ実行し、各描画関数へ渡す（これがコスト削減の本体）。"""
    fb = cc.front_bundle()
    sessions = cc.load_sessions()

    out = {"status": status.render(sessions, fb)}
    for kind in ("allow", "always", "reject"):
        out["perm-" + kind] = btn.render(kind, sessions, fb)

    buttons = cc.command_buttons()
    # "" は「押下なし」。None を渡すと render_cmd 側が自分で読み直してしまう
    pressed = menu.pressed_label() or ""
    target = cc.target_session(sessions, fb)
    # False は「メニュー無し」。None は「未指定」で render_cmd が引き直す
    menu_open = menu.active_menu(sessions, fb) or False
    if target is None:
        # 送り先が無いならコマンドボタンは全部隠す。render_cmd に None を
        # 渡すと「未指定」と区別できず、ウィジェットごとに引き直してしまう
        for i in range(len(buttons)):
            out["cmd-%d" % i] = dict(menu.HIDDEN)
    else:
        for i in range(len(buttons)):
            out["cmd-%d" % i] = menu.render_cmd(i, fb, buttons, pressed,
                                                target, menu_open)
    for n in range(1, cc.menu_slots() + 1):
        out["menu-%d" % n] = menu.render_show(n, fb, sessions)
    return out


def write(name, data, cache, force):
    """変わった時だけ書く。毎秒 9 ファイルを無条件に書き換えないため。"""
    body = json.dumps(data, ensure_ascii=False)
    if not force and cache.get(name) == body:
        return
    path = os.path.join(RENDER, name + ".json")
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w") as f:
            # 末尾の改行は必須。bash の read は区切りに当たらず EOF を踏むと
            # 変数には入れるが**非ゼロを返す**ため、読み手が失敗と誤判定する
            f.write(body + "\n")
        os.replace(tmp, path)
        cache[name] = body
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def sync_commands():
    """commands.json が編集されていたらボタン同期を起動する。
    以前は cc-widget.sh が毎秒 stat していたが、読み手を組み込みだけに
    したいのでこちらへ移した。"""
    try:
        m = int(os.path.getmtime(CMDS))
    except OSError:
        return
    try:
        with open(STAMP) as f:
            if f.read().strip() == str(m):
                return
    except OSError:
        pass
    log = open(os.path.join(BASE, "sync.log"), "a")
    subprocess.Popen(["/usr/bin/python3", os.path.join(BASE, "cc-sync.py")],
                     stdout=log, stderr=log)


# 自分が読み込んでいるコード。更新されたら終了し、ウィジェットに拾い直させる
SOURCES = ("cc-render.py", "cc-status.py", "cc-btn.py", "cc-menu.py",
           "cc_common.py")


def source_stamp():
    out = []
    for n in SOURCES:
        try:
            out.append(os.path.getmtime(os.path.join(BASE, n)))
        except OSError:
            out.append(0)
    return tuple(out)


def seen_recently():
    """ウィジェットが読みに来ているか（cc-widget.sh が .seen を触る）。"""
    try:
        return time.time() - os.path.getmtime(SEEN) < IDLE_EXIT_SEC
    except OSError:
        return False


def main():
    os.makedirs(RENDER, exist_ok=True)

    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 既に動いている。ウィジェットは全員が起動を試みるのでここは正常系
        return 0

    with open(PIDFILE, "w") as f:
        f.write("%d\n" % os.getpid())
    # 起動直後に「読まれていない」と判定して即終了しないよう種を蒔く
    open(SEEN, "a").close()
    os.utime(SEEN, None)

    stop = []
    signal.signal(signal.SIGTERM, lambda *a: stop.append(1))

    cache = {}
    tick = 0
    stamp = source_stamp()
    while not stop:
        t0 = time.time()
        # chezmoi apply でスクリプトが差し替わっても、常駐したままだと
        # 古いコードを実行し続ける（実際に新しい描画対象が出ずに嵌まった）。
        # 終了すればウィジェットが次のティックで起動し直す
        if source_stamp() != stamp:
            break
        try:
            force = tick % FORCE_WRITE_EVERY == 0
            for name, data in render_all().items():
                write(name, data, cache, force)
            sync_commands()
        except Exception:
            # 1ティック失敗しても常駐は続ける（表示は前回値が残る）
            pass
        if not seen_recently():
            break
        tick += 1
        time.sleep(max(0.0, TICK - (time.time() - t0)))

    try:
        os.remove(PIDFILE)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    if "--once" in sys.argv[1:]:
        # デバッグ用: 1ティックぶんの出力を人が読める形で表示する
        print(json.dumps(render_all(), ensure_ascii=False, indent=1))
        sys.exit(0)
    try:
        sys.exit(main())
    except Exception as e:
        print("render daemon failed:", e, file=sys.stderr)
        sys.exit(1)
