@echo off
setlocal

REM Build as a onedir bundle: the OCR models are tens of MB and a onefile exe
REM would unpack them to %TEMP% on every launch.
REM Prerequisites:  pip install -r requirements.txt
REM                 pip install pyinstaller
REM
REM This file is deliberately ASCII-only. cmd.exe reads batch files byte-wise in
REM the active codepage, so UTF-8 Chinese inside REM/echo lines can swallow the
REM "^" line continuations and hand PyInstaller garbage arguments. Chinese notes
REM live in README.md (the "building the exe" section).

if not exist assets\app_icon.ico (
  echo ERROR: assets\app_icon.ico is missing, PyInstaller needs it for --icon.
  echo        Run "python assets\make_icon.py" to rebuild it from
  echo        assets\app_icon_source.png, then build again.
  goto :fail
)

python -m PyInstaller --noconfirm --clean --windowed ^
  --name LiveAutoClicker ^
  --icon assets\app_icon.ico ^
  --manifest app.manifest ^
  --collect-all rapidocr_onnxruntime ^
  app.py
if errorlevel 1 goto :fail

REM config/, templates/ and assets/ must sit NEXT TO the exe: a frozen app treats
REM the exe directory as its base path, so anything packed into _internal is
REM unreadable and uneditable for users. /D copies only newer files, so a config
REM the user already edited inside dist/ is never overwritten.
xcopy /E /I /D /Y config "dist\LiveAutoClicker\config" >nul
if not exist "dist\LiveAutoClicker\assets" mkdir "dist\LiveAutoClicker\assets"
copy /Y assets\app_icon.ico "dist\LiveAutoClicker\assets\" >nul
copy /Y assets\app_icon.png "dist\LiveAutoClicker\assets\" >nul
if not exist "dist\LiveAutoClicker\templates" mkdir "dist\LiveAutoClicker\templates"
REM templates/ holds the button snapshots the UI takes with the region picker.
REM The exe reads them from next to itself, so they must travel with the build.
REM Without this the exe starts with an empty templates folder and every template
REM target silently stops matching - "detected nothing" with no error anywhere.
xcopy /Y templates\*.png "dist\LiveAutoClicker\templates\" >nul 2>nul

REM opencv_videoio_ffmpeg*.dll (28MB) only serves cv2.VideoCapture; this app
REM grabs screenshots and never decodes video.

REM PyInstaller's dependency walk sometimes picks up msvcp140.dll / MSVCP140_1.dll from
REM C:\Windows\System32\HWAudioDriverLibs (an unrelated audio driver copy) and flattens
REM them into _internal. Because _internal is registered as a DLL search directory, that
REM stale CRT shadows the real one and onnxruntime.dll then fails its DllMain with
REM error 1114 - which silently kills the whole OCR channel in the exe. Deleting them is
REM safe: the correct CRT is already in System32 on any machine that can run Python.
set BADCRT=dist\LiveAutoClicker\_internal\msvcp140.dll dist\LiveAutoClicker\_internal\MSVCP140_1.dll
for %%F in (%BADCRT%) do if exist "%%F" del /Q "%%F"
del /Q "dist\LiveAutoClicker\_internal\cv2\opencv_videoio_ffmpeg*.dll" 2>nul

echo.
echo OK  dist\LiveAutoClicker\LiveAutoClicker.exe
echo Copy the whole dist\LiveAutoClicker folder when distributing.
echo config\ and templates\ live next to the exe and are editable.
goto :end

:fail
echo.
echo BUILD FAILED - see the PyInstaller output above.
echo Common causes: pyinstaller not installed, or a dependency is missing.
exit /b 1

:end
pause
