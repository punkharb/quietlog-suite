"""Quietlog Suite - 5 local-first desktop tools in one tray app.

Modules (each toggle on/off in the tray Settings menu, saved to settings.json):
  - Quietlog    : passive time/focus journal (which app you actually used)
  - Tabreaper   : save/restore window layouts ("scenes")
  - Anchor      : detects compulsive window-switching, fades in a focus overlay
  - Localback   : auto-versioned backup of a watched folder (point-in-time restore)
  - Threadkeeper: searchable recall of window titles + things you copied

Everything stays on this machine. No cloud, no account, no network calls.

Run:        python quietlog.py
Self-test:  python quietlog.py --selftest
Debug log:  set QUIETLOG_DEBUG=1  (writes %LOCALAPPDATA%\\Quietlog\\debug.log)
"""
import ctypes
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import webbrowser
import winreg
from collections import deque
from datetime import datetime

APP_NAME = "Quietlog"
APP_VERSION = "1.0.0"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DB_DIR = os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "Quietlog")
DB_PATH = os.path.join(DB_DIR, "quietlog.db")
SETTINGS_PATH = os.path.join(DB_DIR, "settings.json")
SCENES_PATH = os.path.join(DB_DIR, "scenes.json")
BACKUP_DIR = os.path.join(DB_DIR, "localback")

# --- tunable knobs ---------------------------------------------------------
SAMPLE_INTERVAL = 5       # Quietlog: seconds between samples
IDLE_THRESHOLD = 60       # seconds of no input => "(idle)"
IDLE_LABEL = "(idle)"
ANCHOR_WINDOW = 60        # Anchor: rolling window (seconds) to count switches in
ANCHOR_COOLDOWN = 120     # seconds before the overlay can fire again
# sensitivity -> switches-within-window that count as "thrashing" (lower = more sensitive)
ANCHOR_LEVELS = {"low": 18, "medium": 12, "high": 8}
BACKUP_INTERVAL = 15      # Localback: seconds between folder scans
BACKUP_MAX_MB = 20        # skip files bigger than this (don't snapshot huge blobs)
BACKUP_KEEP = 25          # keep at most this many versions per file (prune oldest)
THREAD_INTERVAL = 10      # Threadkeeper: seconds between captures
RETAIN_DAYS = 90          # delete logged samples / recall older than this
RECALL_MAX_ROWS = 50000   # hard cap on recall rows (full-text can grow fast)

# Fonts: Win11-native faces (no bundling). Resolved at first GUI use against what's
# actually installed, falling back to Segoe UI / Consolas on older Windows.
FONT_HEAD = "Segoe UI"    # headings  (upgraded to "Segoe UI Variable Display" if present)
FONT_UI = "Segoe UI"      # body / labels / buttons
FONT_MONO = "Consolas"    # code / recall results (upgraded to "Cascadia Mono" if present)


def _resolve_fonts():
    """Pick the nicest installed face for each role. Call once on the GUI thread."""
    global FONT_HEAD, FONT_MONO
    try:
        import tkinter.font as tkfont
        fams = set(tkfont.families())
        if "Segoe UI Variable Display" in fams:
            FONT_HEAD = "Segoe UI Variable Display"
        if "Cascadia Mono" in fams:
            FONT_MONO = "Cascadia Mono"
        elif "Cascadia Code" in fams:
            FONT_MONO = "Cascadia Code"
    except Exception:
        pass

DEFAULT_SETTINGS = {
    "quietlog": True,
    "tabreaper": True,
    "anchor": False,       # opt-in: it draws an overlay
    "localback": False,    # opt-in: needs a folder chosen
    "threadkeeper": False,  # opt-in: records window titles
    "threadkeeper_clipboard": False,  # extra opt-in: also store copied text (may catch secrets)
    "threadkeeper_fulltext": False,   # extra opt-in: capture visible window text (heavy; needs uiautomation)
    "localback_folder": "",
    "anchor_sensitivity": "medium",   # low | medium | high
    "anchor_snooze_until": 0,         # epoch secs; overlay suppressed until then
    "app_tags": {},                   # exe -> "work" | "distraction" | "neutral"
}


def prune_old(con, table, days=RETAIN_DAYS, now=None):
    """Delete rows older than `days`. table is an internal constant, not user input."""
    now = now if now is not None else int(time.time())
    cur = con.execute(f"DELETE FROM {table} WHERE ts < ?", (now - days * 86400,))
    con.commit()
    return cur.rowcount


def cap_recall(con, max_rows=RECALL_MAX_ROWS):
    """Keep only the newest `max_rows` recall rows (by rowid, robust to dup ts)."""
    n = con.execute("SELECT COUNT(*) FROM recall").fetchone()[0]
    if n > max_rows:
        con.execute(
            "DELETE FROM recall WHERE rowid NOT IN "
            "(SELECT rowid FROM recall ORDER BY ts DESC, rowid DESC LIMIT ?)", (max_rows,))
        con.commit()
    return max(0, n - max_rows)


def clear_table(table):
    """Wipe all rows from a recording table ('samples' or 'recall'). Privacy control."""
    assert table in ("samples", "recall")
    con = ql_connect() if table == "samples" else tk_connect()
    try:
        n = con.execute(f"DELETE FROM {table}").rowcount
        con.commit()
        return n
    finally:
        con.close()


_NOTIFY = None   # set to icon.notify once the tray is up
_ICON = None     # the pystray Icon, so the dashboard can quit the whole app


def notify(msg, title="Quietlog"):
    """Show a tray toast if the icon is up; no-op otherwise."""
    try:
        if _NOTIFY:
            _NOTIFY(msg, title)
    except Exception:
        pass


def quit_app():
    """Stop the tray icon (and thus the whole suite). Used by tray + dashboard."""
    if _ICON:
        _ICON.stop()
    else:
        os._exit(0)


def _dbg(msg):
    """Append a startup breadcrumb when QUIETLOG_DEBUG is set. Survives --noconsole."""
    if not os.environ.get("QUIETLOG_DEBUG"):
        return
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with open(os.path.join(DB_DIR, "debug.log"), "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
    except Exception:
        pass


# --- settings --------------------------------------------------------------
def load_settings(path=SETTINGS_PATH):
    s = dict(DEFAULT_SETTINGS)
    try:
        with open(path, encoding="utf-8") as f:
            s.update(json.load(f))
    except (FileNotFoundError, ValueError):
        pass
    return s


def save_json(obj, path):
    """Atomic write: dump to a temp file then os.replace, so a crash mid-write can't
    leave a half-written (corrupt) JSON that silently resets to defaults."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save_settings(s, path=SETTINGS_PATH):
    save_json(s, path)


SETTINGS = load_settings()


# --- single instance -------------------------------------------------------
def already_running(name="Global\\QuietlogSingleton"):
    """True if another copy is already up. Named mutex, no deps."""
    ctypes.windll.kernel32.CreateMutexW(None, False, name)
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


# --- auto-start on login ----------------------------------------------------
def _launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else sys.executable
    return f'"{exe}" "{os.path.abspath(__file__)}"'


def autostart_command():
    """The command currently registered for startup, or None."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, APP_NAME)[0]
    except FileNotFoundError:
        return None


def is_autostart_enabled():
    return autostart_command() is not None


def set_autostart(enable):
    # CreateKey opens-or-creates, so a missing Run key (rare, but real) won't crash.
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if enable:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except FileNotFoundError:
                pass


# --- shared os probes ------------------------------------------------------
def get_idle_seconds():
    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    info = LASTINPUTINFO()
    info.cbSize = ctypes.sizeof(info)
    ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info))
    return (ctypes.windll.kernel32.GetTickCount() - info.dwTime) / 1000.0


def _exe_for_pid(pid):
    import win32process
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        h = None
        try:
            h = win32process.OpenProcess(0x0410, False, pid)
            return os.path.basename(win32process.GetModuleFileNameEx(h, 0))
        except Exception:
            return "(unknown)"
        finally:
            if h is not None:
                try:
                    import win32api
                    win32api.CloseHandle(h)
                except Exception:
                    pass


