set PYTHONIOENCODING=utf-8
set PYTHONLEGACYWINDOWSSTDIO=utf-8
set SVCFUSION_LAUNCHER_IPC_PORT=12948
set SVCFUSION_LAUNCHER_ACCESS_TOKEN=fa23e677f2770e4bdbb56951de491869e06f487467eca283fb4b75456d802f84
set GRADIO_SERVER_PORT=7777

@echo off
chcp 65001 >nul
title SVC-Fusion Launcher

echo ========================================================
echo                 当前组件为：SVC-Fusion
echo ========================================================
echo.
echo 感谢 @TheSmallHanCat 的聚合启动器制作
echo 感谢 @心空 12138 的聚合启动器制作
echo 感谢 @青山绿水 的聚合启动器制作
echo ========================================================
echo.
echo 当前工作目录是： %~dp0
echo 大憨憨正在为你启动此组件，请稍等片刻......
echo ========================================================
echo.

set "ROOT=%~dp0"
set PATH=%ROOT%ffmpeg\bin;%PATH%
set PYTHONPATH=%PYTHONPATH%;%ROOT%
echo %ROOT% > workdir

"%ROOT%.conda\python.exe" -u "%ROOT%launcher.py"

pause
