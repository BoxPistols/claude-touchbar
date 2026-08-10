"""cc-status.py / cc-btn.py 共通: セッション読込・選択・transcript解析。"""
import json
import os
import re
import subprocess
import time

BASE = os.path.expanduser("~/.claude/btt")
SESS = os.path.join(BASE, "sessions")
CACHE = os.path.join(BASE, "cache")
PROJECTS = os.path.expanduser("~/.claude/projects")

# pid未記録の古い形式の状態ファイル用フォールバック
FALLBACK_STALE_SEC = 900

def priority(rec):
    """表示の注意度（小さいほど優先）。

    **`waiting` を一律で最優先にしないこと。** `waiting` には性質の違う2つが
    同居している:
      permission = 許可プロンプトが出ていてユーザーの操作が要る（緊急）
      input      = 60秒アイドル通知が飛んだだけで、何も要求していない（緊急度ゼロ）
    state だけで並べていた頃は、放置しただけのセッションが**20分ビルド中の
    セッションを最大1時間隠していた**。§3 が「waiting > busy」を採った動機
    （許可待ち2.5時間が隠された件）は permission にのみ当てはまり、input に
    同じ優先度を与えると逆向きの同じ失敗になる。
    描画側（cc-status.py）が input を idle と同格に扱っているのとも揃える。
    """
    st = rec.get("state")
    if st == "waiting":
        return 0 if rec.get("waiting_kind") == "permission" else 2
    return {"busy": 1, "idle": 3}.get(st, 3)

CLAUDE_DESKTOP = "com.anthropic.claudefordesktop"


CMDS = os.path.join(BASE, "commands.json")
PERMS = os.path.join(BASE, "permissions.json")

# 許可プロンプトの選択肢に割り当てる番号ボタンの上限。AskUserQuestion は
# スキーマ上 2〜4 択で、末尾に Other が付いて最大 5
PERM_MENU_SLOTS = 5

DEFAULT_PERM_LABELS = ["許可", "常に許可", "拒否"]

# ボタン1つに載せる文字量の上限（プレフィックス込みの cell 数）。
#
# **文字数ではなく cell で数えること** — CJK は 2 cell 幅なので、「12文字まで」に
# すると日本語では 24 cell ぶんになり、隣のボタンを押し出す。
#
# 値の根拠は実測。`BTTTBWidgetWidth` は下限で、内容が長ければ実際の描画は伸びる
# （perm-* は 3 つとも幅 100px で作られているのに `2 常に許可` = 10 cell が
# 欠けずに出る）。したがって上限を決めるのは**バー全体の残り幅**であって、
# ウィジェットの設定値ではない。
#
# **短すぎる方が危険。** 10 cell にしたところ、選択肢
# 「グループをやめて平らに戻す」「グループのまま残す」が実機で
# `1 グルー…` `2 グルー…` になり **1 と 2 が区別できなくなった**。
# 共通の接頭辞を持つ選択肢では、切り詰めは「読みにくい」ではなく
# 「**どちらを押すか決められない**」に直結する。
#
# 許可プロンプト中はコマンドボタンを隠すので、その幅を丸ごと使える
# （実機でも右側に大きな空きが出ていた）。
#   応答ボタン: 最大3個 → 1個あたり ~220px
#   番号ボタン: 最大5個 → 1個あたり ~130px
# 変更したら**実機で右端が切れないか**を必ず見ること。
#
# **出処で分ける。** 番号ボタンは許可プロンプト由来とコマンドメニュー由来の
# 両方で出るが、混み具合が全く違う:
#   許可プロンプト由来 … コマンドボタンが全部隠れるので広く使える
#   コマンドメニュー由来 … コマンドボタン9個が出たままなので余地が無い
#     （こちらのラベルは commands.json の宣言で元から短い: Def / Opus …）
# 一方に合わせると他方が溢れる。
#
# 値は撮影した実機画像から**画素を測って**決める。目分量で「まだ余っている」と
# 判断すると外す（実際に外した。詳細は DESIGN §16.5）。
#
#   ボタンが使える幅  = 306px 〜 1356px（右はコントロールストリップの左端）
#                     = 1050px ÷ 1.34 px/pt = 784pt
#   n cell のボタン    = 22 + 9n pt（+ 間隔 4pt）
#   5個並べたときの総幅 = 130 + 45n pt
#
#   13 cell → 715pt（余白 69pt。状態表示が「🔐 App許可待ち」でも 42pt 残る）
#   14 cell → 760pt（同 -3pt。あふれる）
#   16 cell → 850pt（**実機で5個目が 50px 欠けた**）
PERM_LABEL_CELLS = 28          # 応答ボタン（最大3個。実測で右に約 300pt の余白）
PERM_MENU_LABEL_CELLS = 13     # 番号ボタン（許可プロンプト由来・最大5個）
MENU_LABEL_CELLS = 12          # 番号ボタン（コマンドメニュー由来・据置）


