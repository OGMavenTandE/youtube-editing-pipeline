# Security and cleanup review

Safe, behavior-preserving fixes only. Public URLs, page copy, and normal filenames are unchanged. Dangerous Drive names, filter breakout text, and config URLs that are not Drive folder links are the exceptions, and those are the security fixes.

No committed secret, token, password, or private key was found. `client_secret.json.example` is a placeholder. Nothing to rotate. History was not rewritten.

## Security findings

### High

1. **Drive inbox path traversal.** `desktop/worker.py:303` used to join the Drive file name onto the inbox directory. A name such as `..\..\Windows\System32\evil.mp4`, `C:foo.mp4`, `file:stream.mp4`, or `con.mp4` can leave that directory or hit a Windows device name. **Fix:** `pipeline/drive_io.py:125` `local_media_name` keeps one path segment, drops a leading drive prefix, replaces `:`, strips trailing spaces and dots, and prefixes reserved device stems. `pipeline/drive_io.py:141` `local_inbox_path` then checks the resolved path stays inside the inbox. Ordinary names such as `Talk 01.mp4` are unchanged. Names with a colon, a path separator, or a reserved device stem get a different local filename. The Drive display name in logs is still the original.

### Medium

2. **Concat demuxer injection.** The concat list is `file 'path'`. A quote or newline in the path can add another directive. `ytp/ff.py:172` `concat_line` already escaped quotes and now also strips CR/LF. **Fix:** `pipeline/media.py:344` `concat_demuxer_line` does the same, and both concat writers use it. `-safe 0` stays (`pipeline/media.py:283`, `ytp/render.py:677`) because the lists use absolute paths.

3. **drawtext filter breakout.** `ytp/ff.py:200` `escape_drawtext` escaped quotes, but ffmpeg splits filters on commas outside a quoted string. On ffmpeg 6.1.1, `text='a\',scale=32:32'` still started a `scale` filter. **Fix:** also escape `,` `;` `[` `]`. Those escapes render the same glyphs (Pillow pixel compare). Newlines are not escaped: ffmpeg treats `\n` as a line break and a backslash-n as the two characters, so escaping newlines would change captions.

4. **Drive query injection.** List and upload queries interpolated ids and names with only a single quote escaped. A backslash can undo that escape. **Fix:** `pipeline/drive_io.py:88` `drive_query_literal` escapes `\` first, then `'`. Call sites include `list_folders` (`pipeline/drive_io.py:366`), `find_child_folder`, inbox listing, the outbox check, and the upload existing-files query (`pipeline/drive_io.py:595`). A normal id such as `root` or `Talk` still produces the same quoted literal.

5. **yt-dlp output leaving the transcript folder.** A reported filepath or a symlink could point outside the video folder, and `%` in the directory is an output-template character. **Fix:** `ytp/fetch.py:258` `_within_dir` accepts a file only after `resolve()` and `relative_to` stay inside the folder. `ytp/fetch.py:268` `_media_outtmpl` percent-escapes the directory. A normal folder still ends in `media.%(ext)s`. Non-http URLs are still accepted; see recommendations.

