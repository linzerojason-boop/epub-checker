@echo off
cd /d %~dp0
set PY=C:\Users\Jason\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe
echo 正在打包 EPUB 检查器...
"%PY%" -m PyInstaller epub_checker.spec --noconfirm || goto fail
echo.
echo 打包完成：dist\EPUB检查器.exe
"dist\EPUB检查器.exe" --smoke
pause
exit /b 0
:fail
echo 打包失败，请检查上面的输出。
pause
exit /b 1