def label_cells(total, prefix=""):
    """prefix を含めて total cell に収めるとき、ラベルに使える cell 数。"""
    return max(0, total - cell_len(prefix))


def cell_len(s):
    return sum(2 if ord(c) > 0x2E80 else 1 for c in str(s))


def clip(s, cells):
    """Touch Bar に収まる cell 数に詰める。元の文言は画面側に出ている。"""
    s = " ".join(str(s).split())
    if cell_len(s) <= cells:
        return s
    out, used = "", 0
    for ch in s:
        w = 2 if ord(ch) > 0x2E80 else 1
        if used + w > cells - 1:      # 末尾の "…" に 1 cell 空ける
            break
        out += ch
        used += w
    return out + "…"


def load_commands():
    try:
        with open(CMDS) as f:
            return json.load(f)
    except Exception:
        return {}


def command_buttons():
    """Touch Bar に出すコマンドボタンの一覧。

    **解釈をここ1箇所に集約すること。** ボタンを作る側(cc-sync.py)と描画する側
    (cc-menu.py)が別々にフィルタしていた頃は、`enabled` の既定値や条件が片方だけ
    変わると、インデックスがずれて「別のボタンを描画する」形で静かに壊れた。
    """
    return [b for b in load_commands().get("buttons", [])
            if b.get("enabled", True) and b.get("label") and b.get("command")]


def menu_slots():
    """用意すべき番号ボタンの数。

    commands.json で宣言されたメニューの最大長と、許可プロンプト由来の選択肢
    （AskUserQuestion は仕様上 2〜4 択 + Other で最大 5）の大きい方。
    ここを増やすと 642 ウィジェットの新規作成が発生し **BTT の再起動が要る**ので、
    許可プロンプト側は必ずこの数に丸めて描く（足りないぶんは出さない）。
    """
    declared = max([len(b.get("menu") or []) for b in command_buttons()] or [0])
    return max(declared, PERM_MENU_SLOTS)


_PERMS_CACHE = (None, None)


def load_permissions():
    """permissions.json。mtime が変わったときだけ読み直す。

    permission_labels() は1ティックにボタンの数だけ呼ばれる（応答3 + 番号5）。
    毎回 open すると、描画を常駐化して削ったコストを細かく戻すことになる。
    """
    global _PERMS_CACHE
    try:
        m = os.path.getmtime(PERMS)
    except OSError:
        return {}
    if _PERMS_CACHE[0] == m:
        return _PERMS_CACHE[1]
    try:
        with open(PERMS) as f:
            data = json.load(f)
    except Exception:
        data = {}
    _PERMS_CACHE = (m, data)
    return data


def permission_labels(rec):
    """許可待ちセッションの選択肢ラベル。

    優先順:
      1. `perm_options` — PermissionRequest の payload から実際に取り出したもの。
         唯一の確かな情報源（AskUserQuestion の `questions[0].options[].label`）
      2. permissions.json の `tools[<ツール名>]` — 実測して書き足す表
      3. `default` — Bash / Edit 等の標準的な 1 Yes / 2 常に / esc No

    **空リストは「出さない」を意味する。** 選択肢が 1/2/esc と違うと分かって
    いるツールは、当てずっぽうのラベルを出すより隠す方が安全（誤爆より無反応）。
    """
    opts = rec.get("perm_options")
    if opts:
        return [str(o) for o in opts if str(o).strip()]
    cfg = load_permissions()
    tools = cfg.get("tools") or {}
    tool = rec.get("tool") or ""
    if tool in tools:
        return [str(o) for o in (tools[tool] or [])]
    return [str(o) for o in (cfg.get("default") or DEFAULT_PERM_LABELS)]


