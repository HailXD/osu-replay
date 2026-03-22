import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent
ENV_PATH = ROOT_DIR / ".env"
VENDOR_DIR = ROOT_DIR / "vendor" / "danser"
WORK_DIR = ROOT_DIR / "work"
DOWNLOADS_DIR = WORK_DIR / "downloads"
SONGS_DIR = WORK_DIR / "songs"
REPLAYS_DIR = WORK_DIR / "replays"
OUTPUT_DIR = ROOT_DIR / "output"

OSU_OAUTH_URL = "https://osu.ppy.sh/oauth/token"
OSU_SCORE_URL = "https://osu.ppy.sh/api/v2/scores/{score_id}"
OSU_REPLAY_URL = "https://osu.ppy.sh/api/v2/scores/{score_id}/download"
OSU_BEATMAP_DOWNLOAD_URL = "https://osu.ppy.sh/beatmapsets/{beatmapset_id}/download?noVideo=1"
DANSER_RELEASE_URL = "https://api.github.com/repos/Wieku/danser-go/releases/latest"

DEFAULT_SETTINGS_NAME = "renderer"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 60
DEFAULT_CONTAINER = "mp4"
DEFAULT_SKIN_INPUT = ""
RULESET_BY_ID = {
    0: "osu",
    1: "taiko",
    2: "fruits",
    3: "mania",
}

client_id = "50062"
client_secret = "yA26yn19pCaLU8MKgmhCGVhCTvHpr4XPXpdYKZp5"
    
@dataclass(frozen=True)
class ScoreInfo:
    score_id: int
    beatmap_id: int
    beatmapset_id: int
    mode: str
    artist: str
    title: str
    difficulty: str
    username: str


@dataclass(frozen=True)
class DanserInstall:
    version: str
    directory: Path
    executable: Path
    ffmpeg: Path | None


