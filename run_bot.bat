@echo off
rem Runs the bot once (used by Task Scheduler). The bot writes its own log to bot.log;
rem anything that crashes before logging starts lands in bot_errors.log.
cd /d "%~dp0"
"%LOCALAPPDATA%\Programs\Python\Python313\python.exe" -W ignore bot.py >> bot_errors.log 2>&1
