"""Shared Windows application identity and icon for main/child windows."""
import ctypes
from project_paths import ROOT


def initialize_app_identity():
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('BrownDust2.Assistant')


def set_window_icon(window):
    path = ROOT / 'assets/icon/bd2_assistant.ico'
    if path.is_file():
        window.iconbitmap(default=str(path))
        window.iconbitmap(str(path))
