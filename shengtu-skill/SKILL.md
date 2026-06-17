---
name: shengtu-skill
description: Generate or edit images through the A站 OpenAI-compatible image API. Use when the user asks for 生图, 生成图片, 图片编辑, 改图, A站生图, gpt-image-2 生图, or wants Codex to create local bitmap images through https://www.subarx.com.
---

# shengtu-skill

Use `shengtu-skill` to generate or edit images through the A站 image gateway and save output files locally.

## Key Rules

- Use the bundled script `scripts/generate_image.py` for image generation and editing.
- Use only a dedicated A站 image API key.
- Use non-streaming image requests only.
- Prefer raw image endpoints `/v1/images/generations` and `/v1/images/edits`; do not use streaming Responses/SSE requests for images.
- Do not read from, write to, replace, or switch `OPENAI_API_KEY`.
- Do not hard-code real API keys into `SKILL.md`, scripts, generated docs, or final answers.
- If no image API key is configured, ask the user for their dedicated A站 image key, then save it with the script's `--save-api-key` command only after user confirmation.

## API Key Lookup Order

The script resolves the image key in this order:

1. `--api-key`
2. Environment variables: `A站_API_KEY`, `AISTATION_API_KEY`, `AIWANWU_API_KEY`
3. Local private config file managed by this skill

The local private config is skill-specific and must not be shared with other skills.

## First-Time Setup

When the user has no configured image key, tell them to run:

```powershell
python scripts\generate_image.py --save-api-key "YOUR_A_SITE_IMAGE_API_KEY"
```

To see where the key is stored:

```powershell
python scripts\generate_image.py --show-config-path
```

To remove the saved key:

```powershell
python scripts\generate_image.py --clear-api-key
```

## Text-To-Image

Use legal resolution strings such as `1024x1024`, not `1K`, `2K`, or `4K`.

```powershell
python scripts\generate_image.py --prompt "A clean product poster with clear subject and soft studio lighting" --size 1024x1024 --out poster.png
```

## Image Edit

```powershell
python scripts\generate_image.py --mode edit --prompt "Improve the lighting and keep the main subject unchanged" --image input.png --size 1024x1024 --out edited.png
```

## Endpoints

- Base URL: `https://www.subarx.com`
- Generation endpoint: `POST /v1/images/generations`
- Edit endpoint: `POST /v1/images/edits`

## Defaults

- `model`: `gpt-image-2`
- `quality`: `low`
- `n`: `1`
- `output_format`: `png`
- `size`: `1024x1024`

## Size Guide

- 1K square: `1024x1024`
- 2K landscape: `1536x1024`
- 2K portrait: `1024x1536`
- 4K landscape: `3840x2160`
- 4K portrait: `2160x3840`

## Acceptance Checklist

- The request uses `/v1/images/generations` or `/v1/images/edits`.
- The request is non-streaming and does not set `stream=true`.
- The `Authorization` header uses only the dedicated image API key for this request.
- The request does not modify global OpenAI or coding-skill credentials.
- The output file exists and has nonzero size.
