# Duplicate File Manager (Windows)

## Requirements
- Windows 10/11
- Python 3.10+ (recommend Python 3.12)

## Quick start
Double-click `run.bat` (requires Python installed and accessible via `py`). First launch installs dependencies.

Or in PowerShell:
```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

## Build executable
Double-click `build_exe.bat`. Output: `dist\DuplicateFileManager.exe`.

## Usage
1. Select a directory and click Scan.
2. Expand duplicate groups. Double-click a file to reveal it in Explorer.
3. Tick individual files, or choose Auto-select (keeps first alphabetical path in each group).
4. Choose Copy, Move, or Recycle Bin. Confirm the action.
5. Rescan after changes.

## Notes
- Duplicate means identical file size and SHA-256 content digest, regardless of filename.
- Skips symlinks. Inaccessible files are reported. No system folders should be scanned without understanding the consequences.
- Auto-select is based on alphabetical path, **not** file age or importance.
- Delete blocks selection of all copies of a group. Copy/Move allow all selections.
- Move/Copy preserve folder structure relative to the selected scan root; collisions receive unique suffixes.
- No overwrite is intentional. Existing files are not replaced.
- If destination lies inside scanned root, copied files may appear in subsequent scans.
- Files are rehashed before each action, but concurrent file modifications can still occur; use on stable files and back up important data.
- Actions run on the UI thread, so large operations may temporarily freeze the window. This is a starter desktop app, not a transactional filesystem manager.
