"""Capture and interact with the identified BrownDust II client window."""
import ctypes
import os
import time
from pathlib import Path
from ctypes import wintypes
from dataclasses import dataclass
from window_access import access_failure

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except (AttributeError, OSError):
    pass

import win32api
import win32con
import win32gui
import win32process
import win32ui
import pywintypes
from functools import wraps


def window_api(operation):
    @wraps(operation)
    def checked(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (pywintypes.error, OSError) as error:
            access_failure("Windows 窗口接口失败：" + str(error))
    return checked

from PIL import Image

_capture_api = ctypes.WinDLL('user32', use_last_error=True)
_capture_api.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
_capture_api.PrintWindow.restype = wintypes.BOOL


def check_requested_stop():
    expected_day = os.environ.get('BD2_EXECUTION_GAME_DAY')
    if expected_day:
        from game_calendar import check_execution_day
        check_execution_day(expected_day)
    signal = os.environ.get('BD2_STOP_FILE')
    if signal and Path(signal).is_file():
        from execution_control import stop_execution
        stop_execution('用户已停止当前任务', 'stopped')
    if win32api.GetAsyncKeyState(0x77)&0x8000:
        from execution_control import stop_execution
        stop_execution('用户已按F8停止当前任务', 'stopped')


@dataclass
class GameWindow:
    hwnd: int
    pid: int
    title: str

    @classmethod
    @window_api
    def find(cls):
        matches = []

        def visit(hwnd, _):
            title = win32gui.GetWindowText(hwnd)
            if win32gui.IsWindowVisible(hwnd) and title.casefold() == "browndust ii":
                matches.append(cls(hwnd, win32process.GetWindowThreadProcessId(hwnd)[1], title))

        win32gui.EnumWindows(visit, None)
        if len(matches) != 1:
            access_failure(f"Expected one visible BrownDust II window; found {len(matches)}")
        return matches[0]

    @window_api
    def geometry(self):
        if not win32gui.IsWindow(self.hwnd) or win32gui.IsIconic(self.hwnd):
            access_failure("Game window closed or minimized")
        if win32process.GetWindowThreadProcessId(self.hwnd)[1] != self.pid:
            access_failure("Game window identity changed")
        _, _, width, height = win32gui.GetClientRect(self.hwnd)
        if width <= 0 or height <= 0:
            raise RuntimeError("Empty client area")
        origin = win32gui.ClientToScreen(self.hwnd, (0, 0))
        return {"hwnd": self.hwnd, "pid": self.pid, "title": self.title,
                "width": width, "height": height, "origin": list(origin)}

    @window_api
    def capture(self):
        # A render transition can briefly return a black PrintWindow bitmap.
        # Retry observation only; never repeat a click or battle submission.
        expected = None
        for attempt in range(5):
            check_requested_stop()
            try:
                return self._capture_once()
            except RuntimeError as error:
                if 'capture is black' not in str(error).lower():
                    raise
                if attempt == 4:
                    access_failure(str(error))
                current = self.geometry()
                if expected is not None and current != expected:
                    raise RuntimeError('Window changed during black-frame observation; capture stopped') from error
                expected = current
                time.sleep(.25)
                check_requested_stop()
                if self.geometry() != expected:
                    raise RuntimeError('Window changed during black-frame observation; capture stopped')

    def _capture_once(self):
        check_requested_stop()
        if os.environ.get('BD2_EXECUTION_GAME_DAY'):
            # Background GPU frames can be stale. Execution observations must
            # foreground this verified client before diagnosing the next step.
            if (win32gui.IsWindow(self.hwnd) and win32gui.IsIconic(self.hwnd)
                    and win32process.GetWindowThreadProcessId(self.hwnd)[1]==self.pid):
                win32gui.ShowWindow(self.hwnd,win32con.SW_RESTORE)
                time.sleep(.3)
            if win32gui.GetForegroundWindow()!=self.hwnd:
                self.focus_client(self.geometry())
                time.sleep(.3)
        geometry = self.geometry()
        width, height = geometry["width"], geometry["height"]
        dc_handle = win32gui.GetDC(self.hwnd)
        source_dc = win32ui.CreateDCFromHandle(dc_handle)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        previous = None
        try:
            bitmap.CreateCompatibleBitmap(source_dc, width, height)
            previous = memory_dc.SelectObject(bitmap)
            # PW_CLIENTONLY | PW_RENDERFULLCONTENT: no title bar or frame.
            # HWND/HDC are pointer-sized, including in the packaged x64 app.
            ctypes.set_last_error(0)
            ok = _capture_api.PrintWindow(self.hwnd, memory_dc.GetSafeHdc(), 3)
            if not ok:
                access_failure(f"PrintWindow did not capture the client (Win32 error {ctypes.get_last_error()})")
            image = Image.frombuffer("RGB", (width, height), bitmap.GetBitmapBits(True), "raw", "BGRX", 0, 1)
            if image.getextrema() == ((0, 0), (0, 0), (0, 0)):
                raise RuntimeError("Game capture is black")
            from frame_store import capture_image
            return capture_image(image.copy()), geometry
        finally:
            if previous is not None:
                memory_dc.SelectObject(previous)
            win32gui.DeleteObject(bitmap.GetHandle())
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(self.hwnd, dc_handle)

    @window_api
    def focus_client(self, expected_geometry):
        """Activate the verified game without sending mouse or keyboard input."""
        check_requested_stop()
        if self.geometry() != expected_geometry:
            raise RuntimeError("Window changed before activation")
        if win32gui.GetForegroundWindow() != self.hwnd:
            try:
                win32gui.SetForegroundWindow(self.hwnd)
            except pywintypes.error:
                pass
            if win32gui.GetForegroundWindow() != self.hwnd:
                # Hidden workers may lack the launcher's foreground permission.
                # Attach only the current foreground and verified game queues;
                # all geometry, foreground and covered-target checks still apply.
                # A worker without a GUI must initialize its own message queue.
                win32gui.PeekMessage(None, 0, 0, win32con.PM_NOREMOVE)
                attached = []
                caller = ctypes.windll.kernel32.GetCurrentThreadId()
                try:
                    foreground = win32gui.GetForegroundWindow()
                    for hwnd in (foreground, self.hwnd):
                        if not hwnd:
                            continue
                        thread = win32process.GetWindowThreadProcessId(hwnd)[0]
                        if thread != caller and thread not in attached:
                            if not ctypes.windll.user32.AttachThreadInput(caller, thread, True):
                                access_failure('Could not attach the verified game focus queue')
                            attached.append(thread)
                    win32gui.BringWindowToTop(self.hwnd)
                    win32gui.SetForegroundWindow(self.hwnd)
                finally:
                    for thread in reversed(attached):
                        ctypes.windll.user32.AttachThreadInput(caller, thread, False)
        deadline = time.monotonic() + 0.8
        while win32gui.GetForegroundWindow() != self.hwnd and time.monotonic() < deadline:
            time.sleep(0.02)
        if win32gui.GetForegroundWindow() != self.hwnd:
            access_failure("Could not foreground the game")
        if self.geometry() != expected_geometry:
            raise RuntimeError("Window changed while activating; capture again")

    @window_api
    def recover_input(self, expected_geometry):
        """One native restore of this same window; caller must re-observe before input."""
        check_requested_stop()
        if self.geometry()!=expected_geometry:
            raise RuntimeError('Window changed before native input recovery')
        win32gui.ShowWindow(self.hwnd,win32con.SW_MINIMIZE)
        try:
            time.sleep(.3)
            check_requested_stop()
        finally:
            # Restore even if stopped, leaving the user's game visible.
            if win32gui.IsWindow(self.hwnd) and win32process.GetWindowThreadProcessId(self.hwnd)[1]==self.pid:
                win32gui.ShowWindow(self.hwnd,win32con.SW_RESTORE)
        time.sleep(.5)
        restored=self.geometry()
        if restored!=expected_geometry:
            raise RuntimeError('Window changed during native input recovery; capture again')
        self.focus_client(restored)
        return {'hwnd':self.hwnd,'pid':self.pid,'method':'native_minimize_restore','geometry':restored}

    @window_api
    def click_client(self, point, expected_geometry, *, press_ms=100, native_motion=False,
                     submission_state=None):
        # [输入阶段] 由实际输入后端报告边界；validating只证明尚未尝试鼠标按下。
        if submission_state is not None:
            if type(submission_state) is not dict:
                raise ValueError('submission_state must be a dictionary')
            submission_state.clear()
            submission_state.update(protocol='game_window_click_v1', phase='validating',
                backend='SendInput' if native_motion else 'mouse_event')
        if type(press_ms) is not int or not 100<=press_ms<=350:
            raise ValueError('Supported mouse press is 100 to 350 milliseconds')
        if type(native_motion) is not bool:
            raise ValueError("native_motion must be boolean")
        check_requested_stop()
        if self.geometry() != expected_geometry:
            raise RuntimeError("Window changed since recognition; capture again")
        x, y = [int(round(value)) for value in point]
        if not (0 <= x < expected_geometry["width"] and 0 <= y < expected_geometry["height"]):
            raise ValueError("Click is outside client")
        self.focus_client(expected_geometry)
        position = win32gui.ClientToScreen(self.hwnd, (x, y))
        hit = win32gui.WindowFromPoint(position)
        if hit != self.hwnd and win32gui.GetAncestor(hit, win32con.GA_ROOT) != self.hwnd:
            raise RuntimeError("Game click target is covered")
        old_position = win32api.GetCursorPos()
        accepted = []
        native_pressed = False
        if native_motion:
            from native_mouse_input import send_mouse
        try:
            if native_motion:
                accepted.append(send_mouse(position))
            else:
                win32api.SetCursorPos(position)
            time.sleep(0.10)
            actual_position = win32api.GetCursorPos()
            if actual_position != position:
                raise RuntimeError(f'点击前检测到鼠标位置变化，未发出鼠标按下；目标{position}，实际{actual_position}。已停止当前项。')
            if win32gui.GetForegroundWindow() != self.hwnd or self.geometry() != expected_geometry:
                raise RuntimeError("Game changed before mouse press")
            check_requested_stop()
            if native_motion:
                hit = win32gui.WindowFromPoint(position)
                if hit != self.hwnd and win32gui.GetAncestor(hit, win32con.GA_ROOT) != self.hwnd:
                    raise RuntimeError("Game click target became covered before mouse press")
                # [提交边界] 即使系统调用抛错，也不能断言按下没有被游戏接收。
                if submission_state is not None:
                    submission_state['phase'] = 'mouse_down_attempted'
                accepted.append(send_mouse(position, 2))
                native_pressed = True
            else:
                # [提交边界] 在调用系统按下接口之前先保守标记，异常时保留未决。
                if submission_state is not None:
                    submission_state['phase'] = 'mouse_down_attempted'
                win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0)
            try:
                # Keep a short press visible to a game's frame-based input loop.
                ctypes.windll.kernel32.Sleep(press_ms)
            finally:
                if native_motion:
                    try:
                        accepted.append(send_mouse(None, 4))
                    except Exception as release_error:
                        # Release-only cleanup cannot submit another click. Never
                        # move the pointer if the accepted down may still be held.
                        try:
                            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
                        except Exception:
                            raise RuntimeError('Native mouse release and release-only cleanup failed; stopped') from release_error
                        raise RuntimeError('Native mouse release failed; release-only cleanup sent; stopped') from release_error
                else:
                    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0)
            # Let the game consume mouse-up at the target before restoring the cursor.
            time.sleep(0.10)
        finally:
            if (not native_motion or not native_pressed) and win32api.GetCursorPos() == position:
                win32api.SetCursorPos(old_position)
        event = {"client_point": [x, y], "screen_point": list(position), 'input_kind': 'left_click', 'press_ms': press_ms}
        if native_motion:
            event.update(input_backend='SendInput_absolute_virtual_desktop',
                         input_packets_accepted=accepted, cursor_restored=False)
        # [输出] 这里只确认按下/松开路径返回；奖励到账仍必须由上层视觉核实。
        if submission_state is not None:
            submission_state['phase'] = 'submitted'
        return event
