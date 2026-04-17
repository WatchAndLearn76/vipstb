@echo off
title VIPEMU HUNTER ELITE

echo.
echo  ============================================================
echo  VIPEMU HUNTER ELITE - Gelistirme Versiyonu
echo  Windows Baslatic
echo  ============================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo  [HATA] Python bulunamadi!
    echo  Python 3.9+ kurun: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo  [*] Bagimliliklar kontrol ediliyor...
pip install -r requirements.txt -q --disable-pip-version-check 2>nul

echo.
echo  [*] Sunucu baslatiliyor...
echo  [*] Dashboard: http://localhost:5000
echo  Durdurmak icin: CTRL + C
echo.

python vipemu_server.py

echo.
echo  Sunucu durduruldu.
pause

