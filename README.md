# Shengtu

Codex image-generation skill package for the Subarx OpenAI-compatible image API.

## Contents

- `shengtu-skill/`: Codex skill group.
- `shengtu-skill/SKILL.md`: skill instructions.
- `shengtu-skill/scripts/generate_image.py`: image generation/edit helper.
- `shengtu-skill/agents/openai.yaml`: agent metadata.
- `shengtu-skill/config.example.json`: local private config example.

## Secret Handling

Do not commit a real `config.json` or API key. Save local credentials through:

```bash
python shengtu-skill/scripts/generate_image.py --save-api-key "YOUR_SUBARX_IMAGE_API_KEY"
```
