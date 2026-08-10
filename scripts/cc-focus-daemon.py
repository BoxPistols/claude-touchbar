#!/usr/bin/env python3
"""iTerm2 AutoLaunch デーモン: フォーカス中セッションの GUID を ~/.claude/btt/focus に書く。

配置先: ~/Library/Application Support/iTerm2/Scripts/AutoLaunch/cc-focus-daemon.py
前提: iTerm2 Settings > General > Magic > "Enable Python API"（Runtime は初回に自動DL）。
イベント駆動(FocusMonitor購読)なのでポーリングゼロ。表示側(cc_common.pick_for_front)は
このファイルの GUID と一致する live セッションがあればそれを最優先する。
"""
import asyncio
import os

import iterm2

FOCUS = os.path.expanduser("~/.claude/btt/focus")


def write_focus(guid):
    """tmp→rename の atomic write。読み手(毎秒のBTTウィジェット)に中途半端な内容を見せない。"""
    if not guid:
        return
    tmp = "%s.%d.tmp" % (FOCUS, os.getpid())
    try:
        os.makedirs(os.path.dirname(FOCUS), exist_ok=True)
        with open(tmp, "w") as f:
            f.write(guid)
        os.replace(tmp, FOCUS)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


async def main(connection):
    # 起動直後の現在フォーカスを一度書く（次のフォーカス変化まで空白にしない）
    try:
        app = await iterm2.async_get_app(connection)
        win = app.current_terminal_window
        if win and win.current_tab and win.current_tab.current_session:
            write_focus(win.current_tab.current_session.session_id)
    except Exception:
        pass

    # 例外で死なないよう購読ループを保護（切断時は run_forever が再接続を担う）
    while True:
        try:
            async with iterm2.FocusMonitor(connection) as mon:
                while True:
                    update = await mon.async_get_next_update()
                    changed = update.active_session_changed
                    if changed:
                        write_focus(changed.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(1)


iterm2.run_forever(main)
