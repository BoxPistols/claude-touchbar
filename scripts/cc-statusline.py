#!/usr/bin/env python3
"""Claude Code の statusLine コマンド。

2つの役割:
  1. stdin の JSON から rate_limits(セッション/週間の使用率)を
     ~/.claude/btt/usage.json へ書き出す(Touch Barウィジェットが読む)
  2. TUI 下部に表示する1行を出力する

statusLine は表示更新のたびに呼ばれるため、処理は軽く保つこと。
"""
import json
import os
import sys
import time

BASE = os.path.expanduser("~/.claude/btt")   # usage.jsonの置き場(Touch Bar連携と共有)


def pct(v):
    """utilization は 0-1 か 0-100 かバージョン差があるため両対応で%整数へ。"""
    if v is None:
        return None
    v = float(v)
    return round(v * 100) if v <= 1.0 else round(v)


def main():
    os.makedirs(BASE, exist_ok=True)   # BTTの無いマシン(WSL2等)でも動くように
    try:
        d = json.load(sys.stdin)
    except Exception:
        d = {}

    # 初回調査用に生データを残す(容量は1エントリのみ)
    try:
        with open(os.path.join(BASE, "usage-raw.json"), "w") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

    rl = d.get("rate_limits") or {}
    out = {"updated_at": time.time()}
    # 実測(v2.1.220): rate_limits.five_hour/.seven_day に used_percentage(0-100) と
    # resets_at(epoch秒) が入る。utilization は旧名の保険
    for key, names in (
        ("session", ("five_hour", "session", "primary")),
        ("week", ("seven_day", "week", "weekly")),
    ):
        for n in names:
            item = rl.get(n)
            if isinstance(item, dict):
                u = item.get("used_percentage")
                if u is None:
                    u = pct(item.get("utilization"))
                if u is not None:
                    out[key] = round(float(u))
                    reset = item.get("resets_at")
                    if reset:
                        out[key + "_resets_at"] = reset
                    break

    tmp = os.path.join(BASE, "usage.json.%d.tmp" % os.getpid())
    try:
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, os.path.join(BASE, "usage.json"))
    except OSError:
        pass

    # コンテキスト使用率をセッション単位で記録する。
    # ここが唯一の正確な情報源: transcript のモデル名には [1m] が出ず、
    # settings.json は /model で「Default」を選ぶと model キー自体が消えるため、
    # 窓サイズを名前から推定すると外れる(200k と誤判定して 225% のような表示になる)。
    # モデル名と effort もここで記録する。**唯一の入手経路**で、hook の payload
    # にも transcript にも出ない(transcript のモデル名には [1m] が現れない)。
    # Touch Bar の /model /effort ボタンで切り替えた結果を確認する術が無かった
    sid = d.get("session_id")
    cw = d.get("context_window") or {}
    if sid and cw.get("used_percentage") is not None:
        ctx = {"pct": round(float(cw["used_percentage"])),
               "window": cw.get("context_window_size"),
               "model": (d.get("model") or {}).get("display_name") or "",
               "model_id": (d.get("model") or {}).get("id") or "",
               "effort": (d.get("effort") or {}).get("level") or "",
               "at": out["updated_at"]}
        cdir = os.path.join(BASE, "cache")
        ctmp = os.path.join(cdir, "%s.ctx.json.%d.tmp" % (sid, os.getpid()))
        try:
            os.makedirs(cdir, exist_ok=True)
            with open(ctmp, "w") as f:
                json.dump(ctx, f)
            os.replace(ctmp, os.path.join(cdir, sid + ".ctx.json"))
        except OSError:
            pass

    # ---- TUI 表示行 ----
    model = (d.get("model") or {}).get("display_name") or ""
    cwd = os.path.basename((d.get("workspace") or {}).get("current_dir") or "")
    parts = [p for p in (model, cwd) if p]
    s, w = out.get("session"), out.get("week")
    if s is not None:
        parts.append("S:%d%%" % s)
    if w is not None:
        parts.append("W:%d%%" % w)
    print(" | ".join(parts) if parts else "Claude Code")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Claude Code")
