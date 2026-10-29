# Windows setup

This is the one-time install for Scott's PC. It does not change Windows settings and it does not touch any other Python install. Takes stay in `C:\AI Eval\YouTube`. `ytp` never copies a take and never publishes a video.

## Install the two programs

Open PowerShell and run:

```
winget install Python.Python.3.12
winget install Gyan.FFmpeg
```

Close that window and open a new one so PATH picks up `py` and `ffmpeg`.

Check the NVIDIA driver:

```
nvidia-smi
```

You want a driver that is current enough for the GPU (about 12 GB). If `nvidia-smi` is not found, install the driver from NVIDIA before the CUDA transcription step. Rendering can still fall back to the CPU encoder.

## Create the ytp environment

From the repo folder:

```
powershell -File scripts\setup-windows.ps1
```

That creates `C:\AI Eval\YouTube\ytp\.venv` with Python 3.11 or 3.12, installs `ytp` with local Whisper, and installs the CUDA 12 libraries that match ctranslate2 4.x (cuBLAS 12 and cuDNN 9). Those DLLs are loaded from the venv when `ytp` starts. Nothing is written to the system PATH.

The script ends with a self-test. It prints the Python version, where ffmpeg was found, whether `h264_nvenc` is listed, whether CUDA is visible, and a tiny Whisper run. A missing GPU is a warning. A failed Whisper run, or a pip error, stops the script with a non-zero exit.

Run the tests from the repo with:

```
pytest tests/ytp
```

If an older checkout still loads `tests/conftest.py` and complains about dotenv, use `pytest tests/ytp --confcutdir=tests/ytp`. Current `ytp` collects `tests/ytp` without that flag.

To put the venv somewhere else:

```
powershell -File scripts\setup-windows.ps1 -Venv D:\ytp\.venv
```

Use that venv's `python.exe` and `ytp.exe` after this. Example:

```
C:\AI Eval\YouTube\ytp\.venv\Scripts\ytp.exe take "C:\AI Eval\YouTube\desk.mp4"
```

If ffmpeg is not on PATH, set `YTP_FFMPEG` to the full path of `ffmpeg.exe` for that window only. ffprobe is read from the same folder.

## Daily flow

1. Drop a `.mp4` or `.mov` in `C:\AI Eval\YouTube`.
2. `ytp take` makes a folder beside that file (`desk\` for `desk.mp4`), hardlinks the take into `source\raw` when Windows allows it, and writes `edit\transcript.txt`, `edit\words.json`, and the clip index. Transcription prefers the GPU (float16). With no CUDA it prints a warning and uses the CPU (int8). The model for a take is `small`. The original file is not copied and not modified.
3. Edit `edit\plan.yaml`.
4. `ytp finish desk` checks the plan, masters the mixed audio to the series loudness target (-16 LUFS, true peak -1.5 dBTP, LRA 11), then renders `renders\v1\` (then v2, and so on) with NVENC when ffmpeg lists it and a 256x256 trial encode works, otherwise libx264. Mastering aims 0.5 dB under that peak limit so the AAC file stays at or below -1.5 dBTP, and the encode stays at the mix sample rate (48000 Hz). It runs `ytp check` on the CPU and prints `qc\check-report.md`, and also writes `qc\check-report-vN.md` for that version. The latest report remains `check-report.md`. The loudness section includes the measured input and output. Raw desk takes near -24 LUFS are brought to that target. The measurement is the full mix, including the end card, after any music ducking. `ytp finish desk --no-master` skips mastering. `ytp finish desk --denoise` runs a light denoise (highpass near 80 Hz, then afftdn) before the loudness pass. The `-af` string for both passes is in `renders\vN\render.log`. If linear gain would push the peaks over the limit, loudnorm uses dynamic mode and the loudness section notes where that gain would have peaked. The note does not warn and does not fail the check.

## End card

The end card picture is not in the git repo. It has Scott's headshot and handles. Put `option-C-clean.png` in one of these places, first match wins:

1. The full path in `YTP_END_CARD`.
2. Next to the clone: `<folder that contains the repo>\assets\end-card\option-C-clean.png`.
3. Inside the episode: `assets\end-card\option-C-clean.png`.
4. Inside the clone, which is gitignored: `assets\end-card\option-C-clean.png`. If the clone is `C:\AI Eval\YouTube\ytp`, that file is `C:\AI Eval\YouTube\ytp\assets\end-card\option-C-clean.png`.

`ytp self-test` prints `end card: found ...` or `end card: missing (expected at ...)`. Missing is a warning. The self-test still succeeds. `ytp finish` draws a plain card and the check report warns that the image was not found.

To watch a folder for new files that have stopped growing:

```
powershell -File scripts\watch-takes.ps1 -Folder "C:\AI Eval\YouTube"
```

Each file is handed to `ytp take` once.