def _exe_path_for_pid(pid):
    """Full exe path for a pid (for relaunch), or '' if unavailable."""
    import win32process
    try:
        import psutil
        return psutil.Process(pid).exe()
    except Exception:
        h = None
        try:
            h = win32process.OpenProcess(0x0410, False, pid)
            return win32process.GetModuleFileNameEx(h, 0)
        except Exception:
            return ""
        finally:
            if h is not None:
                try:
                    import win32api
                    win32api.CloseHandle(h)
                except Exception:
                    pass


def get_active_app():
    import win32gui
    import win32process
    hwnd = win32gui.GetForegroundWindow()
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        exe = _exe_for_pid(pid)
    except Exception:
        exe = "(unknown)"
    return exe, title


def get_clipboard_text():
    import win32clipboard
    try:
        win32clipboard.OpenClipboard()
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        return None


_UIA_OK = None   # None=untried, True/False once we know if uiautomation imports


def get_window_text(max_chars=4000):
    """Visible text of the foreground window via UI Automation, or '' if unavailable.
    Lazy-imports uiautomation; returns '' (never raises) if the dep is missing/slow."""
    global _UIA_OK
    if _UIA_OK is False:
        return ""
    try:
        import uiautomation as auto
        _UIA_OK = True
        try:
            auto.SetGlobalSearchTimeout(1.0)   # don't let a slow COM tree stall the recorder
        except Exception:
            pass
        win = auto.GetForegroundControl()
        if not win:
            return ""
        parts, seen = [], 0
        # breadth-limited walk; bail early so we never stall the recorder thread
        stack = [win]
        steps = 0
        while stack and seen < max_chars and steps < 400:
            steps += 1
            node = stack.pop()
            try:
                t = (node.Name or "").strip()
                if t and len(t) > 1:
                    parts.append(t)
                    seen += len(t)
                stack.extend(node.GetChildren())
            except Exception:
                continue
        return " | ".join(dict.fromkeys(parts))[:max_chars]
    except ImportError:
        _UIA_OK = False
        return ""
    except Exception:
        return ""


