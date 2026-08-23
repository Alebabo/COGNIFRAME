# Palantum

Palantum turns recorded pitch takes into reviewable A/B chunks and a final edited video.

## Quickstart

Install the pinned video engine and verify the local toolchain:

```powershell
uv sync
uv run python scripts/setup_engine.py
uv run palantum doctor
```

For an ordinary local start, copy `.env.example` to `.env`, add the required keys, and run:

```powershell
uv run palantum serve
```

When credentials are injected by a shell, CI runner, or secret manager, use the safe launcher. It
passes `--no-env-file` to uv so an ambient `.env` cannot replace the injected process values;
Palantum itself only fills variables that are still missing.

```powershell
$env:DEVIN_API_KEY = "<injected>"
$env:OPENAI_API_KEY = "<injected>"
.\scripts\serve.ps1 -VideosDir . -TemplateSource "C:\path\to\templates.zip"
```

```sh
DEVIN_API_KEY='<injected>' OPENAI_API_KEY='<injected>' \
  sh scripts/serve.sh --videos-dir . --template-source /path/to/templates.zip
```

The server listens on `127.0.0.1:8000` by default. Pass `-HostAddress`/`-Port` to the PowerShell
launcher or `--host`/`--port` to the shell launcher to change it.
