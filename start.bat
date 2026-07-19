@echo off
rem run.py registers this folder as the `social_widget` package, so the checkout
rem can be named anything and the launch still works from wherever it sits.
rem pythonw has no console window; start launches it detached and the bat closes.
rem Prefer the app's own environment (.venv, created by telegram_login.py or
rem `python -m venv .venv` + pip install) so the host Python needs nothing.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0run.py"
) else (
  start "" pythonw "%~dp0run.py"
)