# ===========================================================================
# MODULE 1: Quietlog - time journal
# ===========================================================================
def _sqlite(path):
    """Connect with WAL + a generous busy timeout so the recorder threads and the
    GUI reader can hit the same file without 'database is locked'."""
    if os.path.dirname(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    if path != ":memory:":
        try:
            con.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass
    return con


def ql_connect(path=DB_PATH):
    con = _sqlite(path)
    con.execute("CREATE TABLE IF NOT EXISTS samples "
                "(ts INTEGER, app TEXT, title TEXT, idle INTEGER)")
    return con


def aggregate_range(con, start, end, interval=SAMPLE_INTERVAL):
    """[(app, seconds), ...] busiest first, idle excluded, for [start, end)."""
    rows = con.execute(
        "SELECT app, COUNT(*) FROM samples WHERE ts >= ? AND ts < ? AND idle = 0 "
        "GROUP BY app ORDER BY COUNT(*) DESC", (start, end)).fetchall()
    return [(app, cnt * interval) for app, cnt in rows]


def aggregate_today(con, day=None, interval=SAMPLE_INTERVAL):
    if day is None:
        day = datetime.now().strftime("%Y-%m-%d")
    start = int(datetime.strptime(day, "%Y-%m-%d").timestamp())
    return aggregate_range(con, start, start + 86400, interval)


def aggregate_week(con, now=None, interval=SAMPLE_INTERVAL):
    now = now if now is not None else int(time.time())
    return aggregate_range(con, now - 7 * 86400, now, interval)


def focus_split(data, tags):
    """Sum seconds per tag from [(app, secs)] using {exe: tag}. Untagged = neutral.
    Returns {'work': s, 'distraction': s, 'neutral': s}. Pure logic."""
    out = {"work": 0, "distraction": 0, "neutral": 0}
    for app, secs in data:
        tag = tags.get(app, "neutral")
        out[tag if tag in out else "neutral"] += secs   # clamp stray/hand-edited tags
    return out


def fmt(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m}m" if h else (f"{m}m {s}s" if m else f"{s}s")


def build_help_html():
    mods = [
        ("Quietlog", "Time journal", "Logs which app you actually use (idle skipped). "
         "Open the <b>dashboard</b> to see Today / This week.", "on"),
        ("Tabreaper", "Window scenes", "Arrange your windows, then in the dashboard hit "
         "<b>Save 1</b>. Later <b>Restore 1</b> snaps them back.", "on"),
        ("Anchor", "Focus overlay", "When you start frantically flipping between windows, a calm "
         "screen fades in. Click it to carry on. Enable in Settings.", "off"),
        ("Localback", "Folder backup", "Pick a folder (<b>Choose backup folder</b>); every change "
         "is saved. <b>Restore a backup</b> brings any version back.", "off"),
        ("Threadkeeper", "Recall search", "Records window titles (clipboard is extra opt-in). "
         "<b>Search recall</b> to find &lsquo;what did that error say&rsquo;. Enable in Settings.", "off"),
    ]
    cards = "".join(
        f'<div class="card"><div class="h"><b>{n}</b> <span class="tag">{t}</span>'
        f'<span class="st {s}">{"default ON" if s=="on" else "opt-in"}</span></div>'
        f'<p>{d}</p></div>' for n, t, d, s in mods)
    return f"""<!doctype html><meta charset=utf-8><title>Quietlog Suite - guide</title>
<style>
 body{{font:16px system-ui,sans-serif;background:#11131a;color:#e6e9f0;max-width:760px;margin:40px auto;padding:0 20px}}
 h1{{font-size:24px;margin:0 0 2px}} .sub{{color:#8b93a7;margin:0 0 24px}}
 .card{{background:#1a1e29;border:1px solid #262b3a;border-radius:10px;padding:16px 18px;margin:12px 0}}
 .h{{display:flex;align-items:center;gap:10px;margin-bottom:6px}} .h b{{font-size:17px}}
 .tag{{color:#8b93a7;font-size:14px}} .st{{margin-left:auto;font-size:12px;padding:2px 9px;border-radius:20px}}
 .st.on{{background:#16351f;color:#5fd17e}} .st.off{{background:#2a2320;color:#d9a85f}}
 p{{margin:0;color:#c7ccd9;line-height:1.5}} .foot{{margin-top:24px;color:#5b6377;font-size:13px}}
 .tip{{background:#16203a;border-radius:8px;padding:12px 16px;color:#aebbe0}}
</style>
<h1>Quietlog Suite</h1><p class="sub">5 private, local-only tools in your tray. Nothing leaves this PC.</p>
<p class="tip">Find the tray icon bottom-right (maybe under the <b>^</b> arrow). <b>Right-click</b> it
for everything. Turn modules on/off under <b>Settings</b>. Colored icon = recording.</p>
{cards}
<p class="foot">Reopen this guide any time: tray &rarr; Help / Guide.</p>"""


def show_help():
    open_html(build_help_html())


def open_html(html):
    f = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    f.write(html)
    f.close()
    webbrowser.open("file://" + f.name)


class BgModule:
    """Base for background modules. Each start() spawns a thread with its OWN stop
    Event, so a quick stop()->start() can't leak the old thread (a shared Event's
    clear() would otherwise un-signal a still-running worker)."""
    def __init__(self):
        self._stop = threading.Event()
        self._stop.set()                 # idle until started

    def start(self):
        self.stop()                      # signal any prior worker to exit
        self._stop = threading.Event()   # fresh event for the new worker
        threading.Thread(target=self._loop, args=(self._stop,), daemon=True).start()

    def stop(self):
        self._stop.set()

    def menu_items(self, Item):
        return []


class Quietlog(BgModule):
    name, label = "quietlog", "Quietlog (time journal)"

    def _loop(self, stop):
        con = ql_connect()
        prune_old(con, "samples")          # trim history on startup
        try:
            while not stop.is_set():
                try:
                    ts = int(time.time())
                    if get_idle_seconds() > IDLE_THRESHOLD:
                        con.execute("INSERT INTO samples VALUES (?,?,?,?)", (ts, IDLE_LABEL, "", 1))
                    else:
                        exe, title = get_active_app()
                        con.execute("INSERT INTO samples VALUES (?,?,?,?)", (ts, exe, title, 0))
                    con.commit()
                except Exception as e:               # one bad cycle must not kill the thread
                    _dbg(f"quietlog loop error: {e!r}")
                stop.wait(SAMPLE_INTERVAL)
        finally:
            con.close()
    # today's stats live in the dashboard; no tray actions (base menu_items=[])


# ===========================================================================
# MODULE 2: Tabreaper - save/restore window layouts
# ===========================================================================
def list_windows():
    """[(hwnd, title, exe, rect, path), ...] for real top-level windows."""
    import win32gui
    import win32process
    out = []

    own = ("Quietlog Suite", "Localback -", "Threadkeeper -")

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or title.startswith(own):   # don't capture our own windows
            return
        rect = win32gui.GetWindowRect(hwnd)
        if rect[2] - rect[0] < 80 or rect[3] - rect[1] < 60:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            exe = _exe_for_pid(pid)
            path = _exe_path_for_pid(pid)
        except Exception:
            exe, path = "(unknown)", ""
        out.append((hwnd, title, exe, rect, path))

    win32gui.EnumWindows(cb, None)
    return out


def match_window(saved, candidates):
    """Pick the candidate window best matching a saved {exe,title}. Pure logic.

    candidates: list of (hwnd, title, exe, rect). Returns hwnd or None.
    Prefer same exe AND exact title, then same exe + title prefix, then exe only.
    """
    se, st = saved["exe"], saved["title"]
    same_exe = [c for c in candidates if c[2] == se]
    for c in same_exe:
        if c[1] == st:
            return c[0]
    for c in same_exe:
        if c[1][:20] == st[:20]:
            return c[0]
    return same_exe[0][0] if same_exe else None


class Tabreaper:
    name, label = "tabreaper", "Tabreaper (window scenes)"

    def __init__(self):
        self.scenes = self._load()

    def start(self):
        pass  # purely on-demand

    def stop(self):
        pass

    def _load(self):
        try:
            with open(SCENES_PATH, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self):
        save_json(self.scenes, SCENES_PATH)

    def save_scene(self, slot):
        wins = [{"title": t, "exe": e, "rect": list(r), "path": p}
                for _, t, e, r, p in list_windows()]
        self.scenes[str(slot)] = wins
        self._save()
        notify(f"Saved {len(wins)} windows to Slot {slot}.", "Tabreaper")

    def _place(self, hwnd, rect):
        import win32gui
        import win32con
        l, t, r, b = rect
        try:
            if win32gui.IsIconic(hwnd) or win32gui.IsZoomed(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.MoveWindow(hwnd, l, t, r - l, b - t, True)
            return True
        except Exception:
            return False

    def restore_scene(self, slot):
        saved = self.scenes.get(str(slot))
        if not saved:
            notify(f"Slot {slot} is empty - save a layout there first.", "Tabreaper")
            return
        cands = list_windows()
        used = set()
        moved = 0
        missing = []           # saved windows with no open match -> candidates to relaunch
        for w in saved:
            hwnd = match_window(w, [c for c in cands if c[0] not in used])
            if hwnd:
                used.add(hwnd)
                if self._place(hwnd, w["rect"]):
                    moved += 1
            elif w.get("path") and os.path.exists(w["path"]):
                missing.append(w)

        relaunched = 0
        for path in {w["path"] for w in missing}:   # dedup: one launch per exe
            try:
                os.startfile(path)          # launch closed app; reposition shortly after
                relaunched += 1
            except Exception:
                pass
        if missing:
            # give apps a moment to open, then position the new windows
            def reposition():
                fresh = {c[0] for c in list_windows()} - {c[0] for c in cands}
                later = list_windows()
                seen = set()
                for w in missing:
                    hwnd = match_window(w, [c for c in later
                                            if c[0] in fresh and c[0] not in seen])
                    if hwnd:
                        seen.add(hwnd)
                        self._place(hwnd, w["rect"])
            run_on_gui(lambda: _GUI_ROOT.after(2500, reposition))

        msg = f"Restored {moved} of {len(saved)} windows from Slot {slot}."
        if relaunched:
            msg += f" Relaunched {relaunched} closed app(s)."
        notify(msg, "Tabreaper")

    def menu_items(self, Item):
        import pystray
        # factory: pystray counts a callable's arg slots (defaults included), so a
        # 3-arg `lambda i,t,n=n` is rejected. Return a clean 2-arg callable instead.
        def act(method, n):
            return lambda icon, item: method(n)
        save = pystray.Menu(*[Item(f"Slot {n}", act(self.save_scene, n)) for n in (1, 2, 3)])
        restore = pystray.Menu(*[Item(f"Slot {n}", act(self.restore_scene, n)) for n in (1, 2, 3)])
        vis = lambda i: SETTINGS["tabreaper"]
        return [Item("Tabreaper: Save layout", save, visible=vis),
                Item("Tabreaper: Restore layout", restore, visible=vis)]


# ===========================================================================
# MODULE 3: Anchor - anti-thrash focus overlay
# ===========================================================================
def show_overlay():
    """Full-screen translucent nudge; click/key to dismiss. Built on the GUI thread
    as a Toplevel (no own root/mainloop) so it can't race other windows."""
    def build():
        import tkinter as tk
        win = tk.Toplevel(_GUI_ROOT)
        win.attributes("-fullscreen", True)
        win.attributes("-alpha", 0.82)
        win.attributes("-topmost", True)
        win.configure(bg="#0e0f15")
        tk.Label(win, text="You're switching a lot.\n\nBreathe. Click to continue.",
                 fg="#e6e9f0", bg="#0e0f15", font=(FONT_HEAD, 28)).pack(expand=True)
        win.bind("<Button-1>", lambda e: win.destroy())
        win.bind("<Key>", lambda e: win.destroy())
        win.focus_force()
        win.after(8000, lambda: win.winfo_exists() and win.destroy())  # guard: may already be gone
    run_on_gui(build)


class Anchor(BgModule):
    name, label = "anchor", "Anchor (focus overlay)"

    def _loop(self, stop):
        import win32gui
        switches = deque()
        last_hwnd = None
        last_fire = 0.0
        while not stop.is_set():
            try:
                hwnd = win32gui.GetForegroundWindow()
            except Exception:
                hwnd = None
            now = time.time()
            if hwnd and hwnd != last_hwnd:
                last_hwnd = hwnd
                switches.append(now)
            while switches and now - switches[0] > ANCHOR_WINDOW:
                switches.popleft()
            threshold = ANCHOR_LEVELS.get(SETTINGS.get("anchor_sensitivity"), 12)
            if (len(switches) >= threshold
                    and now - last_fire > ANCHOR_COOLDOWN
                    and now >= SETTINGS.get("anchor_snooze_until", 0)
                    and get_idle_seconds() < 5):
                last_fire = now
                switches.clear()
                show_overlay()
            stop.wait(1)

    def snooze(self, minutes=30):
        SETTINGS["anchor_snooze_until"] = int(time.time()) + minutes * 60
        save_settings(SETTINGS)
        notify(f"Anchor snoozed {minutes} min.", "Anchor")

    def menu_items(self, Item):
        return [Item("Anchor: Snooze 30 min", lambda i, t: self.snooze(),
                     visible=lambda i: SETTINGS["anchor"])]


# ===========================================================================
# MODULE 4: Localback - auto-versioned folder backup
# ===========================================================================
BACKUP_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".cache"}


def scan_files(watch, store_root=BACKUP_DIR, skip=BACKUP_SKIP_DIRS):
    """Yield file paths under `watch`, skipping junk dirs and the backup store."""
    for root, dirs, files in os.walk(watch):
        dirs[:] = [d for d in dirs
                   if d not in skip and os.path.join(root, d) != store_root]
        for fn in files:
            yield os.path.join(root, fn)


def file_hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def version_name(ts, digest, ext):
    """Human-readable snapshot filename (date_time_hash). Pure logic (testable)."""
    stamp = datetime.fromtimestamp(ts).strftime("%Y-%m-%d_%H%M%S")
    return f"{stamp}_{digest[:8]}{ext}"


def prune_versions(dest_dir, keep=BACKUP_KEEP):
    """Delete oldest snapshots beyond `keep`, ordering by file mtime (robust to a
    wall clock that moved backward), tie-broken by name. Returns count removed."""
    try:
        entries = list(os.scandir(dest_dir))
    except OSError:
        return 0
    try:
        entries.sort(key=lambda e: (e.stat().st_mtime, e.name))
    except OSError:
        entries.sort(key=lambda e: e.name)
    extra = entries[:-keep] if len(entries) > keep else []
    for e in extra:
        try:
            os.remove(e.path)
        except OSError:
            pass
    return len(extra)


def list_backups(store_root=BACKUP_DIR):
    """{relpath: [version filenames, newest first]} for every backed-up file."""
    out = {}
    if not os.path.isdir(store_root):
        return out
    for root, _, files in os.walk(store_root):
        if files:
            out[os.path.relpath(root, store_root)] = sorted(files, reverse=True)
    return out


def version_label(fname):
    """'2026-06-19_180327_aaf4c61d.txt' -> '2026-06-19 18:03:27'. Pure logic."""
    p = fname.split("_")
    if len(p) >= 2 and len(p[1]) >= 6 and p[1][:6].isdigit():
        return f"{p[0]} {p[1][:2]}:{p[1][2:4]}:{p[1][4:6]}"
    return fname


def _force_preserve(src, watch_root, store_root):
    """Copy src into the store even past the size cap, unless its content is already
    stored. Used before a restore so the live file is never lost."""
    try:
        digest = file_hash(src)
    except OSError:
        return
    dest_dir = os.path.join(store_root, os.path.relpath(src, watch_root))
    os.makedirs(dest_dir, exist_ok=True)
    if any(digest[:8] in f for f in os.listdir(dest_dir)):
        return                                   # already safely stored
    ext = os.path.splitext(src)[1]
    shutil.copy2(src, os.path.join(dest_dir, version_name(int(time.time()), digest, ext)))


def restore_version(rel, fname, watch_root, store_root=BACKUP_DIR):
    """Copy a stored snapshot back to its original location. Returns dest path.
    Preserves the current file first so the restore is itself undoable (no data loss,
    even for files over the snapshot size cap)."""
    if not watch_root:
        raise ValueError("no backup folder set")   # never write to CWD
    src = os.path.join(store_root, rel, fname)
    dest = os.path.join(watch_root, rel)
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    if os.path.exists(dest):
        if snapshot_file(dest, watch_root, store_root, int(time.time())) is None:
            _force_preserve(dest, watch_root, store_root)   # cap/dup-skip: preserve anyway
    shutil.copy2(src, dest)
    return dest


def snapshot_file(src, watch_root, store_root, now):
    """Copy src into the version store unless an identical hash already exists.

    Returns the snapshot path if stored, else None (duplicate / too big / error).
    """
    try:
        if os.path.getsize(src) > BACKUP_MAX_MB * 1024 * 1024:
            return None
        digest = file_hash(src)
    except OSError:
        return None
    rel = os.path.relpath(src, watch_root)
    dest_dir = os.path.join(store_root, rel)
    os.makedirs(dest_dir, exist_ok=True)
    if any(digest[:8] in f for f in os.listdir(dest_dir)):
        return None  # already have this exact content
    ext = os.path.splitext(src)[1]
    dest = os.path.join(dest_dir, version_name(now, digest, ext))
    shutil.copy2(src, dest)
    prune_versions(dest_dir)
    return dest


class Localback(BgModule):
    name, label = "localback", "Localback (folder backup)"

    def start(self):
        if not SETTINGS.get("localback_folder"):
            return                       # nothing to watch yet
        super().start()

    def _loop(self, stop):
        watch = SETTINGS.get("localback_folder")
        if not watch or not os.path.isdir(watch):
            return
        seen = {}  # path -> mtime
        while not stop.is_set():
            try:
                for p in scan_files(watch):
                    try:
                        mt = os.path.getmtime(p)
                    except OSError:
                        continue
                    if seen.get(p) != mt:
                        seen[p] = mt
                        snapshot_file(p, watch, BACKUP_DIR, int(time.time()))
            except Exception as e:
                _dbg(f"localback loop error: {e!r}")
            stop.wait(BACKUP_INTERVAL)

    def choose_folder(self):
        def build():
            from tkinter import filedialog
            _init_ctk()
            folder = filedialog.askdirectory(title="Quietlog: folder to back up",
                                             parent=_GUI_ROOT)
            if folder:
                SETTINGS["localback_folder"] = folder
                save_settings(SETTINGS)
                self.stop()
                if SETTINGS["localback"]:
                    self.start()
                notify(f"Now backing up: {folder}", "Localback")
        run_on_gui(build)

    def restore_dialog(self):
        """Pick a file -> pick a dated version -> restore it. Native ctk window."""
        def build():
            try:
                backups = list_backups()
                watch = SETTINGS.get("localback_folder", "")
                ctk, root = gui_window("Localback - restore a version", "620x460")
                if not backups:
                    ctk.CTkLabel(root, text="No backups yet. Choose a folder and edit a file.",
                                 font=(FONT_UI, 13)).pack(expand=True)
                    return
                rels = sorted(backups)
                ctk.CTkLabel(root, text="File:", font=(FONT_UI, 12)).pack(anchor="w", padx=12, pady=(12, 0))
                listing = ctk.CTkScrollableFrame(root)
                listing.pack(fill="both", expand=True, padx=12, pady=8)

                def show_versions(rel):
                    for c in listing.winfo_children():
                        c.destroy()
                    for fname in backups[rel]:
                        row = ctk.CTkFrame(listing, fg_color="transparent")
                        row.pack(fill="x", pady=2)
                        ctk.CTkLabel(row, text=version_label(fname),
                                     font=(FONT_UI, 12)).pack(side="left")

                        def do_restore(f=fname, r=rel):
                            try:
                                dest = restore_version(r, f, watch)
                                notify(f"Restored {r} ({version_label(f)})", "Localback")
                            except Exception as e:
                                notify(f"Restore failed: {e}", "Localback")
                        ctk.CTkButton(row, text="Restore", width=90,
                                      command=do_restore).pack(side="right")

                menu = ctk.CTkOptionMenu(root, values=rels, command=show_versions)
                menu.pack(fill="x", padx=12)
                show_versions(rels[0])
            except Exception as e:
                _dbg(f"restore_dialog error: {e!r}")
                notify(f"Restore window failed: {e}", "Localback")
        run_on_gui(build)

    def menu_items(self, Item):
        vis = lambda i: SETTINGS["localback"]
        return [
            Item("Localback: Choose folder...", lambda i, t: self.choose_folder(), visible=vis),
            Item("Localback: Restore a version...", lambda i, t: self.restore_dialog(), visible=vis),
            Item("Localback: Open backups", lambda i, t: os.startfile(BACKUP_DIR)
                 if os.path.isdir(BACKUP_DIR) else None, visible=vis),
        ]


# ===========================================================================
# MODULE 5: Threadkeeper - searchable recall (titles + clipboard)
# ponytail: indexes window titles (+ clipboard if opted in), not full window text.
#           Upgrade path: add `uiautomation` to scrape visible control text.
# ===========================================================================
def tk_connect(path=DB_PATH):
    con = _sqlite(path)
    try:
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS recall "
                    "USING fts5(content, kind, ts UNINDEXED)")
    except sqlite3.OperationalError:           # FTS5 not compiled in -> plain table
        con.execute("CREATE TABLE IF NOT EXISTS recall (content TEXT, kind TEXT, ts INTEGER)")
    return con


