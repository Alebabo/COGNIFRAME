[CmdletBinding()]
param(
    [string]$VideosDir = ".",
    [string]$HostAddress = "127.0.0.1",
    [int]$Port = 8000,
    [string]$TemplateSource = ""
)

$pitchcraftArgs = @(
    "run",
    "--no-env-file",
    "pitchcraft",
    "--videos-dir",
    $VideosDir,
    "serve",
    "--host",
    $HostAddress,
    "--port",
    [string]$Port
)
if ($TemplateSource) {
    $pitchcraftArgs += @("--template-source", $TemplateSource)
}

& uv @pitchcraftArgs
exit $LASTEXITCODE
