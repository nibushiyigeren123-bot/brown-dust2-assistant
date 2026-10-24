"""Build a portable Windows app using generic defaults and runtime assets only."""
import importlib.metadata
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BUILD_ROOT = PROJECT.parent / (PROJECT.name + '_build')


def main(target=None):
    if (PROJECT / 'models/gamevision_bd2').is_dir():
        try:
            import onnxruntime
        except ImportError as error:
            raise RuntimeError('构建内嵌模型版本需要当前Python环境安装ONNX Runtime CPU；不能生成缺少模型运行库的包。') from error
        if 'CPUExecutionProvider' not in onnxruntime.get_available_providers():
            raise RuntimeError('当前ONNX Runtime没有CPU执行器，未构建。')
    deps = BUILD_ROOT / 'deps'
    if deps.is_dir():
        sys.path.insert(0, str(deps))
    # PyInstaller may report success while omitting unavailable Win32 modules.
    # Reject that build environment before creating a broken application.
    import importlib
    for dependency in ('win32api', 'win32gui', 'win32process', 'win32security', 'win32ui', 'cv2', 'PIL.Image', 'tkinter'):
        try:
            importlib.import_module(dependency)
        except ImportError as error:
            raise RuntimeError(f'构建环境缺少 {dependency}；请使用已安装运行依赖的 Python 环境。') from error
    import PyInstaller.__main__
    target = Path(target).resolve() if target is not None else PROJECT / '发布/BD2Assistant'
    if target.exists():
        raise RuntimeError('Release directory exists; preserve it before rebuilding')
    icon = PROJECT / 'assets/icon/bd2_assistant.ico'
    if not icon.is_file():
        raise RuntimeError('Application icon is missing')
    # PyInstaller can reuse EXE resources when only the contents at --icon's
    # path change. A content-addressed build copy makes icon changes explicit.
    build_icon = BUILD_ROOT / 'icons' / (hashlib.sha256(icon.read_bytes()).hexdigest()[:16] + '.ico')
    build_icon.parent.mkdir(parents=True, exist_ok=True)
    if not build_icon.exists():
        shutil.copy2(icon, build_icon)
    hidden = ['common_navigation', 'm01_management', 'm02_free_draw', 'pending_daily',
              'guild_check_in', 'daily_settings', 'equipment_tasks', 'task_rewards',
              'pass_rewards', 'event_tasks', 'workflow_runner', 'workbench_ui', 'win32timezone']
    args = [str(PROJECT / 'tools/assistant_app.py'), '--name', 'BD2Assistant', '--onedir',
            '--windowed', '--noupx', '--icon', str(build_icon), '--distpath', str(target.parent),
            '--workpath', str(BUILD_ROOT / 'work'), '--specpath', str(BUILD_ROOT),
            '--paths', str(PROJECT / 'tools')]
    if deps.is_dir():
        args.extend(['--paths', str(deps)])
    for name in ('torch', 'torchvision', 'paddle', 'onnx', 'tensorflow', 'matplotlib', 'scipy', 'pandas', 'PySide6', 'PyQt6'):
        args.extend(['--exclude-module', name])
    for name in hidden:
        args.extend(['--hidden-import', name])
    PyInstaller.__main__.run(args)
    def asset_ignore(directory, names):
        if Path(directory) == PROJECT / 'assets':
            return ['launcher']
        if Path(directory).name == 'm00':
            return [name for name in names if name.startswith('home_')]
        return []
    shutil.copytree(PROJECT / 'assets', target / 'assets', ignore=asset_ignore)
    shutil.copytree(PROJECT / 'templates', target / 'templates')
    if (PROJECT / 'models/gamevision_bd2').is_dir():
        shutil.copytree(PROJECT / 'models/gamevision_bd2', target / 'models/gamevision_bd2')
    shutil.copytree(PROJECT / 'templates/config', target / 'config')
    (target / 'tools').mkdir()
    shutil.copy2(PROJECT / 'tools/windows_ocr.ps1', target / 'tools/windows_ocr.ps1')
    shutil.copy2(PROJECT / 'README.md', target / 'README.md')
    shutil.copy2(PROJECT / 'LICENSE', target / 'LICENSE')
    if (PROJECT / 'NOTICE.md').is_file():
        shutil.copy2(PROJECT / 'NOTICE.md', target / 'NOTICE.md')
    (target / 'build_info.json').write_text(json.dumps({
        'application_version':'1.0.0', 'python':sys.version,
        'pyinstaller':importlib.metadata.version('pyinstaller'), 'kind':'portable_onedir',
        # [元数据] GUI仅显示执行入口；启动界面不自动运行，用户点击后才启动worker。
        'entry':'BD2Assistant.exe', 'data_root':'beside_executable', 'default_mode':'execute_after_user_click',
        'config_source':'generic_templates', 'personal_fixtures_shipped':False,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'executable':str(target / 'BD2Assistant.exe')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
