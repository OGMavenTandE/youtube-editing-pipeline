# Art jobs (`ytp art`)

`ytp art` makes Studio's plates, host poses, and props on the local GPU from a
small YAML (or JSON) jobs file. Each job is text to image, img2img (style from a
reference image, or a variation of an init image), or an inpaint fix with a
mask. Jobs with `alpha: true` get their background cut out. Every image gets a
JSON sidecar, and each run writes a contact sheet for review.

Nothing leaves the PC: models run locally, art is written outside the repo, and
`assets/scenes/` stays gitignored. Use generated test art in tests, never
Studio's real art.

```powershell
ytp art "C:\AI Eval\YouTube\ep02\edit\art.yaml" --dry-run      # validate, list outputs, load nothing
ytp art "C:\AI Eval\YouTube\ep02\edit\art.yaml"                # all jobs
ytp art "C:\AI Eval\YouTube\ep02\edit\art.yaml" --only host-wave --force
ytp art art.yaml --out "C:\AI Eval\YouTube\ytp-test\art"       # scratch folder instead of assets
```

| Option | Meaning |
| --- | --- |
| `--out DIR` | Output root. Default `YTP_ART_OUT`, else `C:\AI Eval\YouTube\assets\scenes` on Windows. |
| `--only ID` | Run one job (repeatable). |
| `--force` | Replace existing PNGs, sidecars, and the contact sheet. Without it the run stops before loading any model if a target exists. |
| `--dry-run` | Validate the file, check model policy, print each job's outputs, sizes, and seeds. Loads no model and writes nothing. |
| `--models-dir DIR` | Model cache, used as `HF_HOME` (and `U2NET_HOME=<dir>\u2net`). Default `YTP_MODELS_DIR`, else `C:\AI Eval\YouTube\models` on Windows. Never the user profile. |
| `--model ID` / `--inpaint-model ID` / `--matte-model NAME` | Override the model for every job (also `YTP_ART_MODEL`, `YTP_ART_INPAINT_MODEL`, `YTP_ART_MATTE_MODEL`). |
| `--allow-origin CC` | Allowed model origin, repeatable; replaces the default list (also `YTP_ART_ALLOWED_ORIGINS=US,UK`). |
| `--offload` | `auto` (model CPU offload on cards under 16 GB, such as the 12 GB 4070), `model`, `sequential` (slowest, least VRAM), or `none`. |

## Output

```
<out>\plate\<id>.png          first variant
<out>\plate\<id>-v2.png       second variant (count: 2), then -v3, ...
<out>\plate\<id>.json         sidecar for the job (all variants)
<out>\pose\...  <out>\prop\...
<out>\contact-<jobs-stem>.png contact sheet (contact-<stem>-2.png etc. when one exists and --force is off)
```

The sidecar records the prompt, negative, size, steps, cfg, strength, each
variant's seed, seconds, generated size and alpha stats (share of clear,
partial, and opaque pixels), the diffusion and matte models with their
organization, origin, and license, and the runtime (GPU, torch and diffusers
versions, offload mode, model load times, VRAM peak).

The contact sheet shows labelled thumbnails (`id  kind  seed`) with a
checkerboard behind transparent pixels, so a bad cutout is easy to spot.

## Jobs file

```yaml
version: 1
defaults:                     # any job key except id, init, mask, reference
  negative: text, watermark, logo, blurry, extra fingers
  steps: 30
  cfg: 6.5
jobs:
  - id: plate-kitchen
    kind: plate               # plate | pose | prop  (sets the subfolder)
    prompt: flat vector illustration, bright modern kitchen counter, soft morning light, clean shapes, no people
    seed: 1201
    count: 2                  # plate-kitchen.png and plate-kitchen-v2.png (seeds 1201, 1202)

  - id: host-wave
    kind: pose
    prompt: flat vector illustration of a friendly man in a navy cap waving, full body, plain white background
    negative: text, watermark, cropped feet, busy background
    seed: 2201
    alpha: true               # cut out the background (transparent PNG)
    reference: refs/host-style.png   # style from this image via img2img
    strength: 0.65            # how far to move away from the reference (0..1)

  - id: coin-meter
    kind: prop
    prompt: flat vector illustration of a gold coin gauge, isolated object, plain white background
    size: 768x768
    seed: 3201
    alpha: true

  - id: host-wave-capfix
    kind: pose
    mode: inpaint             # repaint only the white part of the mask
    init: refs/host-wave-backward-cap.png
    mask: refs/cap-mask.png
    prompt: navy baseball cap worn facing forward, brim over the eyes, flat vector style
    seed: 4201
    strength: 0.99
    alpha: true
```