6. **Outbox link open.** `desktop/main_window.py:181` opened `config.last_job_outbox_url` in the browser. A tampered config could open any URL. **Fix:** `pipeline/drive_io.py:98` `is_drive_folder_url` allows only `https://drive.google.com/drive/folders/<id>` with no whitespace and no `/` or `\` in the id. `pipeline/drive_io.py:94` `drive_folder_url` percent-encodes the id. `drive_folder_url("abc123")` is unchanged. Anything else is logged and not opened.

7. **Drive media download URI.** `pipeline/drive_io.py:498` used the client request URI, and the fallback put the file id in the URL unencoded. **Fix:** `pipeline/drive_io.py:108` `drive_media_uri` keeps the request URI only when it is https and the host is `googleapis.com` or `*.googleapis.com`. Otherwise it uses a percent-encoded fallback. Redirects are not pinned to that host; Google may redirect media to `googleusercontent.com`.

8. **HTML placeholder rescan.** Slide, thumbnail, and motion HTML used sequential `str.replace` of `{{TOKENS}}`. Text inserted for an earlier token was scanned again, so a kicker of `{{lt_title}}` could fill the lower-third slot, and motion JSON could rewrite `{{WIDTH}}`. **Fix:** one-pass replacement in `pipeline/broll/slides.py:96`, `pipeline/studio.py:223`, and `ytp/motion.py:1224`. `pipeline/studio.py:208` also requires the thumbnail frame to be a `data:image/` URI with no quotes or brackets. Copy that still contains `{{` fails the motion build, including a literal `{{WIDTH}}` that used to be substituted. Normal plans do not contain those tokens.

### Low

9. **Secret files and `.env` lines.** Secret files were written then chmodded, and an env value with a newline could append another key. **Fix:** `desktop/secrets.py:62` `write_private_bytes` creates the file mode `0600` with `os.open`. `desktop/envfile.py:22` rejects `\n`, `\r`, and NUL and writes through that helper. `desktop/paths.py:65` sets mode `0600` after copying a migrated `.env`. `desktop/secrets.py:12` passes `CRYPTPROTECT_UI_FORBIDDEN` to DPAPI so a UI prompt is not shown. Old tokens still decrypt. No extra entropy was added, because that would invalidate them.

10. **Symlink upload.** A symlink inside a studio folder could upload a file from outside the package. **Fix:** `pipeline/drive_io.py:602` uploads regular files only. A symlinked studio directory itself is still accepted.

11. **Log redaction gaps.** `desktop/logutil.py:23` and `:24` now redact `GOCSPX-` client secrets and `Bearer` tokens, in addition to the existing key, access-token, and refresh-token patterns. Key names without values stay visible so errors such as "Gemini API key is not set" still read correctly.

12. **Title-update errors were dropped.** `desktop/main_window.py:169` logged the exception from a lambda. Python deletes the `except` variable when the block ends, so the deferred call raised `NameError` and the UI never showed the failure. **Fix:** `desktop/main_window.py:170` binds the message before scheduling the callback. The log line is the one the UI already intended to show.

## Removals

- **`ffmpeg-python` removed from `requirements.txt`.** Nothing imports the `ffmpeg` module. ffmpeg is invoked as a binary. The suite passed without the package installed. MoviePy 2 does not need it.
- **Unused imports removed.** `time` in `desktop/worker.py`. `first_existing_client_secret` in `desktop/wizard.py`. `GOLD`, `OVERLAY_W`, and `WHITE` in `pipeline/picture_kit.py` (the RGBA names stay). `HEADLINE_CHAR_LIMIT`, `KICKER_CHAR_LIMIT`, and `field_is_locked` in `pipeline/talk_sheet.py`. The `sanitize_script_kickers` import in `tests/test_talk_sheet.py`. The functions remain.
- **`tests/test_compositor.py` imports `split_webcam_rect` from `pipeline.layouts`.** `pipeline.compositor` does not call it. `pipeline/ffmpeg_scene.py` already imports it from `layouts`. The geometry test is unchanged.

## Not changed

- **OAuth scope stays `https://www.googleapis.com/auth/drive`** (`pipeline/drive_io.py:14`). The inbox holds files the app did not create. `drive.file` would not see them, and a narrower scope would invalidate existing tokens. There is no YouTube upload scope.
- **`ytp transcribe` still accepts any yt-dlp URL and local files.** The README documents that. Downloads stay inside the transcript folder (finding 5).
- **Retired Chromium slide renderer stays.** `pipeline/broll/slides.py:52` `slide_variant` returns `None` and `:70` `collect_slide_jobs` returns `[]`. It is exported and tested. Playwright is still used for studio thumbnails.
- **`requirements.txt` is not pinned.** Pinning would change which versions a fresh install resolves.
- **`requirements-ytp.txt` stays.** It duplicates the pyproject core deps and is not used by CI. The comment in the file is the install note for the editor venv.
- **drawtext newlines stay unescaped.** See finding 3.
- **A symlinked studio directory is still uploaded.** It may be a data disk. Only symlink files inside it are skipped (finding 10).
- **Hooks still run the user's script.** `ytp/hooks.py:111` is the extension point for graphics and mix scripts the operator configured.
- **Gemini still receives the source audio, and `ytp art` still downloads its models.** `art` already requires safetensors and an origin allowlist. No model, service, or dependency was added.
- **The desktop client secret remains a plaintext value in `.env`.** That is how the installed app is configured. Non-Windows token files are mode `0600`, not DPAPI.
- **`import asyncio` in `desktop/__main__.py:1` stays.** It must run before `subprocess.Popen` is patched. Ruff F401 on that line is a false positive.
- **No format sweep of `pipeline/` or `desktop/`.** CI lints `ytp` and `tests/ytp` only. Existing line-length, import-order, and `LowerThird` findings there were left alone.
- **The two products stay separate.** `ytp` and the desktop/Drive pipeline were not merged.
- **No deploy, secret, DNS, or YouTube action was run.**

## Checks

Before these edits, on `main`:

- `python3 -m ruff check ytp tests/ytp` — passed
- `python3 -m pytest tests/ytp -m "not whisper" --confcutdir=tests/ytp -q` — 313 passed, 1 deselected
- `python3 -m pytest tests -q --tb=line --ignore=tests/ytp` — 300 passed, 2 failed because Playwright was not installed (`test_write_studio_package_folder`, `test_pipeline_skip_gemini_writes_studio_folder`). After `python3 -m playwright install chromium`, those two tests passed. That was an environment gap, not a product failure.

After these edits:

- `python3 -m ruff check ytp tests/ytp` — passed
- `python3 -m pytest tests/ytp -m "not whisper" --confcutdir=tests/ytp -q` — 314 passed, 1 deselected
- `python3 -m pytest tests -q --tb=line --ignore=tests/ytp` — 307 passed

The extra tests cover the path, query, concat, drawtext, placeholder, and secret-file fixes. No page or Streamlit flow changed, so there was no browser pass. No deploy script was run.

## Counts

Tracked files on `main`: 182. Text files (`.py .js .css .html .ps1 .yml .yaml .json .toml .txt .md .example`, plus `requirements*.txt`, `.gitignore`, `.env.example`): 177, 42111 lines. Count is `git ls-tree -r main`, then newline count of `git show`, plus one when a file has no trailing newline.

Direct dependencies before: 30 unique names. `requirements.txt` had 14. `pyproject.toml` adds 16 that do not overlap (2 runtime, 5 whisper including the two Windows CUDA wheels, 7 art, 2 dev; `transcribe` repeats `yt-dlp`).

After this change, including this file: 183 files, 178 text files, 42511 lines. `requirements.txt` has 13 entries. Unique direct dependencies: 29. The one removal is `ffmpeg-python`. No dependency was added.
