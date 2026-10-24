"""Shared Windows access checks; never changes ACLs or submits game input."""
import os

from execution_control import ExecutionControlError, stop_execution


def process_token(pid):
    import win32api
    import win32con
    import win32security
    process = win32api.OpenProcess(0x1000, False, pid)
    try:
        token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
            return {'pid': pid, 'integrity_rid': sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1),
                    'elevated': bool(win32security.GetTokenInformation(token, win32security.TokenElevation))}
        finally:
            token.Close()
    finally:
        process.Close()


def access_failure(reason):
    # Persist the global signal even if a business module catches RuntimeError.
    stop_execution(str(reason), 'window_access')


def check_integrity(window):
    try:
        caller, game = process_token(os.getpid()), process_token(window.pid)
    except Exception as error:
        access_failure('无法核对助手与游戏的进程权限，未启动操作：' + str(error))
    if caller['integrity_rid'] < game['integrity_rid']:
        access_failure('助手权限层级低于游戏，Windows 将拒绝截图或输入。'
                       f"助手 RID={caller['integrity_rid']}，游戏 RID={game['integrity_rid']}。"
                       '请核对启动进程及 EXE 的完整性标签；本程序不会自动提权、修改 ACL 或重跑任务。')
    return {'assistant': caller, 'game': game}


def preflight(*, focus=False, recognize=False):
    """One capture, optionally foreground activation; no clicks/keys/recovery."""
    try:
        from game_window import GameWindow, check_requested_stop
        check_requested_stop()
        window = GameWindow.find()
        tokens = check_integrity(window)
        geometry = window.geometry()
        if focus:
            window.focus_client(geometry)
        image, captured = window.capture()
        if captured != geometry:
            access_failure('预检期间游戏窗口发生变化，未启动任务。')
        report = {'completed': True, 'tokens': tokens, 'geometry': captured,
                  'capture_size': list(image.size), 'focus_checked': focus, 'actions': [],
                  'scope': 'window_access_only'}
        if recognize:
            import numpy as np
            from common_navigation import PageDiagnosis
            report['page'] = PageDiagnosis().inspect(np.array(image)[:, :, ::-1].copy())
        return report
    except ExecutionControlError:
        raise
    except Exception as error:
        access_failure('游戏窗口访问预检失败，未启动任务：' + str(error))


def diagnostic(output):
    """CLI diagnostic before configuration/date migration, with no focus changes."""
    from atomic_json import write_json
    # A diagnostic is independent of execution environment inherited from a GUI.
    for name in ('BD2_EXECUTION_GAME_DAY', 'BD2_CONTROL_FILE', 'BD2_STOP_FILE'):
        os.environ.pop(name, None)
    try:
        report = preflight(focus=False, recognize=True)
        code = 0
    except ExecutionControlError as error:
        report = {'completed': False, 'error': str(error), 'control_kind': error.kind,
                  'actions': [], 'focus_checked': False}
        code = 1
    write_json(output, report)
    return code