def tk_search(con, query, limit=50):
    try:                                       # FTS5 path
        rows = con.execute(
            "SELECT content, kind, ts FROM recall WHERE recall MATCH ? "
            "ORDER BY ts DESC LIMIT ?", (query, limit)).fetchall()
        return rows
    except sqlite3.OperationalError:           # plain table -> LIKE
        pass
    like = f"%{query}%"
    return con.execute("SELECT content, kind, ts FROM recall WHERE content LIKE ? "
                       "ORDER BY ts DESC LIMIT ?", (like, limit)).fetchall()


class Threadkeeper(BgModule):
    name, label = "threadkeeper", "Threadkeeper (recall)"

    def _loop(self, stop):
        con = tk_connect()
        prune_old(con, "recall")           # trim history on startup
        cap_recall(con)
        last_title, last_clip, last_text = None, None, None
        cycles = 0
        try:
            while not stop.is_set():
                try:
                    cycles += 1
                    if cycles % 360 == 0:          # ~hourly: re-trim while long-running
                        prune_old(con, "recall")
                        cap_recall(con)
                    if get_idle_seconds() < IDLE_THRESHOLD:
                        _, title = get_active_app()
                        if title and title != last_title:
                            last_title = title
                            con.execute("INSERT INTO recall VALUES (?,?,?)",
                                        (title, "window", int(time.time())))
                        # clipboard is extra opt-in: it can capture passwords/secrets
                        if SETTINGS.get("threadkeeper_clipboard"):
                            clip = get_clipboard_text()
                            if clip and clip != last_clip and len(clip) < 10000:
                                last_clip = clip
                                con.execute("INSERT INTO recall VALUES (?,?,?)",
                                            (clip, "clipboard", int(time.time())))
                        # full window text is extra opt-in: heavy + reads everything on screen
                        if SETTINGS.get("threadkeeper_fulltext"):
                            text = get_window_text()
                            if text and text != last_text:
                                last_text = text
                                con.execute("INSERT INTO recall VALUES (?,?,?)",
                                            (text, "screen", int(time.time())))
                        con.commit()
                except Exception as e:               # one bad cycle must not kill the thread
                    _dbg(f"threadkeeper loop error: {e!r}")
                stop.wait(THREAD_INTERVAL)
        finally:
            con.close()

    def search_dialog(self):
        def build():
            try:
                con = tk_connect()
                ctk, root = gui_window("Threadkeeper - recall search", "680x460")
                entry = ctk.CTkEntry(root, placeholder_text="Search what you saw / copied...",
                                     font=(FONT_UI, 14))
                entry.pack(fill="x", padx=12, pady=12)
                entry.focus_set()
                out = ctk.CTkTextbox(root, font=(FONT_MONO, 12), wrap="word")
                out.pack(fill="both", expand=True, padx=12, pady=(0, 12))

                def do_search(_=None):
                    out.delete("1.0", "end")
                    q = entry.get().strip()
                    if not q:
                        return
                    rows = tk_search(con, q)
                    if not rows:
                        out.insert("end", "No matches.")
                    for content, kind, ts in rows:
                        when = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
                        out.insert("end", f"[{when}] ({kind}) {content}\n\n")
                entry.bind("<Return>", do_search)
                root.protocol("WM_DELETE_WINDOW",
                              lambda: (con.close(), root.destroy()))
            except Exception as e:
                _dbg(f"search_dialog error: {e!r}")
                notify(f"Search window failed: {e}", "Threadkeeper")
        run_on_gui(build)

    def menu_items(self, Item):
        return [Item("Threadkeeper: Search...", lambda i, t: self.search_dialog(),
                     visible=lambda i: SETTINGS["threadkeeper"])]