def permission_session(sessions=None, fb=None):
    """応答ボタンの対象になる許可待ちセッション（**表示用**）。

    以前は cc-btn.py が「許可待ちのセッションが1つでもあるか」で表示を決め、
    cc-send.py は「全許可待ちのうち updated_at 最大」へ送っていた。両者は
    一致する保証が無く、2つのセッションが同時に許可待ちだと**別のセッションを
    見て出したボタンが、別のセッションへ送る**。§12.3 で番号ボタンについて
    直した誤りが、許可経路にそのまま残っていたもの。選択をここに一本化する。

    送信側は send_target()（実測でフォーカスを確定）を使い、その結果がここと
    食い違えば送らない。表示は近似・送信は実測、という既存の役割分担に合わせる。
    """
    if sessions is None:
        sessions = load_sessions()
    if fb is None:
        fb = front_bundle()
    rec, _others, _fb = pick_for_front(sessions, fb)
    if not rec:
        return None
    if rec.get("state") != "waiting" or rec.get("waiting_kind") != "permission":
        return None
    if not rec.get("term_guid") or is_peers(rec):
        return None
    if session_host(rec) != fb:
        return None
    return rec


def is_peers(rec):
    """デスクトップアプリ駆動セッション(server:claude-peers)か。
    UIがアプリ側にあるため、ターミナルへのキー送信・タブフォーカスは無効。"""
    return rec.get("ui") == "peers"


PID_RECHECK_SEC = 60
_pid_cache = {}


def process_start(pid):
    """プロセスの開始時刻文字列。取れなければ None。

    **`LC_ALL=C` を必ず付けること。** lstart はロケール依存で書式が変わり
    (日本語環境では "月 8/ 3 01:52:17 2026")、記録する側(hook＝ターミナルの
    環境を継承)と照合する側(描画＝BTT から起動)で環境が違うと文字列が一致せず、
    **生きているセッションを死んだと判定して状態ファイルを消してしまう**。
    """
    try:
        out = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "lstart="],
                             capture_output=True, text=True, timeout=2,
                             env=dict(os.environ, LC_ALL="C")).stdout
    except Exception:
        return None
    return " ".join(out.split()) or None


def pid_matches(pid, want, now):
    """pid が**再利用されていない**ことを開始時刻で確認する。

    os.kill(pid, 0) だけだと、Claude を強制終了したあと(SessionEnd 未発火)に
    その pid が別プロセスへ割り当てられた場合、状態ファイルが永久に消えず
    実在しないセッションの表示が残り続ける。
    ps は 60 秒に1回までに抑える（描画は毎秒走るため）。
    """
    if not want:
        return True      # 開始時刻を持たない旧形式の記録は照合できないので通す
    key = (pid, want)
    hit = _pid_cache.get(key)
    if hit and now - hit[0] < PID_RECHECK_SEC:
        return hit[1]
    ok = process_start(pid) == want
    _pid_cache[key] = (now, ok)
    return ok


def load_sessions():
    """生きているセッションの一覧。死んだpidの状態ファイルはここで削除する。"""
    out = []
    try:
        files = [f for f in os.listdir(SESS) if f.endswith(".json")]
    except OSError:
        return out
    now = time.time()
    for name in files:
        path = os.path.join(SESS, name)
        try:
            with open(path) as f:
                rec = json.load(f)
        except Exception:
            continue
        pid = rec.get("pid")
        if pid:
            alive = True
            try:
                os.kill(int(pid), 0)
                # pid が生きていても、別プロセスに再利用されていれば別物
                alive = pid_matches(int(pid), rec.get("pid_start"), now)
            except ProcessLookupError:
                alive = False
            except (PermissionError, ValueError, TypeError):
                # 別ユーザーのプロセス等。判断材料が無いので生存扱いで通す
                pass
            if not alive:
                try:
                    os.remove(path)
                except OSError:
                    pass
                continue
        elif now - rec.get("updated_at", 0) > FALLBACK_STALE_SEC:
            continue
        out.append(rec)
    return out


