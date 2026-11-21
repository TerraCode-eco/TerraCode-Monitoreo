@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist "monitoreo.py" (
  echo No se encuentra monitoreo.py en esta carpeta.
  echo Si abriste el archivo .zip con doble clic, primero extraelo: clic derecho sobre el .zip ^> "Extraer todo",
  echo y luego ejecuta INICIAR.bat desde la carpeta extraida.
  pause
  exit /b 1
)

rem Busca un Python que REALMENTE funcione. "where python" no basta: el acceso directo de la Microsoft Store
rem aparece como "python" pero no ejecuta nada, y antes eso impedia probar con "py".
set "PY="
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys, tkinter" >nul 2>&1 && set "PY=%%~C"
  )
)
if not defined PY for /d %%D in ("%LocalAppData%\Programs\Python\Python3*") do if not defined PY if exist "%%~fD\python.exe" set PY="%%~fD\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if not defined PY if exist "%%~fD\python.exe" set PY="%%~fD\python.exe"
if not defined PY for /d %%D in ("C:\Python3*") do if not defined PY if exist "%%~fD\python.exe" set PY="%%~fD\python.exe"

if not defined PY (
  echo No se encontro un Python que funcione.
  echo.
  echo Lo que ve Windows:
  where py 2>nul
  where python 2>nul
  echo.
  echo Si aparece una ruta con "WindowsApps", es el acceso directo de la Microsoft Store, no Python real.
  echo Solucion: instala Python 3.10 o superior desde https://www.python.org/downloads/ marcando
  echo "Add python.exe to PATH", o desactiva en Configuracion ^> Aplicaciones ^> Alias de ejecucion de aplicaciones
  echo los alias "python.exe" y "python3.exe".
  pause
  exit /b 1
)

echo Usando Python:
%PY% --version
echo Instalando/verificando librerias (la primera vez tarda un poco)...
%PY% -m pip install --quiet -r requirements.txt
if errorlevel 1 (
  echo No se pudieron instalar las librerias. Revisa tu conexion a internet e intenta de nuevo.
  pause
  exit /b 1
)
echo Iniciando el sistema de monitoreo...
%PY% monitoreo.py
if errorlevel 1 pause