# ===========================================================================
# Tray shell
# ===========================================================================
MODULES = []


def make_icon_image(active=True):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (64, 64), "#11131a")
    d = ImageDraw.Draw(img)
    c = ("#4f7cff", "#8b5cf6", "#e6e9f0") if active else ("#3a3f4d", "#4a4f5d", "#5a606e")
    d.rectangle([14, 34, 24, 50], fill=c[0])
    d.rectangle([28, 22, 38, 50], fill=c[1])
    d.rectangle([42, 14, 52, 50], fill=c[2])
    return img


def apply_enabled(mod):
    """Start or stop a module to match its setting."""
    mod.stop()
    if SETTINGS.get(mod.name):
        mod.start()


def module_by_name(name):
    return next((m for m in MODULES if m.name == name), None)


# modules that passively record; tray icon greys out when none are on
RECORDING_MODULES = ("quietlog", "threadkeeper")


def refresh_icon():
    """Grey the tray icon when nothing is being recorded; colored otherwise."""
    if _ICON:
        active = any(SETTINGS.get(n) for n in RECORDING_MODULES)
        _ICON.icon = make_icon_image(active=active)


def set_module_enabled(name, val, prompt_folder=True):
    """Toggle a module on/off, persist, start/stop it. Shared by tray + dashboard."""
    SETTINGS[name] = bool(val)
    save_settings(SETTINGS)
    m = module_by_name(name)
    if m:
        apply_enabled(m)
    refresh_icon()
    if name == "localback" and val and not SETTINGS["localback_folder"] and prompt_folder and m:
        m.choose_folder()


# --- native dashboard window (customtkinter) -------------------------------
_DASH_OPEN = False
_CTK_INIT = False


def _init_ctk():
    """Import customtkinter once with safe settings. Disables the automatic DPI
    tracker, whose periodic callback reaches across threads."""
    global _CTK_INIT
    import customtkinter as ctk
    if not _CTK_INIT:
        try:
            ctk.deactivate_automatic_dpi_awareness()
        except Exception:
            pass
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        _CTK_INIT = True
    return ctk


# All customtkinter windows must live on ONE thread: ctk keeps global window
# dicts that race ("dict changed size during iteration") if CTk roots are made
# on multiple threads. So: one hidden root + one mainloop on a dedicated thread,
# every window a CTkToplevel built via a thread-safe queue.
import queue as _queue
_GUI_ROOT = None
_GUI_READY = threading.Event()
_GUI_Q = _queue.Queue()
_GUI_START_LOCK = threading.Lock()
_GUI_STARTED = False


def _gui_main():
    global _GUI_ROOT
    ctk = _init_ctk()
    _GUI_ROOT = ctk.CTk()
    _GUI_ROOT.withdraw()                       # hidden master window
    _resolve_fonts()                           # needs a root to exist first

    def pump():
        # drain everything queued; one bad callback must not skip the rest
        while True:
            try:
                fn = _GUI_Q.get_nowait()
            except _queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                _dbg(f"gui callback error: {e!r}")
        _GUI_ROOT.after(50, pump)

    pump()
    _GUI_READY.set()
    _GUI_ROOT.mainloop()


def run_on_gui(fn):
    """Schedule fn to build/run on the single GUI thread. Starts it on first use.
    Start is serialized so concurrent callers can never spawn two CTk roots."""
    global _GUI_STARTED
    with _GUI_START_LOCK:
        if not _GUI_STARTED:
            _GUI_STARTED = True
            threading.Thread(target=_gui_main, daemon=True).start()
    if not _GUI_READY.wait(5):
        _dbg("run_on_gui: GUI thread not ready after 5s")
        return False
    _GUI_Q.put(fn)
    return True


def gui_window(title, geometry):
    """Make a themed CTkToplevel on the GUI thread (caller already on it)."""
    ctk = _init_ctk()
    win = ctk.CTkToplevel(_GUI_ROOT)
    win.title(title)
    win.geometry(geometry)
    try:
        from PIL import ImageTk
        win._icon = ImageTk.PhotoImage(make_icon_image())
        win.iconphoto(True, win._icon)
    except Exception:
        pass
    win.lift()
    win.attributes("-topmost", True)
    win.after(600, lambda: win.attributes("-topmost", False))
    return ctk, win