def main() -> int:
    load_dotenv_file()
    ensure_directories()
    score_input = input("Score URL or score ID: ").strip()
    if not score_input:
        fail("A score URL or score ID is required.")

    score_id = parse_score_id(score_input)
    skin_input = input("Optional local skin folder path (blank = default): ").strip()

    client_id, client_secret = load_osu_credentials()
    access_token = fetch_access_token(client_id, client_secret)
    score_payload = fetch_json(
        OSU_SCORE_URL.format(score_id=score_id),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    score = parse_score_info(score_payload, score_id)
    ensure_supported_mode(score)

    replay_path = download_replay(access_token, score)
    beatmap_archive_path = download_beatmap_archive(score)
    extract_beatmap_archive(beatmap_archive_path, score)

    danser_install = ensure_danser_install()
    encoder = choose_encoder(danser_install.ffmpeg)
    skin_path = parse_skin_path(skin_input)
    settings_path = write_danser_settings(danser_install.directory, encoder, skin_path)
    output_stem = build_output_stem(score)
    output_path = OUTPUT_DIR / f"{output_stem}.{DEFAULT_CONTAINER}"

    render_replay(
        danser_install=danser_install,
        settings_path=settings_path,
        replay_path=replay_path,
        output_stem=output_stem,
        skin_path=skin_path,
    )

    if not output_path.exists():
        fail(f"Danser finished but the output file was not created: {output_path}")

    print(f"Replay saved to: {output_path}")
    return 0


def ensure_directories() -> None:
    for directory in (VENDOR_DIR, DOWNLOADS_DIR, SONGS_DIR, REPLAYS_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def parse_score_id(raw_value: str) -> int:
    stripped = raw_value.strip()
    if stripped.isdigit():
        return int(stripped)

    match = re.search(r"/scores/(?:[a-z]+/)?(\d+)", stripped, flags=re.IGNORECASE)
    if match is None:
        fail("Could not find a score ID in the provided input.")

    return int(match.group(1))


def load_osu_credentials() -> tuple[str, str]:
    client_id = os.environ.get("OSU_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OSU_CLIENT_SECRET", "").strip()

    if not client_id:
        client_id = input("osu! OAuth client ID: ").strip()
    if not client_secret:
        client_secret = getpass.getpass("osu! OAuth client secret: ").strip()

    if not client_id or not client_secret:
        fail("Both osu! OAuth client ID and client secret are required.")

    return client_id, client_secret


def load_dotenv_file() -> None:
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        os.environ.setdefault(key, value)


def fetch_access_token(client_id: str, client_secret: str) -> str:
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials",
        "scope": "public",
    }
    response = fetch_json(OSU_OAUTH_URL, data=payload)
    token = str(response.get("access_token", "")).strip()
    if not token:
        fail("osu! OAuth did not return an access token.")
    return token


def parse_score_info(payload: dict[str, Any], requested_score_id: int) -> ScoreInfo:
    beatmap = as_dict(payload.get("beatmap"))
    beatmapset = as_dict(payload.get("beatmapset")) or as_dict(beatmap.get("beatmapset"))
    user = as_dict(payload.get("user"))

    beatmap_id = as_int(beatmap.get("id"))
    beatmapset_id = as_int(beatmapset.get("id") or beatmap.get("beatmapset_id"))
    mode = str(payload.get("mode") or RULESET_BY_ID.get(as_int(payload.get("ruleset_id")), "")).strip().lower()
    artist = str(beatmapset.get("artist") or "unknown artist").strip()
    title = str(beatmapset.get("title") or "unknown title").strip()
    difficulty = str(beatmap.get("version") or f"beatmap {beatmap_id}").strip()
    username = str(user.get("username") or "unknown user").strip()

    if beatmap_id <= 0:
        fail("The score payload did not contain a valid beatmap ID.")
    if beatmapset_id <= 0:
        fail("The score payload did not contain a valid beatmapset ID.")
    if not mode:
        fail("The score payload did not contain a ruleset/mode.")

    return ScoreInfo(
        score_id=requested_score_id,
        beatmap_id=beatmap_id,
        beatmapset_id=beatmapset_id,
        mode=mode,
        artist=artist,
        title=title,
        difficulty=difficulty,
        username=username,
    )


def ensure_supported_mode(score: ScoreInfo) -> None:
    if score.mode != "osu":
        fail(f"danser only supports osu!standard replays. This score is '{score.mode}'.")


def download_replay(access_token: str, score: ScoreInfo) -> Path:
    replay_path = REPLAYS_DIR / f"score_{score.score_id}.osr"
    print(f"Downloading replay {score.score_id}...")
    download_to_file(
        url=OSU_REPLAY_URL.format(score_id=score.score_id),
        destination=replay_path,
        headers={"Authorization": f"Bearer {access_token}"},
        accepted_content_types=("application/octet-stream", "application/x-osu-replay"),
    )
    return replay_path


def download_beatmap_archive(score: ScoreInfo) -> Path:
    print(f"Downloading beatmapset {score.beatmapset_id}...")
    safe_name = sanitize_name(f"{score.beatmapset_id} {score.artist} - {score.title}")
    destination = DOWNLOADS_DIR / f"{safe_name}.osz"
    official_cookie = os.environ.get("OSU_SESSION", "").strip()

    attempts: list[tuple[str, dict[str, str]]] = []
    if official_cookie:
        attempts.append(
            (
                OSU_BEATMAP_DOWNLOAD_URL.format(beatmapset_id=score.beatmapset_id),
                {"Cookie": f"osu_session={official_cookie}"},
            )
        )
    attempts.append((f"https://catboy.best/d/{score.beatmapset_id}n", {}))
    attempts.append((f"https://api.nerinyan.moe/d/{score.beatmapset_id}", {}))

    errors: list[str] = []
    for url, headers in attempts:
        try:
            download_to_file(
                url=url,
                destination=destination,
                headers=headers,
                accepted_content_types=("application/x-osu-beatmap-archive", "application/octet-stream", "application/zip"),
            )
            return destination
        except RuntimeError as error:
            errors.append(f"{url}: {error}")

    fail("Beatmap download failed.\n" + "\n".join(errors))


def extract_beatmap_archive(archive_path: Path, score: ScoreInfo) -> Path:
    destination = SONGS_DIR / sanitize_name(f"{score.beatmapset_id} {score.artist} - {score.title}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)

    print(f"Extracting {archive_path.name}...")
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            member_path = destination / member.filename
            resolved_path = member_path.resolve()
            if not str(resolved_path).startswith(str(destination.resolve())):
                fail(f"Unsafe archive entry detected: {member.filename}")
            archive.extract(member, destination)

    osu_files = list(destination.rglob("*.osu"))
    if not osu_files:
        fail("The beatmap archive extracted successfully but did not contain any .osu files.")

    return destination


def ensure_danser_install() -> DanserInstall:
    release = fetch_json(DANSER_RELEASE_URL)
    version = str(release.get("tag_name", "")).strip()
    assets = release.get("assets")

    if not version or not isinstance(assets, list):
        fail("GitHub did not return a usable danser release payload.")

    install_dir = VENDOR_DIR / version
    executable = find_existing_danser(install_dir)
    if executable is None:
        print(f"Downloading danser {version}...")
        archive_url = find_windows_asset_url(assets)
        archive_path = DOWNLOADS_DIR / f"danser-{version}-win.zip"
        download_to_file(
            archive_url,
            archive_path,
            headers={},
            accepted_content_types=("application/zip", "application/octet-stream"),
        )
        if install_dir.exists():
            shutil.rmtree(install_dir)
        install_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            archive.extractall(install_dir)
        executable = find_existing_danser(install_dir)
        if executable is None:
            fail(f"danser {version} was extracted but danser-cli.exe was not found.")

    ffmpeg = find_existing_ffmpeg(install_dir)
    return DanserInstall(version=version, directory=install_dir, executable=executable, ffmpeg=ffmpeg)


def find_windows_asset_url(assets: list[Any]) -> str:
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if name.endswith("-win.zip") and url:
            return url
    fail("Could not find a Windows danser release asset.")


def find_existing_danser(directory: Path) -> Path | None:
    matches = list(directory.rglob("danser-cli.exe"))
    return matches[0] if matches else None


def find_existing_ffmpeg(directory: Path) -> Path | None:
    matches = list(directory.rglob("ffmpeg.exe"))
    if matches:
        return matches[0]

    path_name = shutil.which("ffmpeg")
    return Path(path_name) if path_name else None


def choose_encoder(ffmpeg_path: Path | None) -> dict[str, Any]:
    has_nvenc = False
    if ffmpeg_path is not None and shutil.which("nvidia-smi"):
        result = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
        )
        has_nvenc = result.returncode == 0 and "h264_nvenc" in result.stdout

    if has_nvenc:
        print("Using h264_nvenc for rendering.")
        return {
            "Encoder": "h264_nvenc",
            "h264_nvenc": {
                "RateControl": "cq",
                "Bitrate": "10M",
                "CQ": 20,
                "Profile": "high",
                "Preset": "p7",
                "AdditionalOptions": "",
            },
        }

    print("Using libx264 for rendering.")
    return {
        "Encoder": "libx264",
        "libx264": {
            "RateControl": "crf",
            "Bitrate": "10M",
            "CRF": 14,
            "Profile": "high",
            "Preset": "faster",
            "AdditionalOptions": "",
        },
    }


def parse_skin_path(raw_value: str) -> Path | None:
    if not raw_value:
        return None

    candidate = Path(raw_value).expanduser()
    if not candidate.is_absolute():
        candidate = (ROOT_DIR / candidate).resolve()
    if not candidate.exists() or not candidate.is_dir():
        fail(f"Skin path does not exist or is not a directory: {candidate}")
    return candidate


def write_danser_settings(danser_directory: Path, encoder: dict[str, Any], skin_path: Path | None) -> Path:
    settings_dir = danser_directory / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / f"{DEFAULT_SETTINGS_NAME}.json"

    general: dict[str, Any] = {
        "OsuSongsDir": str(SONGS_DIR),
        "OsuSkinsDir": str(skin_path.parent if skin_path else danser_directory / "skins"),
        "OsuReplaysDir": str(REPLAYS_DIR),
        "DiscordPresenceOn": False,
        "UnpackOszFiles": True,
        "VerboseImportLogs": False,
    }

    skin_settings: dict[str, Any] = {
        "CurrentSkin": skin_path.name if skin_path else "default",
        "FallbackSkin": "default",
        "UseColorsFromSkin": False,
        "UseBeatmapColors": False,
    }

    recording = {
        "FrameWidth": DEFAULT_WIDTH,
        "FrameHeight": DEFAULT_HEIGHT,
        "FPS": DEFAULT_FPS,
        "EncodingFPSCap": 0,
        "PixelFormat": "yuv420p",
        "AudioCodec": "aac",
        "aac": {
            "Bitrate": "192k",
            "AdditionalOptions": "",
        },
        "OutputDir": str(OUTPUT_DIR),
        "Container": DEFAULT_CONTAINER,
        "ShowFFmpegLogs": True,
        "MotionBlur": {
            "Enabled": False,
            "OversampleMultiplier": 16,
            "BlendFrames": 24,
            "BlendFunctionID": 27,
            "GaussWeightsMult": 1.5,
        },
    }
    recording.update(encoder)

    payload = {
        "General": general,
        "Skin": skin_settings,
        "Recording": recording,
    }

    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return settings_path


def build_output_stem(score: ScoreInfo) -> str:
    base = sanitize_name(
        f"{score.artist} - {score.title} [{score.difficulty}] ({score.username}) [{score.score_id}]"
    )
    candidate = base
    suffix = 2
    while (OUTPUT_DIR / f"{candidate}.{DEFAULT_CONTAINER}").exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def render_replay(
    danser_install: DanserInstall,
    settings_path: Path,
    replay_path: Path,
    output_stem: str,
    skin_path: Path | None,
) -> None:
    print("Rendering video with danser...")
    command = [
        str(danser_install.executable),
        f"-settings={settings_path.stem}",
        "-record",
        f"-replay={replay_path}",
        f"-out={output_stem}",
        "-quickstart",
        "-preciseprogress",
        "-noupdatecheck",
    ]
    if skin_path is not None:
        command.append(f"-skin={skin_path.name}")

    result = subprocess.run(command, cwd=danser_install.directory, check=False)
    if result.returncode != 0:
        fail(f"danser exited with code {result.returncode}.")


def fetch_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    raw_data = None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "osu-replay-renderer/1.0",
    }
    if headers:
        request_headers.update(headers)
    if data is not None:
        raw_data = json.dumps(data).encode("utf-8")
        request_headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, headers=request_headers, data=raw_data, method="POST" if raw_data else "GET")
    try:
        with urllib.request.urlopen(request) as response:
            charset = response.headers.get_content_charset("utf-8")
            payload = json.loads(response.read().decode(charset))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        fail(f"Request failed: {url}\nHTTP {error.code}\n{body}")
    except urllib.error.URLError as error:
        fail(f"Request failed: {url}\n{error}")

    if not isinstance(payload, dict):
        fail(f"Expected a JSON object from {url}.")
    return payload


def download_to_file(
    url: str,
    destination: Path,
    *,
    headers: dict[str, str],
    accepted_content_types: tuple[str, ...],
) -> None:
    request_headers = {
        "Accept": "*/*",
        "User-Agent": "osu-replay-renderer/1.0",
    }
    request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)

    try:
        with urllib.request.urlopen(request) as response:
            content_type = response.headers.get_content_type()
            if not content_type_matches(content_type, accepted_content_types):
                body_preview = response.read(200).decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"unexpected content type '{content_type}' from {response.geturl()}\n{body_preview}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("wb") as handle:
                shutil.copyfileobj(response, handle)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}\n{body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(str(error)) from error


def content_type_matches(content_type: str, accepted: tuple[str, ...]) -> bool:
    normalized = content_type.lower().strip()
    return any(normalized == value or normalized.startswith(f"{value};") for value in accepted)


def sanitize_name(raw_value: str) -> str:
    collapsed = re.sub(r"\s+", " ", raw_value).strip()
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", collapsed)
    sanitized = sanitized.rstrip(". ")
    return sanitized or "output"


def as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def fail(message: str) -> None:
    print(message, file=sys.stderr)
    raise SystemExit(1)


if __name__ == "__main__":
    raise SystemExit(main())
