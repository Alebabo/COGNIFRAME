[CmdletBinding()]
param(
    [string]$VideosDir = ".",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [string]$TemplateSource = ""
)

$palantumArgs = @(
    "run",
    "--no-env-file",
    "palantum",
    "--videos-dir",
    $VideosDir,
    "serve",
    "--host",
    $HostAddress,
    "--port",
    [string]$Port
)
if ($TemplateSource) {
    $palantumArgs += @("--template-source", $TemplateSource)
}

& uv @palantumArgs
exit $LASTEXITCODE