def gui_confirm(parent, title, message):
    """Modal yes/no on the GUI thread (caller already on it). Returns bool."""
    from tkinter import messagebox
    return messagebox.askyesno(title, message, parent=parent, icon="warning")


def open_dashboard():
    """One real app window (customtkinter), scrollable, built on the GUI thread."""
    def build():
        global _DASH_OPEN
        # guard + flag both run on the single GUI thread => no cross-thread race
        if _DASH_OPEN:
            return
        try:
            ctk, root = gui_window("Quietlog Suite", "600x700")
            _DASH_OPEN = True

            def on_close():
                global _DASH_OPEN
                _DASH_OPEN = False
                root.destroy()
            root.protocol("WM_DELETE_WINDOW", on_close)

            def label(parent, text, size=13, bold=False, **kw):
                fam = FONT_HEAD if bold else FONT_UI
                return ctk.CTkLabel(parent, text=text,
                                    font=(fam, size, "bold" if bold else "normal"))

            def button(parent, text, cmd, w=160):
                return ctk.CTkButton(parent, text=text, command=cmd, width=w,
                                     font=(FONT_UI, 13))

            root.minsize(540, 600)   # below this, rows clip

            # fixed footer (always visible, outside the scroll area)
            footer = ctk.CTkFrame(root, height=52)
            footer.pack(side="bottom", fill="x")
            footer.pack_propagate(False)

            def do_quit():
                on_close()
                quit_app()
            ctk.CTkButton(footer, text="Quit Quietlog Suite", command=do_quit, width=180,
                          fg_color="#7a2630", hover_color="#9a3340",
                          font=(FONT_UI, 13)).pack(side="right", padx=12, pady=8)

            host = ctk.CTkScrollableFrame(root, fg_color="transparent")
            host.pack(fill="both", expand=True)

            # header
            label(host, "Quietlog Suite", size=20, bold=True).pack(anchor="w", padx=16, pady=(12, 0))
            label(host, "5 private, local-only tools. Nothing leaves this PC.",
                  size=11).pack(anchor="w", padx=16)

            # activity: Today / This week
            head = ctk.CTkFrame(host, fg_color="transparent")
            head.pack(fill="x", padx=16, pady=(16, 2))
            title_lbl = label(head, "Today", size=15, bold=True)
            title_lbl.pack(side="left")
            seg = ctk.CTkSegmentedButton(head, values=["Today", "Week"])
            seg.set("Today")
            seg.pack(side="right")
            stats_box = ctk.CTkFrame(host, fg_color="transparent")
            stats_box.pack(fill="x", padx=16)

            TAG_NEXT = {"neutral": "work", "work": "distraction", "distraction": "neutral"}
            TAG_COLOR = {"work": "#5fd17e", "distraction": "#d9a85f", "neutral": "#5b6377"}

            def render_stats(_=None):
                which = seg.get()
                title_lbl.configure(text="This week" if which == "Week" else "Today")
                for c in stats_box.winfo_children():
                    c.destroy()
                con = ql_connect()
                data = (aggregate_week(con) if which == "Week" else aggregate_today(con))[:8]
                con.close()
                if not data:
                    label(stats_box, "No activity logged yet - use your PC for a bit.").pack(anchor="w")
                    return
                total = sum(s for _, s in data) or 1
                tags = SETTINGS.get("app_tags", {})

                # focus summary line (tap a tag dot on a row to classify that app)
                split = focus_split(data, tags)
                stot = sum(split.values()) or 1
                summary = (f"Focus: {split['work']/stot*100:.0f}% work  -  "
                           f"{split['distraction']/stot*100:.0f}% distraction  -  "
                           f"{split['neutral']/stot*100:.0f}% neutral")
                label(stats_box, summary, size=11, bold=True).grid(
                    row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))

                # name(+tag toggle) | bar (fills) | time
                stats_box.grid_columnconfigure(0, weight=0)
                stats_box.grid_columnconfigure(1, weight=1)
                stats_box.grid_columnconfigure(2, weight=0)
                for i, (app, secs) in enumerate(data, start=1):
                    tag = tags.get(app, "neutral")
                    if tag not in TAG_COLOR:
                        tag = "neutral"

                    def cycle(a=app):
                        t = SETTINGS.setdefault("app_tags", {})
                        cur = t.get(a, "neutral")
                        t[a] = TAG_NEXT.get(cur, "work")
                        save_settings(SETTINGS)
                        render_stats()
                    dot = ctk.CTkButton(stats_box, text="●", width=22, height=22,
                                        fg_color="transparent", hover_color="#1d2130",
                                        text_color=TAG_COLOR[tag], font=(FONT_UI, 14),
                                        command=cycle)
                    dot.grid(row=i, column=0, sticky="w")
                    label(stats_box, app[:22], size=11).grid(row=i, column=0, sticky="w", padx=(28, 0), pady=2)
                    bar = ctk.CTkProgressBar(stats_box)
                    bar.set(secs / total)
                    bar.grid(row=i, column=1, sticky="we", padx=10, pady=2)
                    label(stats_box, fmt(secs), size=11).grid(row=i, column=2, sticky="e", pady=2)

            seg.configure(command=render_stats)
            render_stats()
            row = ctk.CTkFrame(host, fg_color="transparent")
            row.pack(fill="x", padx=16, pady=(8, 0))
            button(row, "Refresh", render_stats, w=90).pack(side="left")
            label(row, "tap the dot to tag an app  work / distraction / neutral",
                  size=10).pack(side="left", padx=10)

            # modules
            label(host, "Modules", size=15, bold=True).pack(anchor="w", padx=16, pady=(16, 2))
            mod_box = ctk.CTkFrame(host, fg_color="transparent")
            mod_box.pack(fill="x", padx=16)
            for m in MODULES:
                rowf = ctk.CTkFrame(mod_box, fg_color="transparent")
                rowf.pack(fill="x", pady=3)
                sw = ctk.CTkSwitch(rowf, text=m.label)
                if SETTINGS.get(m.name):
                    sw.select()

                def on_switch(mm=m, s=sw):
                    set_module_enabled(mm.name, bool(s.get()))
                sw.configure(command=on_switch)
                sw.pack(side="left")

                # Anchor row gets a labelled sensitivity selector inline
                if m.name == "anchor":
                    label(rowf, "sensitivity", size=10).pack(side="left", padx=(10, 4))
                    sens = ctk.CTkSegmentedButton(rowf, values=["low", "medium", "high"])
                    sens.set(SETTINGS.get("anchor_sensitivity", "medium"))

                    def on_sens(val):
                        SETTINGS["anchor_sensitivity"] = val
                        save_settings(SETTINGS)
                    sens.configure(command=on_sens)
                    sens.pack(side="right")

            tab = module_by_name("tabreaper")
            lb = module_by_name("localback")
            tkm = module_by_name("threadkeeper")

            # actions gated by their module's enabled state (matches the tray menu)
            def gated(name, fn):
                def run():
                    if not SETTINGS.get(name):
                        notify(f"Enable {name.capitalize()} first (toggle above).", "Quietlog")
                        return
                    fn()
                return run

            # actions
            label(host, "Actions", size=15, bold=True).pack(anchor="w", padx=16, pady=(16, 2))
            label(host, "Window layout (Tabreaper) - save/restore your open windows",
                  size=10).pack(anchor="w", padx=16)
            slots = ctk.CTkFrame(host, fg_color="transparent")
            slots.pack(fill="x", padx=16, pady=(2, 6))
            label(slots, "Save").pack(side="left", padx=(0, 4))
            for n in (1, 2, 3):
                button(slots, str(n), gated("tabreaper", lambda n=n: tab.save_scene(n)), w=36).pack(side="left", padx=2)
            label(slots, "Restore").pack(side="left", padx=(14, 4))

            def restore_slot(n):
                if gui_confirm(root, "Restore layout",
                               f"Move your open windows to the Slot {n} layout?"):
                    tab.restore_scene(n)
            for n in (1, 2, 3):
                button(slots, str(n), gated("tabreaper", lambda n=n: restore_slot(n)), w=36).pack(side="left", padx=2)

            # other actions: clean 2-col grid, equal widths
            act = ctk.CTkFrame(host, fg_color="transparent")
            act.pack(fill="x", padx=16)
            act.grid_columnconfigure(0, weight=1, uniform="a")
            act.grid_columnconfigure(1, weight=1, uniform="a")

            def grid_btn(text, cmd, r, c):
                button(act, text, cmd).grid(row=r, column=c, padx=4, pady=4, sticky="we")

            grid_btn("Choose backup folder", gated("localback", lambda: lb.choose_folder()), 0, 0)
            grid_btn("Restore a backup", gated("localback", lambda: lb.restore_dialog()), 0, 1)
            grid_btn("Search recall", gated("threadkeeper", lambda: tkm.search_dialog()), 1, 0)
            grid_btn("Help / Guide", show_help, 1, 1)

            # privacy / data: wipe what's been recorded (confirm-gated)
            label(host, "Privacy / Data", size=15, bold=True).pack(anchor="w", padx=16, pady=(16, 2))
            label(host, f"History older than {RETAIN_DAYS} days is auto-deleted. Wipe now:",
                  size=10).pack(anchor="w", padx=16)
            pz = ctk.CTkFrame(host, fg_color="transparent")
            pz.pack(fill="x", padx=16, pady=(2, 0))
            pz.grid_columnconfigure(0, weight=1, uniform="b")
            pz.grid_columnconfigure(1, weight=1, uniform="b")

            def clear(table, human):
                if gui_confirm(root, "Clear data", f"Permanently delete all {human}? This cannot be undone."):
                    n = clear_table(table)
                    notify(f"Cleared {n} {human} rows.", "Quietlog")
            button(pz, "Clear usage history", lambda: clear("samples", "usage")).grid(
                row=0, column=0, padx=4, pady=4, sticky="we")
            button(pz, "Clear recall history", lambda: clear("recall", "recall")).grid(
                row=0, column=1, padx=4, pady=4, sticky="we")

            # status
            label(host, "Status", size=15, bold=True).pack(anchor="w", padx=16, pady=(16, 2))
            auto = "on" if is_autostart_enabled() else "off"
            wf = SETTINGS.get("localback_folder") or "(none chosen)"
            clip = "on" if SETTINGS.get("threadkeeper_clipboard") else "off"
            label(host, f"Run on Windows startup: {auto}   |   Clipboard capture: {clip}",
                  size=11).pack(anchor="w", padx=16)
            label(host, f"Backup folder: {wf}", size=11).pack(anchor="w", padx=16)
            label(host, f"v{APP_VERSION}  -  data in {DB_DIR}", size=10).pack(anchor="w", padx=16, pady=(2, 12))
            _dbg("dashboard built")

        except Exception as e:
            _dbg(f"dashboard error: {e!r}")
            notify(f"Dashboard failed: {e}", "Quietlog")
            _DASH_OPEN = False

    run_on_gui(build)


