@echo off
setlocal
set "STATION_RUNTIME=D:\LifeBeacon-build\station-runtime-verified"
set "PATH=%STATION_RUNTIME%;%STATION_RUNTIME%\Library\bin;%STATION_RUNTIME%\Scripts;%PATH%"
set "CONDA_PREFIX=%STATION_RUNTIME%"
"%STATION_RUNTIME%\python.exe" "%~dp0station.py" %*
if errorlevel 1 pause
