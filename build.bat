@echo off
echo Installing build tools...
pip install -r requirements.txt pyinstaller
echo Building PhoneSpeaker...
pyinstaller --noconfirm phonespeaker.spec
echo.
echo Done. Your app is in dist\PhoneSpeaker\PhoneSpeaker.exe
pause
