param(
    [Parameter(Mandatory = $true)]
    [string]$Tag,

    [string]$Registry = "ghcr.io",
    [string]$Owner = "bl0rb",
    [string]$Platform = "linux/amd64,linux/arm64",
    [string]$Builder = "paddledock-builder",
    [switch]$AlsoLatest
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI not found in PATH."
}

$null = & docker buildx version
if ($LASTEXITCODE -ne 0) {
    throw "Docker Buildx is required. Enable it in Docker Desktop."
}

Write-Host "Ensuring buildx builder '$Builder' exists..."
$existingBuilders = docker buildx ls | Out-String
if ($existingBuilders -notmatch [regex]::Escape($Builder)) {
    docker buildx create --name $Builder --use | Out-Null
} else {
    docker buildx use $Builder | Out-Null
}
docker buildx inspect --bootstrap | Out-Null

function Publish-Image {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [string]$Dockerfile,
        [Parameter(Mandatory = $true)]
        [string]$Context
    )

    $imageBase = "$Registry/$Owner/paddledock-$Name"
    $tags = @("$imageBase`:$Tag")
    if ($AlsoLatest) {
        $tags += "$imageBase`:latest"
    }

    $tagArgs = @()
    foreach ($t in $tags) {
        $tagArgs += "-t"
        $tagArgs += $t
    }

    Write-Host "Publishing $imageBase with tag '$Tag'..."
    docker buildx build --platform $Platform -f $Dockerfile $Context --push @tagArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Failed publishing image: $imageBase"
    }
}

Write-Host "You must be logged in to GHCR (docker login ghcr.io) before running this script."

Publish-Image -Name "backend" -Dockerfile "backend/Dockerfile" -Context "backend"
Publish-Image -Name "worker" -Dockerfile "backend/worker.Dockerfile" -Context "backend"
Publish-Image -Name "frontend" -Dockerfile "frontend/Dockerfile" -Context "frontend"

Write-Host "Done. Published images under $Registry/$Owner with tag '$Tag'."