| Key | Meaning |
| --- | --- |
| `id` | Letters, digits, `-`, `_`. Unique. Becomes the file name. |
| `kind` | `plate`, `pose`, or `prop`. Sets the subfolder and the default size. |
| `prompt` | Required. |
| `negative` | Negative prompt. |
| `size` | `1920x1080` or `[1920, 1080]`. Default 1920x1080 for plates, 1024x1024 for poses and props, and the init image's size for img2img and inpaint jobs. Images are generated at the closest SDXL-native size (1344x768 for 16:9, 1024x1024 for square, also 1152x896, 1216x832, 1536x640 and their portrait twins), then scaled to cover and center-cropped to `size`. |
| `seed` | Variant *n* uses `seed + n - 1`. Default: a stable number from the id. |
| `count` | Variants, 1..16. Default 1. |
| `steps`, `cfg` | Default 30 and 7.0. |
| `reference` | Style image for a text-to-image job. The job becomes img2img from the reference at `strength` (default 0.6). Not IP-Adapter. |
| `mode` | `txt2img` (default), `img2img` (needs `init`, `strength` default 0.6), or `inpaint` (needs `init` and `mask`, `strength` default 0.99). |
| `init` | The image to vary or fix. Alpha is flattened onto white first; set `alpha: true` to cut the result out again. |
| `mask` | Inpaint mask, same framing as `init`: white repaints, black keeps. An RGBA mask uses its alpha. |
| `alpha` | `true` cuts the background out with the matte model and writes an RGBA PNG. |
| `model`, `inpaint_model`, `matte_model` | Per-job model override. |
| `allowed_origins` | In `defaults` only: the allowed origin list for this file. |

Relative paths resolve against the jobs file's folder. Unknown keys are
errors, so a typo never silently does nothing.

## Models and origin policy

Every model a run needs must have a known origin, and the origin must be on the
allowed list (default `US, UK, CA`; change it with `defaults.allowed_origins`,
`--allow-origin`, or `YTP_ART_ALLOWED_ORIGINS`). A model with no declared
origin is refused. Model families from China-origin labs, IP-Adapter from
h94/Tencent, and BiRefNet are blocked by name whatever a jobs file declares.

| Role | Default id | Organization | Origin | License |
| --- | --- | --- | --- | --- |
| diffusion | `stabilityai/stable-diffusion-xl-base-1.0` | Stability AI (London) | UK | CreativeML Open RAIL++-M |
| inpaint | `diffusers/stable-diffusion-xl-1.0-inpainting-0.1` | Hugging Face diffusers team (Hugging Face Inc., New York) | US | CreativeML Open RAIL++-M |
| matte | `u2net` (rembg) | University of Alberta (U^2-Net, Qin et al. 2020) | CA | Apache-2.0 (U^2-Net); rembg is MIT |

None of these is hard-wired. To use another model, name it and declare it:

```yaml
models:
  example-org/example-diffusion-model:
    role: diffusion
    org: Example Org (Austin, Texas)
    origin: US
    license: Apache-2.0
    source: https://huggingface.co/example-org/example-diffusion-model
defaults:
  model: example-org/example-diffusion-model
```

The diffusion backend loads any diffusers text-to-image, img2img, or inpaint
pipeline in fp16 (the `fp16` weight variant when the repo has one). Img2img
and same-model inpaint reuse the loaded weights. On a 12 GB card it uses model
CPU offload and VAE tiling. The matte model is any rembg session name.

Downloads go to `--models-dir` (default `C:\AI Eval\YouTube\models`): SDXL
base in fp16 is about 7 GB, the inpainting model about 5 GB more, and u2net
about 170 MB. The run prints the size it downloaded.

## Install

The `[art]` extra holds torch, diffusers, and rembg so the core install stays
small. Install a CUDA build of torch that matches the driver first (check the
CUDA version `nvidia-smi` reports, and pick the matching wheel index from
pytorch.org), then the extra:

```powershell
& "C:\AI Eval\YouTube\ytp\.venv\Scripts\python.exe" -m pip install torch --index-url https://download.pytorch.org/whl/cu128
& "C:\AI Eval\YouTube\ytp\.venv\Scripts\python.exe" -m pip install -e ".[art]"
```

Without the extra, `ytp art --dry-run` still works and a real run stops with
a message naming the missing packages.
