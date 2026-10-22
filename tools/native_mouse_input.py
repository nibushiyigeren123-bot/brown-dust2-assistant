"""Native mouse queue packets; no input is sent during import."""
import ctypes
from ctypes import wintypes
import win32api

class MouseInput(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG),
                ('mouseData', wintypes.DWORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ctypes.c_size_t)]

class InputUnion(ctypes.Union):
    _fields_ = [('mi', MouseInput)]

class Input(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('data', InputUnion)]

_api = ctypes.WinDLL('user32', use_last_error=True)
_send_input = _api.SendInput
_send_input.argtypes = (wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int)
_send_input.restype = wintypes.UINT

def absolute_point(position):
    left, top, width, height = (win32api.GetSystemMetrics(index) for index in (76, 77, 78, 79))
    x, y = position
    if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
        raise RuntimeError('Mouse target is outside the virtual desktop')
    # Pixel centres map back to the same physical pixel, also on negative monitors.
    return tuple(min(65535, ((2 * (value - origin) + 1) * 65536) // (2 * span))
                 for value, origin, span in ((x, left, width), (y, top, height)))

def send_mouse(position=None, button=0):
    if button not in (0, 2, 4):
        raise ValueError('Only move, left down and left up packets are supported')
    if position is None:
        if button != 4:
            raise ValueError('Only release may omit mouse motion')
        x, y, flags = 0, 0, button
    else:
        x, y = absolute_point(position)
        flags = 0x0001 | 0x8000 | 0x4000 | 0x2000 | button
    packet = Input(type=0, data=InputUnion(mi=MouseInput(x, y, 0, flags, 0, 0)))
    ctypes.set_last_error(0)
    accepted = int(_send_input(1, ctypes.byref(packet), ctypes.sizeof(Input)))
    if accepted != 1:
        raise RuntimeError(f'SendInput accepted {accepted}/1 mouse packets (Win32 error {ctypes.get_last_error()}); no automatic retry')
    return accepted
