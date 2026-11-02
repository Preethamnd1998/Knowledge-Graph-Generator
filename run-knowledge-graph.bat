@echo off
setlocal

:: ============================================================
::  Knowledge Graph Indexer - Launcher
::
::  Double-click this file to build the knowledge graph. It
::  reads kg-config.env next to it and writes the .db file(s)
::  into the output/ folder - same as running "python indexer.py"
::  by hand.
::
::  This window stays open the whole time, on purpose, no matter
::  what happens - success, an error, even a crash. It will NOT
::  close itself. If YOU close it before it finishes, indexing
::  stops immediately (closing the window kills everything running
::  inside it). Wait for it to say "Done" (or an error message)
::  and press a key, or just leave it open until then.
::
::  This file works as-is when left in the "Knowledge Graph
::  Generator" folder (it finds everything automatically).
::
::  IF YOU MOVE THIS FILE to a different folder, edit the line
::  below and put the full path to your "Knowledge Graph
::  Generator" folder between the quotes, for example:
::
::      set "KG_ROOT=<platform-repo>\shared\mcp-tools\knowledge-graph-generator"
::
::  Leave it as %~dp0 (the next line) if this file is still
::  sitting inside that folder.
:: ============================================================

:: --- Re-launch inside a window that cmd itself will never close on
::     its own (cmd /k), even if something below crashes hard before
::     reaching a "pause". Only happens once (KG_BAT_RELAUNCHED guards
::     it) - the actual logic runs on the second pass, below. ---
if not defined KG_BAT_RELAUNCHED (
    set "KG_BAT_RELAUNCHED=1"
    cmd /k call "%~f0"
    exit /b
)

set "KG_ROOT=%~dp0"

:: --- normalize a trailing backslash so paths join up cleanly ---
if "%KG_ROOT:~-1%"=="\" set "KG_ROOT=%KG_ROOT:~0,-1%"

echo ============================================================
echo  Knowledge Graph Indexer
echo ============================================================
echo  Using folder: %KG_ROOT%
echo.

if not exist "%KG_ROOT%\indexer.py" (
    echo ERROR: Could not find indexer.py inside:
    echo   %KG_ROOT%
    echo.
    echo If you moved this .bat file to a new location, right-click it,
    echo choose Edit, and set KG_ROOT to the full path of your
    echo "Knowledge Graph Generator" folder ^(the one containing
    echo indexer.py and kg-config.env^).
    echo.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python was not found on this computer.
    echo.
    echo Install it from a Command Prompt with:
    echo   winget install --id Python.Python.3.12 -e --source winget
    echo.
    echo Or download it from https://www.python.org/downloads/ and, on
    echo the installer's first screen, tick "Add python.exe to PATH".
    echo.
    echo Then close this window and double-click this file again.
    echo.
    pause
    exit /b 1
)

pushd "%KG_ROOT%"
python indexer.py
set "EXIT_CODE=%ERRORLEVEL%"
popd

echo.
if not "%EXIT_CODE%"=="0" (
    echo ============================================================
    echo  Indexing finished with errors ^(exit code %EXIT_CODE%^).
    echo  Scroll up to see what went wrong.
    echo ============================================================
) else (
    echo ============================================================
    echo  Done. The .db file^(s^) are wherever OUTPUT_DIR points in
    echo  kg-config.env - see "Output dir" a few lines up, or
    echo  %KG_ROOT%\output if you never changed OUTPUT_DIR.
    echo ============================================================
)
echo.
pause
