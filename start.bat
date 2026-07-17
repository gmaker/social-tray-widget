@echo off
rem run.py registers this folder as the `social_widget` package, so the checkout
rem can be named anything and the launch still works from wherever it sits.
rem pythonw has no console window; start launches it detached and the bat closes.
start "" pythonw "%~dp0run.py"
