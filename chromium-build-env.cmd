@echo off
rem Chromium build shell: bootstraps depot_tools into build\ and sets up the
rem environment for this session only (system/user PATH is left untouched).
rem Chromium itself is fetched manually into build\chromium.
rem See https://chromium.googlesource.com/chromium/src/+/main/docs/windows_build_instructions.md
rem
rem Usage:
rem   chromium-build-env.cmd                 - open an interactive build shell
rem   chromium-build-env.cmd <command...>    - run a command in the env and exit
rem                                            e.g. chromium-build-env.cmd autoninja -C out\Default chrome

setlocal

set "ROOT=%~dp0"
set "ROOT=%ROOT:~0,-1%"
set "BUILD_DIR=%ROOT%\build"
set "DEPOT_TOOLS=%BUILD_DIR%\depot_tools"
set "CHROMIUM_ROOT=%BUILD_DIR%\chromium"
set "PATCHES_DIR=%ROOT%\patches"

where git >nul 2>&1 || (
  echo [chromium-build-env] git not found in PATH, install Git for Windows first.
  exit /b 1
)

if not exist "%BUILD_DIR%" mkdir "%BUILD_DIR%"

if not exist "%DEPOT_TOOLS%\gclient.bat" (
  echo [chromium-build-env] Cloning depot_tools into "%DEPOT_TOOLS%"...
  git clone https://chromium.googlesource.com/chromium/tools/depot_tools.git "%DEPOT_TOOLS%" || exit /b 1
)

rem depot_tools must be ahead of any Python / git installs.
set "PATH=%DEPOT_TOOLS%;%PATH%"

rem Use locally installed Visual Studio instead of the Google-internal toolchain.
set DEPOT_TOOLS_WIN_TOOLCHAIN=0

rem VS 2026 is installed under "18\<Edition>", not the default "2026\<Edition>".
if not defined vs2026_install (
  for %%E in (Enterprise Professional Community BuildTools) do (
    if not defined vs2026_install if exist "%ProgramFiles%\Microsoft Visual Studio\18\%%E\VC" (
      set "vs2026_install=%ProgramFiles%\Microsoft Visual Studio\18\%%E"
    )
  )
)
if not defined vs2026_install (
  echo [chromium-build-env] WARNING: Visual Studio 2026 not found, set vs2026_install manually.
)

rem Chromium pins the Windows SDK version in setup_toolchain.py; gn gen fails
rem with an obscure traceback if that exact SDK is not installed.
set "SDK_VERSION="
set "SETUP_TOOLCHAIN=%CHROMIUM_ROOT%\src\build\toolchain\win\setup_toolchain.py"
if exist "%SETUP_TOOLCHAIN%" (
  for /f "tokens=2 delims='" %%V in ('findstr /b /c:"SDK_VERSION = " "%SETUP_TOOLCHAIN%"') do set "SDK_VERSION=%%V"
)
if defined SDK_VERSION if not exist "%ProgramFiles(x86)%\Windows Kits\10\Include\%SDK_VERSION%\um" (
  echo [chromium-build-env] WARNING: Windows SDK %SDK_VERSION% required by Chromium is not installed.
  echo [chromium-build-env]          Install it via Visual Studio Installer -^> Individual components,
  echo [chromium-build-env]          or from https://developer.microsoft.com/windows/downloads/windows-sdk/
)

rem Print build performance summary after autoninja.
set NINJA_SUMMARIZE_BUILD=1

if not exist "%CHROMIUM_ROOT%" mkdir "%CHROMIUM_ROOT%"
if exist "%CHROMIUM_ROOT%\src" (
  cd /d "%CHROMIUM_ROOT%\src"
) else (
  cd /d "%CHROMIUM_ROOT%"
  echo [chromium-build-env] No checkout yet, run here: gclient ^&^& fetch chromium
)

if not "%~1"=="" (
  call %*
  exit /b %ERRORLEVEL%
)

title Chromium build env
echo depot_tools    : %DEPOT_TOOLS%
echo vs2026_install : %vs2026_install%
echo windows sdk    : %SDK_VERSION%
echo patches        : %PATCHES_DIR%
echo cwd            : %CD%
cmd /k