def run_tray():
    global MODULES
    import pystray
    from pystray import Menu, MenuItem as Item
    _dbg("run_tray: pystray imported")

    first_run = not os.path.exists(SETTINGS_PATH)
    save_settings(SETTINGS)   # create the file so first_run is only true once
    if first_run and not is_autostart_enabled():
        set_autostart(True)   # run on Windows startup by default; toggle off in the menu
    elif is_autostart_enabled() and autostart_command() != _launch_command():
        set_autostart(True)   # exe/script moved -> refresh the stale registry path

    MODULES = [Quietlog(), Tabreaper(), Anchor(), Localback(), Threadkeeper()]
    by_name = {m.name: m for m in MODULES}
    for m in MODULES:
        apply_enabled(m)

    def toggle_module(mod):
        def handler(icon, item):
            set_module_enabled(mod.name, not SETTINGS.get(mod.name))
            icon.update_menu()
        return handler

    def toggle_autostart(icon, item):
        set_autostart(not is_autostart_enabled())

    def toggle_clipboard(icon, item):
        SETTINGS["threadkeeper_clipboard"] = not SETTINGS.get("threadkeeper_clipboard")
        save_settings(SETTINGS)

    def toggle_fulltext(icon, item):
        SETTINGS["threadkeeper_fulltext"] = not SETTINGS.get("threadkeeper_fulltext")
        save_settings(SETTINGS)

    settings_menu = Menu(
        *[Item(m.label, toggle_module(m), checked=lambda i, m=m: SETTINGS.get(m.name))
          for m in MODULES],
        Menu.SEPARATOR,
        Item("Threadkeeper: also save clipboard", toggle_clipboard,
             checked=lambda i: SETTINGS.get("threadkeeper_clipboard")),
        Item("Threadkeeper: also save window text", toggle_fulltext,
             checked=lambda i: SETTINGS.get("threadkeeper_fulltext")),
    )

    action_items = []
    for m in MODULES:
        action_items += m.menu_items(Item)

    menu = Menu(
        Item("Open dashboard", lambda i, t: open_dashboard(), default=True),
        Menu.SEPARATOR,
        *action_items,
        Menu.SEPARATOR,
        Item("Settings (enable / disable)", settings_menu),
        Item("Start on login", toggle_autostart, checked=lambda i: is_autostart_enabled()),
        Item("Help / Guide", lambda i, t: show_help()),
        Item("Quit", lambda i, t: quit_app()),
    )

    def on_ready(icon):
        global _NOTIFY, _ICON
        icon.visible = True
        _NOTIFY = icon.notify
        _ICON = icon
        refresh_icon()      # grey if nothing is recording
        _dbg("on_ready: icon visible set")
        if first_run:
            try:
                icon.notify("Right-click the tray icon for the dashboard, modules & Help.",
                            "Quietlog Suite installed")
            except Exception as e:
                _dbg(f"notify failed: {e!r}")
            show_help()   # open the guide once, so a new user knows what this is

    _dbg("run_tray: building icon")
    icon = pystray.Icon("Quietlog", make_icon_image(), "Quietlog Suite", menu)
    _dbg(f"run_tray: run() backend={type(icon).__module__}")
    icon.run(setup=on_ready)


