@echo off
echo Building site locally...
python gdoc_site.py --doc 17fjNvJzj8ZGSer7c7OFe_CNfUKbAxEh_OBv94ZdRG5c --out _site
if errorlevel 1 (
    echo Build failed!
    pause
    exit /b 1
)
echo.
echo Starting local server at http://localhost:8000
echo Press Ctrl+C to stop
python serve.py --dir _site
