@echo off
setlocal EnableExtensions DisableDelayedExpansion
chcp 65001 >nul
pushd "%~dp0"
set "ASR_PY=.asr\venv\Scripts\python.exe"
set "HF_HUB_DISABLE_XET=1"
set "HF_HUB_DOWNLOAD_TIMEOUT=60"
set "HF_HUB_DISABLE_TELEMETRY=1"

if not exist "%ASR_PY%" (
    py -3 -m venv ".asr\venv"
    if errorlevel 1 goto failed
)
rem Faster-whisper 1.2.1 uses an audio-decoding option removed in PyAV 19.
"%ASR_PY%" -B -m pip --disable-pip-version-check --timeout 60 install --only-binary=:all: "faster-whisper==1.2.1" "av==16.1.0" "sherpa-onnx==1.12.30"
if errorlevel 1 goto failed
where /q nvidia-smi.exe
if not errorlevel 1 (
    "%ASR_PY%" -B -X utf8 -m pip --disable-pip-version-check --timeout 60 install --only-binary=:all: "nvidia-cublas-cu12==12.9.1.4" "nvidia-cudnn-cu12>=9.10,<10"
    if errorlevel 1 echo CUDA libraries could not be installed. CPU transcription is available.
)
"%ASR_PY%" -B -X utf8 -m seu_voice_lib.local_transcription --prepare
if errorlevel 1 goto failed
"%ASR_PY%" -B -X utf8 -m seu_voice_lib.punctuation --prepare
if errorlevel 1 goto failed
popd
pause
exit /b 0

:failed
echo Could not prepare offline transcription. Check internet access and the x64 Python installation.
popd
pause
exit /b 1
