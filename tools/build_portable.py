"""Build a portable Windows app using generic defaults and runtime assets only."""
import importlib.metadata
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
BUILD_ROOT = PROJECT.parent / (PROJECT.name + '_build')


def main():
    deps = BUILD_ROOT / 'deps'
    if deps.is_dir():
        sys.path.insert(0, str(deps))
    import PyInstaller.__main__
    target = PROJECT / '发布/BD2Assistant'
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
    for name in ('torch', 'tensorflow', 'matplotlib', 'scipy', 'pandas', 'PySide6', 'PyQt6'):
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
    shutil.copytree(PROJECT / 'templates/config', target / 'config')
    (target / 'tools').mkdir()
    shutil.copy2(PROJECT / 'tools/windows_ocr.ps1', target / 'tools/windows_ocr.ps1')
    shutil.copy2(PROJECT / 'README.md', target / 'README.md')
    if (PROJECT / 'NOTICE.md').is_file():
        shutil.copy2(PROJECT / 'NOTICE.md', target / 'NOTICE.md')
    (target / 'build_info.json').write_text(json.dumps({
        'application_version':'0.2.4', 'python':sys.version,
        'pyinstaller':importlib.metadata.version('pyinstaller'), 'kind':'portable_onedir',
        'entry':'BD2Assistant.exe', 'data_root':'beside_executable', 'default_mode':'read_only',
        'config_source':'generic_templates', 'personal_fixtures_shipped':False,
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'executable':str(target / 'BD2Assistant.exe')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
