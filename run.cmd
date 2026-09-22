@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONPATH=%CD%\src

echo ============================================================
echo  NI 43-101 抽取 Agent —— 一键验证（无需任何 API Key）
echo ============================================================
echo.
echo [1/3] 单元测试
python -m unittest discover -s tests
if errorlevel 1 goto :err
echo.
echo [2/3] 全链路（抽查 - 评审 - 修订 - 拒答 - 评测）
python -m ni43101.cli mock
if errorlevel 1 goto :err
echo.
echo [3/3] 复跑对照（Evolution Log 到 few-shot 是否带来提升）
python -m ni43101.cli rerun --mock
if errorlevel 1 goto :err

echo.
echo ============================================================
echo  完成。生成的报告：
echo    %CD%\out\report.md
echo    %CD%\out_ablation\ablation.md
echo ============================================================
start "" "%CD%\out\report.md"
start "" "%CD%\out_ablation\ablation.md"
pause
exit /b 0

:err
echo.
echo [失败] 请把上面的报错信息发给我。
pause
exit /b 1
