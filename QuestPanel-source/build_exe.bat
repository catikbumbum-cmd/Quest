@echo off
rem Builds dist\QuestPanel.exe: one file, no console window.
rem Needs Python 3.10+ and internet access for pip. Run it from the folder with all the project files.
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto failed
python -m pip install --upgrade pywebview
if errorlevel 1 echo pywebview did not install, the app will open in an Edge or Chrome app window instead.
python -m PyInstaller --noconsole --onefile --name QuestPanel --add-data "panel.html;." quest_app.py
if errorlevel 1 goto failed
echo.
echo Done: dist\QuestPanel.exe
goto end
:failed
echo.
echo Build failed, see the messages above.
:end
pause