# ===========================================================================
# Self-test (pure logic only; no GUI, no threads)
# ===========================================================================
def selftest():
    # Quietlog aggregation
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE samples (ts INTEGER, app TEXT, title TEXT, idle INTEGER)")
    day = datetime.now().strftime("%Y-%m-%d")
    base = int(datetime.strptime(day, "%Y-%m-%d").timestamp()) + 3600
    con.executemany("INSERT INTO samples VALUES (?,?,?,?)",
                    [(base, "chrome.exe", "", 0)] * 12
                    + [(base, IDLE_LABEL, "", 1)] * 6
                    + [(base, "code.exe", "", 0)] * 2)
    agg = dict(aggregate_today(con, day, SAMPLE_INTERVAL))
    assert agg == {"chrome.exe": 60, "code.exe": 10}, agg
    assert IDLE_LABEL not in agg

    # week view spans 7 days: a sample 3 days ago counts for week, not today
    wk_now = base + 3 * 86400
    con.execute("INSERT INTO samples VALUES (?,?,?,?)", (base, "vim.exe", "", 0))
    week = dict(aggregate_week(con, now=wk_now, interval=SAMPLE_INTERVAL))
    assert week.get("vim.exe") == 5 and "vim.exe" in week, week

    # retention prune: old rows go, recent stay
    pc = sqlite3.connect(":memory:")
    pc.execute("CREATE TABLE samples (ts INTEGER, app TEXT, title TEXT, idle INTEGER)")
    now = 1_000_000_000
    pc.execute("INSERT INTO samples VALUES (?,?,?,?)", (now - 100 * 86400, "old.exe", "", 0))
    pc.execute("INSERT INTO samples VALUES (?,?,?,?)", (now - 1 * 86400, "new.exe", "", 0))
    assert prune_old(pc, "samples", days=90, now=now) == 1
    assert pc.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1

    # single-instance (own mutex name so a real instance doesn't collide)
    tm = "Local\\QuietlogSelftest"
    assert already_running(tm) is False
    assert already_running(tm) is True

    # autostart roundtrip
    was = is_autostart_enabled()
    set_autostart(True); assert is_autostart_enabled() is True
    set_autostart(False); assert is_autostart_enabled() is False
    if was:
        set_autostart(True)

    # settings load/save roundtrip
    tmp = os.path.join(tempfile.gettempdir(), "ql_settings_test.json")
    s = dict(DEFAULT_SETTINGS); s["anchor"] = True
    save_settings(s, tmp)
    assert load_settings(tmp)["anchor"] is True
    os.remove(tmp)

    # Tabreaper match_window
    cands = [(101, "Notepad", "notepad.exe", (0, 0, 100, 100)),
             (102, "main.py - VS Code", "Code.exe", (0, 0, 100, 100))]
    assert match_window({"exe": "Code.exe", "title": "main.py - VS Code"}, cands) == 102
    assert match_window({"exe": "Code.exe", "title": "other.py - VS Code"}, cands) == 102  # prefix/exe
    assert match_window({"exe": "ghost.exe", "title": "x"}, cands) is None

    # Quietlog focus_split: tags -> work/distraction/neutral seconds (untagged=neutral)
    fs = focus_split([("Code.exe", 100), ("game.exe", 40), ("misc.exe", 10)],
                     {"Code.exe": "work", "game.exe": "distraction"})
    assert fs == {"work": 100, "distraction": 40, "neutral": 10}, fs
    # stray/hand-edited tag clamps to neutral instead of raising
    assert focus_split([("x", 5)], {"x": "bogus"}) == {"work": 0, "distraction": 0, "neutral": 5}

    # cap_recall keeps only the newest N rows
    rc = sqlite3.connect(":memory:")
    rc.execute("CREATE TABLE recall (content TEXT, kind TEXT, ts INTEGER)")
    rc.executemany("INSERT INTO recall VALUES ('x','screen',?)", [(i,) for i in range(120)])
    cap_recall(rc, max_rows=50)
    assert rc.execute("SELECT COUNT(*) FROM recall").fetchone()[0] == 50
    assert rc.execute("SELECT MIN(ts) FROM recall").fetchone()[0] == 70   # oldest 70 dropped

    # Localback version_name: readable date + hash, keeps the 8-char hash for dedup
    vn = version_name(1700000000, "abcdef0123456789", ".txt")
    assert vn.endswith("_abcdef01.txt") and vn[:4].isdigit(), vn

    # Localback pruning keeps only the newest N (ordered by mtime, not filename)
    pd = os.path.join(tempfile.gettempdir(), "ql_prune_test")
    shutil.rmtree(pd, ignore_errors=True)
    os.makedirs(pd)
    for i in range(30):
        fp = os.path.join(pd, f"2026-01-{i+1:02d}_000000_{i:08x}.txt")
        open(fp, "w").close()
        os.utime(fp, (1700000000 + i, 1700000000 + i))   # explicit increasing mtimes
    removed = prune_versions(pd, keep=25)
    kept = sorted(os.listdir(pd))
    assert removed == 5 and len(kept) == 25 and kept[0].startswith("2026-01-06"), (removed, kept[:2])
    shutil.rmtree(pd, ignore_errors=True)

    # Localback scan skips junk dirs (.git etc.) and the backup store
    sc = os.path.join(tempfile.gettempdir(), "ql_scan_test")
    shutil.rmtree(sc, ignore_errors=True)
    os.makedirs(os.path.join(sc, ".git"))
    os.makedirs(os.path.join(sc, "src"))
    open(os.path.join(sc, "keep.txt"), "w").close()
    open(os.path.join(sc, "src", "ok.py"), "w").close()
    open(os.path.join(sc, ".git", "config"), "w").close()
    found = {os.path.relpath(p, sc) for p in scan_files(sc, store_root="")}
    assert found == {"keep.txt", os.path.join("src", "ok.py")}, found
    shutil.rmtree(sc, ignore_errors=True)

    # Localback restore: snapshot -> list -> restore roundtrip
    rt = os.path.join(tempfile.gettempdir(), "ql_restore_test")
    shutil.rmtree(rt, ignore_errors=True)
    watch = os.path.join(rt, "watch")
    store = os.path.join(rt, "store")
    os.makedirs(watch)
    src = os.path.join(watch, "note.txt")
    open(src, "w").write("v1")
    snapshot_file(src, watch, store, 1700000000)        # save v1
    open(src, "w").write("v2-current")                  # change current file
    bks = list_backups(store)
    assert "note.txt" in bks and len(bks["note.txt"]) == 1, bks
    assert version_label(bks["note.txt"][0]).startswith("2023-"), version_label(bks["note.txt"][0])
    restore_version("note.txt", bks["note.txt"][0], watch, store)
    assert open(src).read() == "v1", "restore should bring back v1"
    # restore preserved the pre-restore content (v2) -> now 2 versions, undoable
    assert len(list_backups(store)["note.txt"]) == 2, "restore must preserve current first"
    shutil.rmtree(rt, ignore_errors=True)

    # Anchor sensitivity maps to thresholds (high = fires sooner than low)
    assert ANCHOR_LEVELS["high"] < ANCHOR_LEVELS["medium"] < ANCHOR_LEVELS["low"]

    # restore_version refuses an empty watch root (never writes to CWD)
    try:
        restore_version("x", "y", "")
        assert False, "should have raised on empty watch_root"
    except ValueError:
        pass

    # atomic save_json leaves no .tmp and round-trips
    aj = os.path.join(tempfile.gettempdir(), "ql_atomic.json")
    save_json({"x": 1}, aj)
    assert json.load(open(aj))["x"] == 1 and not os.path.exists(aj + ".tmp")
    os.remove(aj)

    # BgModule: stop()->start() leaves exactly one live worker (no leak)
    class _Probe(BgModule):
        def __init__(self):
            super().__init__()
            self.live = 0
        def _loop(self, stop):
            self.live += 1
            stop.wait(10)
            self.live -= 1
    p = _Probe()
    p.start(); time.sleep(0.1)
    p.start(); time.sleep(0.1)        # restart: old worker must exit
    assert p.live == 1, f"expected 1 live worker, got {p.live}"
    p.stop(); time.sleep(0.1)
    assert p.live == 0, f"worker did not stop, {p.live} alive"

    # Threadkeeper FTS insert + search
    tc = tk_connect(":memory:")
    tc.execute("INSERT INTO recall VALUES (?,?,?)", ("database connection timeout", "window", base))
    tc.execute("INSERT INTO recall VALUES (?,?,?)", ("lunch menu", "clipboard", base))
    res = tk_search(tc, "timeout")
    assert len(res) == 1 and "timeout" in res[0][0], res

    print("selftest OK | quietlog + tabreaper + anchor + localback + threadkeeper logic pass")


if __name__ == "__main__":
    _dbg(f"__main__ frozen={getattr(sys,'frozen',False)}")
    if "--selftest" in sys.argv:
        selftest()
    elif "--dashtest" in sys.argv:        # internal: prove the dashboard builds (esp. frozen)
        MODULES = [Quietlog(), Tabreaper(), Anchor(), Localback(), Threadkeeper()]
        open_dashboard()
        time.sleep(6)
        _dbg("dashtest done")
    elif already_running():
        _dbg("__main__ already_running -> exit")
        sys.exit(0)
    else:
        _dbg("__main__ -> run_tray")
        run_tray()
