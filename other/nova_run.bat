@echo off
chcp 936 >nul
cd /d "%~dp0"
set IF=\Device\NPF_{10B75DB8-255E-42F6-BBA4-A01C3FE683BC}
echo ==========================================================
echo  480x1080 校准彩条  红/绿/蓝/白/黄/青  各占 180 行
echo  屏幕应从上到下出现 6 条等宽纯色横带，一直显示到按 Ctrl+C
echo ==========================================================
py -3 "e:\发送卡\nova_led.py" --iface "%IF%" --pattern calib --width 480 --height 1080 --fps 30
echo.
echo ---- 已停止发送，屏幕应恢复黑屏 ----
pause