def pick(sessions):
    """表示すべきセッションと「他にliveなセッション数」を返す。
    1時間以上更新の無いセッションは(状態に関わらず)後回しにする —
    放置されたwaitingが現役のbusyより優先されるのを防ぐ。"""
    if not sessions:
        return None, 0
    now = time.time()
    s = sorted(sessions,
               key=lambda r: (now - r.get("updated_at", 0) >= 3600,
                              priority(r),
                              -r.get("updated_at", 0)))
    return s[0], len(sessions) - 1


def front_bundle():
    """フロントアプリの bundle id。

    実測 16.9ms(lsappinfo を 2 プロセス起動するため)。**ウィジェットごとに
    呼ばないこと** — 以前は 9 ウィジェットが毎秒それぞれ呼んでいた。
    今は cc-render.py がティックごとに 1 回だけ呼び、結果を各描画関数へ渡す。
    """
    import subprocess
    try:
        out = subprocess.run(
            "/usr/bin/lsappinfo info -only bundleid `/usr/bin/lsappinfo front`",
            shell=True, capture_output=True, text=True, timeout=2).stdout
        parts = out.split('"')
        return parts[3] if len(parts) >= 4 else ""
    except Exception:
        return ""


def session_host(rec):
    """このセッションがどのアプリのものかを bundle id で返す。"""
    # peersのterm_guid/term_programはサーバーが継承した環境の残骸なので見ない
    if is_peers(rec):
        return CLAUDE_DESKTOP
    if rec.get("term_guid") or rec.get("term_program") == "iTerm.app":
        return "com.googlecode.iterm2"
    if rec.get("term_program") == "Apple_Terminal":
        return "com.apple.Terminal"
    return rec.get("host_bundle") or ""


def focused_guid():
    """cc-focus-daemon.py(iTerm2 AutoLaunch)が書くフォーカス中セッションGUID。
    ファイルが無い環境(デーモン未導入)では None → 従来ロジックに完全フォールバック。
    mtimeの閾値は設けない: デーモンはフォーカス「変化時」のみ書くため、同じタブに
    長時間留まるほど古くなるのが正常で、鮮度＝有効性ではない(フォーカスは状態)。
    無効化はGUID一致するliveセッションの有無だけで判定する。"""
    try:
        with open(os.path.join(BASE, "focus")) as f:
            return f.read().strip() or None
    except OSError:
        return None


def pick_for_front(sessions, fb=None):
    """フロントアプリに属するセッションを優先して選ぶ（アプリ切替に表示が追従する）。
    iTerm2フロント時はフォーカス中タブのセッション(focusファイルのGUID一致)を最優先。
    フロントに対応するセッションが無ければ全体から優先度順に選ぶ。
    fb を渡すと front_bundle() の再実行を省ける（呼び出し側で共有するため）。
    戻り値: (選ばれたセッション, 他のliveセッション数, フロントのbundle id)"""
    if fb is None:
        fb = front_bundle()
    if fb == "com.googlecode.iterm2":
        g = focused_guid()
        if g:
            # peersのterm_guidは継承環境の残骸で実際の表示位置ではないため除外
            hit = [r for r in sessions
                   if r.get("term_guid") == g and not is_peers(r)]
            if hit:
                rec, _ = pick(hit)
                return rec, max(0, len(sessions) - 1), fb
    scoped = [r for r in sessions if session_host(r) == fb] if fb else []
    rec, _ = pick(scoped or sessions)
    others = max(0, len(sessions) - 1)
    return rec, others, fb


def target_session(sessions=None, fb=None):
    """**表示用**の「送れそうな相手」。ボタンを出すかどうかの判定にだけ使う。

    毎ティック呼ばれるので安いロジック（pick_for_front）で近似する。
    **送信には絶対に使わないこと** — 下記 send_target() を使う。
    """
    if sessions is None:
        sessions = load_sessions()
    rec, _others, _fb = pick_for_front(sessions, fb)
    if rec and not is_peers(rec) and rec.get("term_guid"):
        return rec
    return None


