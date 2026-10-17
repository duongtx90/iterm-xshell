"""Cửa sổ riêng chứa panel kênh SSH, dock vào mép trái cửa sổ iTerm2 đang active.

Toolbelt của iTerm2 chỉ nằm bên phải, nên panel kênh SSH chạy trong một NSPanel riêng:
- process con (pyobjc + WKWebView), không có icon Dock (activation policy Accessory);
- panel "non-activating": click vào không lấy focus khỏi iTerm2, nhưng vẫn gõ phím được;
- mỗi ~150ms tìm cửa sổ iTerm2 trên cùng (CGWindowList, không cần quyền Screen Recording)
  và đặt panel sát bên trái; ẩn khi app khác ở trên cùng;
- giao tiếp với daemon qua stdin/stdout, mỗi dòng một JSON.

Chạy: XSHELL_SESSIONS_URL=URL python -m xshell_panels.sessions_window [--iterm-pid PID]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading

STATE_FILE = os.path.expanduser("~/.config/iterm-xshell/window.json")
MIN_W, MAX_W, DEFAULT_W = 200, 520, 260
TICK = 0.15


# ------------------------------------------------- hình học (test được, không cần AppKit)
def cg_to_cocoa(bounds, main_height):
    """CGWindow bounds (gốc trên-trái, y hướng xuống) -> (x, y, w, h) gốc dưới-trái của Cocoa."""
    x, y, w, h = bounds
    return (x, main_height - (y + h), w, h)


def screen_for(frame, screens):
    """Màn hình (x, y, w, h) chứa tâm của frame; mặc định màn đầu."""
    cx, cy = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
    for s in screens:
        if s[0] <= cx < s[0] + s[2] and s[1] <= cy < s[1] + s[3]:
            return s
    return screens[0]


def dock_frame(win, visible, width):
    """Tính frame panel bên trái cửa sổ `win` (toạ độ Cocoa).

    Trả về (frame, need) với need > 0 là số px cửa sổ iTerm2 cần dịch sang phải để
    panel không đè lên nó (khi sát mép trái màn hình hoặc đang full màn hình).
    """
    wx, wy, ww, wh = win
    vx, vy, vw, vh = visible
    x = wx - width
    need = 0
    if x < vx:
        need = vx - x
        x = vx
    # Giữ panel trong vùng hiển thị theo chiều dọc.
    y = max(wy, vy)
    top = min(wy + wh, vy + vh)
    h = max(top - y, 200)
    return (x, y, width, h), need


def load_width():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            w = int(json.load(f).get("width", DEFAULT_W))
            return min(max(w, MIN_W), MAX_W)
    except (OSError, ValueError, TypeError):
        return DEFAULT_W


def save_width(w):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), mode=0o700, exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"width": int(w)}, f)
        os.replace(tmp, STATE_FILE)
    except OSError:
        pass


def emit(**msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


# ------------------------------------------------------------------ AppKit
def main(argv=None):
    ap = argparse.ArgumentParser()
    # URL chứa token: truyền qua biến môi trường (argv thì user khác thấy được bằng `ps`).
    ap.add_argument("--url", default=os.environ.get("XSHELL_SESSIONS_URL"))
    ap.add_argument("--iterm-pid", type=int, default=0)
    ap.add_argument("--make-room", type=int, default=1)
    ap.add_argument("--hidden", action="store_true")
    args = ap.parse_args(argv)
    if not args.url:
        ap.error("thiếu --url hoặc XSHELL_SESSIONS_URL")

    import objc
    import AppKit
    import Quartz
    import WebKit
    from Foundation import NSObject, NSURL, NSURLRequest, NSMakeRect, NSTimer
    from PyObjCTools import AppHelper

    my_pid = os.getpid()
    if not args.iterm_pid:
        apps = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(
            "com.googlecode.iterm2")
        if not apps:
            emit(ev="error", msg="iTerm2 không chạy")
            return 1
        args.iterm_pid = apps[0].processIdentifier()

    class DockPanel(AppKit.NSPanel):
        def canBecomeKeyWindow(self):
            return True

        def canBecomeMainWindow(self):
            return False

        def performKeyEquivalent_(self, event):
            # App không có menu Edit nên tự chuyển ⌘C/⌘V/⌘X/⌘A/⌘Z cho web view.
            flags = event.modifierFlags() & AppKit.NSEventModifierFlagDeviceIndependentFlagsMask
            if flags in (AppKit.NSEventModifierFlagCommand,
                         AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagShift):
                ch = (event.charactersIgnoringModifiers() or "").lower()
                shift = bool(flags & AppKit.NSEventModifierFlagShift)
                sel = {"c": "copy:", "v": "paste:", "x": "cut:", "a": "selectAll:",
                       "z": "redo:" if shift else "undo:"}.get(ch)
                if sel and AppKit.NSApp.sendAction_to_from_(sel, None, self):
                    return True
            return objc.super(DockPanel, self).performKeyEquivalent_(event)

    class Controller(NSObject):
        def init(self):
            self = objc.super(Controller, self).init()
            self.wanted = not args.hidden     # người dùng muốn hiện (toggle)
            self.shown = False
            self.width = load_width()
            self.room_key = None              # (cửa sổ, bounds) đã xin dịch, tránh lặp vô hạn
            self.last_frame = None
            self.last_win = None
            self.overlay = False              # cho phép đè lên iTerm2 đang full screen native
            return self

        @objc.python_method
        def build(self):
            style = (AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable
                     | AppKit.NSWindowStyleMaskResizable | AppKit.NSWindowStyleMaskUtilityWindow
                     | AppKit.NSWindowStyleMaskNonactivatingPanel)
            p = DockPanel.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, self.width, 600), style, AppKit.NSBackingStoreBuffered, False)
            p.setTitle_("Kênh SSH")
            p.setHidesOnDeactivate_(False)
            p.setFloatingPanel_(True)
            p.setLevel_(AppKit.NSFloatingWindowLevel)
            p.setBecomesKeyOnlyIfNeeded_(True)
            p.setReleasedWhenClosed_(False)
            p.setMinSize_((MIN_W, 200))
            p.setMaxSize_((MAX_W, 100000))
            p.setCollectionBehavior_(AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces
                                     | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary
                                     | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
            p.setDelegate_(self)
            cfg = WebKit.WKWebViewConfiguration.alloc().init()
            try:
                cfg.preferences().setValue_forKey_(True, "developerExtrasEnabled")
            except Exception:
                pass
            wv = WebKit.WKWebView.alloc().initWithFrame_configuration_(
                p.contentView().bounds(), cfg)
            wv.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewHeightSizable)
            p.contentView().addSubview_(wv)
            wv.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(args.url)))
            self.panel, self.webview = p, wv
            self.build_status_item()
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                TICK, self, "tick:", None, True)
            emit(ev="ready", pid=my_pid)

        @objc.python_method
        def build_status_item(self):
            # Icon trên thanh menu macOS: click để ẩn/hiện panel kênh SSH.
            item = AppKit.NSStatusBar.systemStatusBar().statusItemWithLength_(
                AppKit.NSVariableStatusItemLength)
            btn = item.button()
            img = None
            try:
                img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "server.rack", "Kênh SSH")
            except Exception:
                pass
            if img is not None:
                img.setTemplate_(True)
                btn.setImage_(img)
            else:
                btn.setTitle_("SSH")
            btn.setToolTip_("Kênh SSH: ẩn/hiện panel")
            btn.setTarget_(self)
            btn.setAction_("statusClicked:")
            self.status_item = item

        def statusClicked_(self, sender):
            self.command({"cmd": "toggle"})

        # ---- NSWindowDelegate
        def windowShouldClose_(self, sender):
            self.wanted = False
            self.hide()
            emit(ev="hidden")
            return False

        def windowDidEndLiveResize_(self, note):
            w = int(self.panel.frame().size.width)
            if w != self.width:
                self.width = w
                save_width(w)
                self.last_frame = None

        # ---- commands từ daemon
        @objc.python_method
        def command(self, cmd):
            c = cmd.get("cmd")
            if c == "show":
                self.wanted = True
                self.overlay = True
            elif c == "hide":
                self.wanted = False
                self.hide()
            elif c == "toggle":
                self.wanted = not self.wanted
                self.overlay = self.wanted
                if not self.wanted:
                    self.hide()
            elif c == "reload":
                url = cmd.get("url") or args.url
                self.webview.loadRequest_(NSURLRequest.requestWithURL_(NSURL.URLWithString_(url)))
            elif c == "focus":
                self.wanted = True
                self.overlay = True
                self.tick_(None)
                if self.shown:
                    self.panel.makeKeyAndOrderFront_(None)
                    self.webview.evaluateJavaScript_completionHandler_(
                        "window.xshellFocusSearch && window.xshellFocusSearch()", None)
            elif c == "quit":
                AppKit.NSApp.terminate_(None)
            emit(ev="state", visible=self.wanted)

        @objc.python_method
        def hide(self):
            if self.shown:
                self.panel.orderOut_(None)
                self.shown = False

        # ---- vòng dock
        @objc.python_method
        def front_iterm_window(self):
            opts = (Quartz.kCGWindowListOptionOnScreenOnly
                    | Quartz.kCGWindowListExcludeDesktopElements)
            for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
                if w.get("kCGWindowOwnerPID") != args.iterm_pid:
                    continue
                if w.get("kCGWindowLayer", 0) != 0 or w.get("kCGWindowAlpha", 1) == 0:
                    continue
                b = w.get("kCGWindowBounds") or {}
                if b.get("Width", 0) < 300 or b.get("Height", 0) < 150:
                    continue
                return w.get("kCGWindowNumber"), (b["X"], b["Y"], b["Width"], b["Height"])
            return None

        def tick_(self, _timer):
            try:
                self._tick()
            except Exception as e:  # không để timer chết
                emit(ev="error", msg=repr(e))

        @objc.python_method
        def _tick(self):
            if not self.wanted:
                return self.hide()
            front = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
            fpid = front.processIdentifier() if front else None
            if fpid not in (args.iterm_pid, my_pid):
                return self.hide()
            found = self.front_iterm_window()
            if not found:
                return self.hide()
            num, bounds = found
            screens_ns = AppKit.NSScreen.screens()
            main_h = screens_ns[0].frame().size.height
            win = cg_to_cocoa(bounds, main_h)

            def rect(r):
                return (r.origin.x, r.origin.y, r.size.width, r.size.height)
            screens = [rect(s.frame()) for s in screens_ns]
            idx = screens.index(screen_for(win, screens))
            visible = rect(screens_ns[idx].visibleFrame())
            frame, need = dock_frame(win, visible, self.width)
            # Full screen native: cửa sổ phủ cả thanh menu nên không dịch được. Chỉ hiện
            # (đè lên) khi người dùng chủ động bật bằng phím tắt.
            full = bounds[1] <= 1 and win[3] >= screens[idx][3] - 1 and need > 0
            if not full:
                self.overlay = False
            elif not self.overlay:
                return self.hide()
            # Xin dịch lại mỗi khi bounds đổi (snap/maximize đưa cửa sổ về mép trái);
            # nếu dịch không được (full screen native) thì bounds giữ nguyên nên không lặp.
            if need and not full and args.make_room and self.room_key != (num, bounds):
                self.room_key = (num, bounds)
                emit(ev="need_room", dx=int(need), window=num)
            if frame != self.last_frame:
                self.panel.setFrame_display_(NSMakeRect(*frame), True)
                self.last_frame = frame
            if not self.shown:
                self.panel.orderFrontRegardless()
                self.shown = True
            self.last_win = num

    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)
    ctl = Controller.alloc().init()
    ctl.build()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except ValueError:
                continue
            AppHelper.callAfter(ctl.command, cmd)
        # stdin đóng = daemon đã thoát
        AppHelper.callAfter(AppKit.NSApp.terminate_, None)

    threading.Thread(target=reader, daemon=True).start()
    AppHelper.runEventLoop(installInterrupt=True)


if __name__ == "__main__":
    main()