def focused_iterm_guid():
    """いま前面にある iTerm2 セッションの GUID。取れなければ None。

    実測 ~300ms なので毎ティックには使えない（表示側は focus デーモンが書く
    ファイルを読む）。**送信直前の1回だけ**使う。
    """
    try:
        r = subprocess.run(
            ["/usr/bin/osascript", "-e",
             'tell application "iTerm2" to return id of '
             'current session of current tab of current window'],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    return (r.stdout or "").strip() or None


def send_target(sessions=None):
    """**送信先**セッション。確定できなければ None（＝送らない）。

    表示用の選択（pick_for_front）を送信に流用してはいけない。あちらは
    「最も注意すべきセッションを見せる」方針なので、優先度の高い別タブが
    あればそちらを返す。送信に使うと**ユーザーが見ていないセッションに
    文字が飛ぶ**。

    実際に起きた事故: フォーカス追従デーモンが動いておらず `focused_guid()`
    が None だったため優先度順のフォールバックに落ち、ユーザーが押した
    「1」と「/review」が丸ごと**別プロジェクトのセッション**に入り、
    そちらの入力欄に `1/review` として現れた。

    送信先は「いま実際にフォーカスしているセッション」でなければならない。
    ここだけは 300ms 払ってでも実測で確定させ、確定できなければ送らない。
    """
    if front_bundle() != "com.googlecode.iterm2":
        return None
    guid = focused_iterm_guid()
    if not guid:
        return None
    if sessions is None:
        sessions = load_sessions()
    for r in sessions:
        if r.get("term_guid") == guid and not is_peers(r):
            return r
    return None


def transcript_path(rec):
    p = rec.get("transcript")
    if p and os.path.exists(p):
        return p
    cwd, sid = rec.get("cwd"), rec.get("session_id")
    if not cwd or not sid:
        return None
    # projects配下のディレクトリ名は英数字以外がすべて '-' になる
    proj = re.sub(r"[^A-Za-z0-9]", "-", cwd)
    cand = os.path.join(PROJECTS, proj, sid + ".jsonl")
    return cand if os.path.exists(cand) else None


def transcript_stamp(rec):
    """transcript の (size, mtime)。取れなければ None。

    「このセッションで実際に何か起きたか」の判定に使う。`updated_at` は hook が
    発火するたび無条件に動くため使えない — **60秒アイドルの Notification は
    ユーザー操作なしに発火する**ので、それで失効させるとメニューが開いたままなのに
    番号ボタンが消える。transcript はタイマー起因では書かれず、実際のターンでは
    必ず追記されるので、状態を分類せずに活動だけを拾える。
    """
    path = transcript_path(rec)
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    # JSON を往復するので list で返す(tuple だと比較時に型がずれる)
    return [st.st_size, st.st_mtime]


def transcript_info(rec):
    """(現在のコンテキストトークン数, モデル名) を1パスで取得。(mtime,size)キャッシュ付き。"""
    path = transcript_path(rec)
    if not path:
        return None, None
    try:
        st = os.stat(path)
    except OSError:
        return None, None

    cpath = os.path.join(CACHE, (rec.get("session_id") or "x") + ".json")
    try:
        with open(cpath) as f:
            c = json.load(f)
        if c.get("mtime") == st.st_mtime and c.get("size") == st.st_size:
            return c.get("tokens"), c.get("model")
    except Exception:
        pass

    tokens = model = None
    try:
        with open(path, "rb") as f:
            chunk = min(st.st_size, 400_000)
            f.seek(st.st_size - chunk)
            lines = f.read().split(b"\n")
    except OSError:
        return None, None
    for raw in reversed(lines):
        if b'"assistant"' not in raw:
            continue
        try:
            m = json.loads(raw)["message"]
        except Exception:
            continue
        u = m.get("usage") or {}
        total = (u.get("input_tokens", 0)
                 + u.get("cache_creation_input_tokens", 0)
                 + u.get("cache_read_input_tokens", 0)
                 + u.get("output_tokens", 0))
        if total:
            tokens, model = total, m.get("model")
            break

    try:
        os.makedirs(CACHE, exist_ok=True)
        tmp = "%s.%d.tmp" % (cpath, os.getpid())
        with open(tmp, "w") as f:
            json.dump({"mtime": st.st_mtime, "size": st.st_size,
                       "tokens": tokens, "model": model}, f)
        os.replace(tmp, cpath)
    except OSError:
        pass
    return tokens, model


CTX_FRESH_SEC = 900   # statusLine の記録をどこまで信用するか


def context_pct(rec):
    """コンテキスト使用率(%)。無ければ None。

    正の情報源は statusLine が cache/<session_id>.ctx.json に書く実測値
    (cc-statusline.py)。窓サイズをモデル名から推定する方法は当てにならない:
    transcript のモデル名に [1m] は現れず、settings.json の model キーは
    /model で「Default」を選ぶと消えるため、1M のセッションを 200k と
    誤判定して 225% のような値になる。

    statusLine の記録が無い/古い場合だけ、transcript のトークン数を
    「記録済みの窓サイズ」で割って代用する(窓サイズも無ければ諦める)。
    """
    sid = rec.get("session_id")
    ctx = {}
    if sid:
        try:
            with open(os.path.join(CACHE, sid + ".ctx.json")) as f:
                ctx = json.load(f)
        except Exception:
            ctx = {}

    if ctx.get("pct") is not None and time.time() - ctx.get("at", 0) < CTX_FRESH_SEC:
        return float(ctx["pct"])

    window = ctx.get("window")
    if not window:
        return None
    tokens = transcript_info(rec)[0]
    if not tokens:
        return None
    return 100.0 * tokens / window


def compact_model(display, model_id=""):
    """'Opus 5 (1M context)' → 'Opus5 1M' / 'Sonnet 5' → 'Sonnet5'。
    窓が1Mかどうかはコンテキスト%の意味が変わる情報なので落とさない。"""
    if not display:
        return None
    name = display.split("(")[0].strip().replace(" ", "")
    if "[1m]" in (model_id or "").lower() or "1M" in display:
        name += " 1M"
    return name


EFFORT_SHORT = {"medium": "med"}


def session_model_effort(rec):
    """(モデル短縮名, effort) を返す。無ければ (None, None)。

    情報源は statusLine が書いたキャッシュだけ。hook の payload にも
    transcript にも出ない（transcript のモデル名には `[1m]` が現れない）。
    Touch Bar の /model /effort ボタンで切り替えた結果を確認する手段が
    これまで無かったので、表示に載せる。
    """
    sid = rec.get("session_id")
    if not sid:
        return None, None
    try:
        with open(os.path.join(CACHE, sid + ".ctx.json")) as f:
            c = json.load(f)
    except Exception:
        return None, None
    if time.time() - c.get("at", 0) > CTX_FRESH_SEC:
        return None, None
    eff = c.get("effort") or ""
    return compact_model(c.get("model"), c.get("model_id")), EFFORT_SHORT.get(eff, eff)


def is_active(rec, now=None):
    """+Nバッジ集計用: 実際に動いている/待っているとみなすか。
    busyは30分、waitingは60分以内に更新があるものだけ。
    peersはセッション終了後もサーバープロセスが残りpid生存sweepを
    すり抜けるため、時間でゾンビ状態ファイルを弾く。"""
    now = now or time.time()
    age = now - rec.get("updated_at", 0)
    st = rec.get("state")
    if st == "busy":
        return age < 1800
    if st == "waiting":
        return age < 3600
    return False


def usage_limits(max_age=1800):
    """statusLine(cc-statusline.py)が書いたプラン使用率キャッシュを返す。
    (セッション%, 週間%) 。古い/無いときは (None, None)。"""
    try:
        with open(os.path.join(BASE, "usage.json")) as f:
            u = json.load(f)
        if time.time() - u.get("updated_at", 0) > max_age:
            return None, None
        return u.get("session"), u.get("week")
    except Exception:
        return None, None


def short_model(model):
    if not model:
        return None
    m = model.replace("claude-", "")
    m = re.sub(r"-20\d{6,}.*$", "", m)
    return m
