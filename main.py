import getpass
import json
import lzma
import os
import re
import shutil
import subprocess
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(sys.argv[0]).resolve().parent
ENV_PATH = ROOT_DIR / ".env"
VENDOR_DIR = ROOT_DIR / "vendor" / "danser"
WORK_DIR = ROOT_DIR / "work"
DOWNLOADS_DIR = WORK_DIR / "downloads"
SONGS_DIR = WORK_DIR / "songs"
REPLAYS_DIR = WORK_DIR / "replays"
DANSER_RUNTIME_DIR = WORK_DIR / "danser-runtime"
OUTPUT_DIR = ROOT_DIR / "output"
RENDER_LOG_PATH = WORK_DIR / "render.log"
OVERLAY_LOG_PATH = WORK_DIR / "overlay.log"

OSU_OAUTH_URL = "https://osu.ppy.sh/oauth/token"
OSU_SCORE_URL = "https://osu.ppy.sh/api/v2/scores/{score_id}"
OSU_REPLAY_URL = "https://osu.ppy.sh/api/v2/scores/{score_id}/download"
OSU_BEATMAP_DOWNLOAD_URL = "https://osu.ppy.sh/beatmapsets/{beatmapset_id}/download"
DANSER_RELEASE_URL = "https://api.github.com/repos/Wieku/danser-go/releases/latest"

DEFAULT_SETTINGS_NAME = "renderer"
DEFAULT_WIDTH = 1280
DEFAULT_HEIGHT = 720
DEFAULT_FPS = 60
DEFAULT_CONTAINER = "mp4"
DEFAULT_SKIN_INPUT = str(Path("skin") / "DT Pastel")
DEFAULT_BACKGROUND_DIM = 0.7
DEFAULT_INCLUDE_BEATMAP_VIDEO = False
KEY_HOLD_OVERLAY_VERSION = 5
KEY_HOLD_OVERLAY_ENABLED = True
KEY_HOLD_OVERLAY_FPS = 60
KEY_HOLD_OVERLAY_WIDTH = 180
KEY_HOLD_OVERLAY_HEIGHT = 88
KEY_HOLD_OVERLAY_WINDOW_MS = 3100
KEY_HOLD_OVERLAY_FIRST_PRESS_MS = 6600
KEY_HOLD_OVERLAY_MARGIN_X = 10
KEY_HOLD_OVERLAY_KEY_IDLE = (42, 47, 58, 220)
KEY_HOLD_OVERLAY_TRACK_BG = (24, 27, 34, 150)
KEY_HOLD_OVERLAY_LEFT_BAR = (96, 214, 255, 235)
KEY_HOLD_OVERLAY_RIGHT_BAR = (255, 141, 109, 235)
KEY_HOLD_OVERLAY_TEXT = (255, 255, 255, 255)
KEY_HOLD_OVERLAY_LEFT_BITS = 1 | 4
KEY_HOLD_OVERLAY_RIGHT_BITS = 2 | 8
KEY_HOLD_OVERLAY_KEY_WIDTH = 32
KEY_HOLD_OVERLAY_KEY_HEIGHT = 26
KEY_HOLD_OVERLAY_TRACK_WIDTH = KEY_HOLD_OVERLAY_WIDTH - KEY_HOLD_OVERLAY_KEY_WIDTH
KEY_HOLD_OVERLAY_TRACK_HEIGHT = 20
KEY_HOLD_OVERLAY_ROW_TOPS = (12, 50)
KEY_HOLD_OVERLAY_FONT = {
    "X": ("10001", "01010", "00100", "00100", "00100", "01010", "10001"),
    "Z": ("11111", "00010", "00100", "00100", "01000", "10000", "11111"),
}
OUTPUT_METADATA_DIR_NAME = ".render-metadata"
OUTPUT_METADATA_SUFFIX = ".render.json"
EXTRACT_CACHE_NAME = ".extract-cache.json"
FAILED_LOG_TAIL_LINES = 40
RULESET_BY_ID = {
    0: "osu",
    1: "taiko",
    2: "fruits",
    3: "mania",
}

def main():
    load_dotenv_file()
    ensure_directories()
    score_input = input("Score URL or score ID: ").strip()
    if not score_input:
        fail("A score URL or score ID is required.")

    score_id = parse_score_id(score_input)

    client_id, client_secret = load_osu_credentials()
    access_token = fetch_access_token(client_id, client_secret)
    score_payload = fetch_json(
        OSU_SCORE_URL.format(score_id=score_id),
        headers={"Authorization": f"Bearer {access_token}"},
    )
    score = parse_score_info(score_payload, score_id)
    ensure_supported_mode(score)

    danser_install = prepare_danser_runtime(ensure_danser_install())
    encoder = choose_encoder(danser_install["ffmpeg"])
    skin_path = parse_skin_path("")
    settings_path = write_danser_settings(danser_install["directory"], encoder, skin_path)
    render_metadata = build_render_metadata(score, danser_install, settings_path)
    existing_output = find_existing_render(score, render_metadata)
    if existing_output is not None:
        print(f"Replay already rendered with identical settings: {existing_output}")
        return 0

    replay_path = download_replay(access_token, score)
    beatmap_archive_path = download_beatmap_archive(score)
    extract_beatmap_archive(beatmap_archive_path, score)

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

    apply_key_hold_overlay(output_path, replay_path, danser_install["ffmpeg"])
    write_render_metadata(output_path, render_metadata)
    print(f"Replay saved to: {output_path}")
    return 0


def ensure_directories():
    for directory in (VENDOR_DIR, DOWNLOADS_DIR, SONGS_DIR, REPLAYS_DIR, DANSER_RUNTIME_DIR, OUTPUT_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def parse_score_id(raw_value):
    stripped = raw_value.strip()
    if stripped.isdigit():
        return int(stripped)

    match = re.search(r"/scores/(?:[a-z]+/)?(\d+)", stripped, flags=re.IGNORECASE)
    if match is None:
        fail("Could not find a score ID in the provided input.")

    return int(match.group(1))


def load_osu_credentials():
    client_id = os.environ.get("OSU_CLIENT_ID", "").strip()
    client_secret = os.environ.get("OSU_CLIENT_SECRET", "").strip()

    if not client_id:
        client_id = input("osu! OAuth client ID: ").strip()
    if not client_secret:
        client_secret = getpass.getpass("osu! OAuth client secret: ").strip()

    if not client_id or not client_secret:
        fail("Both osu! OAuth client ID and client secret are required.")

    return client_id, client_secret


def load_dotenv_file():
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


def fetch_access_token(client_id, client_secret):
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


def parse_score_info(payload, requested_score_id):
    beatmap = as_dict(payload.get("beatmap"))
    beatmapset = as_dict(payload.get("beatmapset")) or as_dict(beatmap.get("beatmapset"))
    user = as_dict(payload.get("user"))

    beatmap_id = as_int(beatmap.get("id"))
    beatmapset_id = as_int(beatmapset.get("id") or beatmap.get("beatmapset_id"))
    mode = str(payload.get("mode") or RULESET_BY_ID.get(as_int(payload.get("ruleset_id")), "")).strip().lower()
    has_video = as_bool(beatmapset.get("video"))
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

    return {
        "score_id": requested_score_id,
        "beatmap_id": beatmap_id,
        "beatmapset_id": beatmapset_id,
        "mode": mode,
        "has_video": has_video,
        "artist": artist,
        "title": title,
        "difficulty": difficulty,
        "username": username,
    }


def ensure_supported_mode(score):
    if score["mode"] != "osu":
        fail(f"danser only supports osu!standard replays. This score is '{score['mode']}'.")


def download_replay(access_token, score):
    replay_path = REPLAYS_DIR / f"score_{score['score_id']}.osr"
    if is_cached_file(replay_path):
        print(f"Using cached replay {score['score_id']}...")
        return replay_path

    print(f"Downloading replay {score['score_id']}...")
    download_to_file(
        url=OSU_REPLAY_URL.format(score_id=score["score_id"]),
        destination=replay_path,
        headers={"Authorization": f"Bearer {access_token}"},
        accepted_content_types=("application/octet-stream", "application/x-osu-replay"),
    )
    return replay_path


def download_beatmap_archive(score):
    safe_name = sanitize_name(f"{score['beatmapset_id']} {score['artist']} - {score['title']}")
    official_cookie = os.environ.get("OSU_SESSION", "").strip()
    errors = []
    include_video_options = [DEFAULT_INCLUDE_BEATMAP_VIDEO]
    if score["has_video"] and DEFAULT_INCLUDE_BEATMAP_VIDEO != score["has_video"]:
        include_video_options.append(score["has_video"])

    for include_video in include_video_options:
        archive_variant = "video" if include_video else "no-video"
        destination = DOWNLOADS_DIR / f"{safe_name} [{archive_variant}].osz"
        if is_cached_file(destination):
            print(f"Using cached beatmapset {score['beatmapset_id']}...")
            return destination

        video_label = "with video" if include_video else "without video"
        print(f"Downloading beatmapset {score['beatmapset_id']} {video_label}...")
        attempts = []
        if official_cookie:
            attempts.append(
                (
                    build_official_beatmap_download_url(score, include_video),
                    {"Cookie": f"osu_session={official_cookie}"},
                )
            )
        attempts.append((build_catboy_download_url(score, include_video), {}))
        if include_video:
            attempts.append((f"https://api.nerinyan.moe/d/{score['beatmapset_id']}", {}))

        variant_errors = []
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
                variant_errors.append(f"{url}: {error}")

        errors.append(f"{video_label}:\n" + "\n".join(variant_errors))

    fail("Beatmap download failed.\n" + "\n".join(errors))


def extract_beatmap_archive(archive_path, score):
    destination = SONGS_DIR / sanitize_name(f"{score['beatmapset_id']} {score['artist']} - {score['title']}")
    cache_path = destination / EXTRACT_CACHE_NAME
    archive_cache = build_archive_cache_payload(archive_path)

    if should_reuse_extracted_beatmap(destination, cache_path, archive_cache):
        print(f"Using cached extraction for {archive_path.name}...")
        return destination

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

    cache_path.write_text(json.dumps(archive_cache, indent=2), encoding="utf-8")
    return destination


def ensure_danser_install():
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
    return {
        "version": version,
        "directory": install_dir,
        "executable": executable,
        "ffmpeg": ffmpeg,
    }


def prepare_danser_runtime(source_install):
    runtime_dir = DANSER_RUNTIME_DIR / source_install["version"]
    executable = find_existing_danser(runtime_dir)
    if executable is None:
        print(f"Preparing danser runtime {source_install['version']}...")
        if runtime_dir.exists():
            shutil.rmtree(runtime_dir)
        shutil.copytree(source_install["directory"], runtime_dir)
        executable = find_existing_danser(runtime_dir)
        if executable is None:
            fail(f"danser runtime {source_install['version']} is missing danser-cli.exe.")

    ffmpeg = find_existing_ffmpeg(runtime_dir)
    return {
        "version": source_install["version"],
        "directory": runtime_dir,
        "executable": executable,
        "ffmpeg": ffmpeg,
    }


def find_windows_asset_url(assets):
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name", ""))
        url = str(asset.get("browser_download_url", ""))
        if name.endswith("-win.zip") and url:
            return url
    fail("Could not find a Windows danser release asset.")


def find_existing_danser(directory):
    matches = list(directory.rglob("danser-cli.exe"))
    return matches[0] if matches else None


def find_existing_ffmpeg(directory):
    matches = list(directory.rglob("ffmpeg.exe"))
    if matches:
        return matches[0]

    path_name = shutil.which("ffmpeg")
    return Path(path_name) if path_name else None


def choose_encoder(ffmpeg_path):
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


def parse_skin_path(raw_value):
    candidate_value = raw_value or DEFAULT_SKIN_INPUT
    if not candidate_value:
        return None

    candidate = Path(candidate_value).expanduser()
    if not candidate.is_absolute():
        candidate = (ROOT_DIR / candidate).resolve()
    if not candidate.exists() or not candidate.is_dir():
        fail(f"Skin path does not exist or is not a directory: {candidate}")
    return candidate


def write_danser_settings(danser_directory, encoder, skin_path):
    settings_dir = danser_directory / "settings"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / f"{DEFAULT_SETTINGS_NAME}.json"

    general = {
        "OsuSongsDir": str(SONGS_DIR),
        "OsuSkinsDir": str(skin_path.parent if skin_path else danser_directory / "skins"),
        "OsuReplaysDir": str(REPLAYS_DIR),
        "DiscordPresenceOn": False,
        "UnpackOszFiles": True,
        "VerboseImportLogs": False,
    }

    skin_settings = {
        "CurrentSkin": skin_path.name if skin_path else "default",
        "FallbackSkin": "default",
        "UseColorsFromSkin": skin_path is not None,
        "UseBeatmapColors": False,
        "Cursor": {
            "UseSkinCursor": skin_path is not None,
            "Scale": 1,
            "TrailScale": 1,
            "ForceLongTrail": False,
            "LongTrailLength": 2048,
            "LongTrailDensity": 1,
        },
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
        "Playfield": {
            "Background": {
                "LoadVideos": True,
                "Dim": {
                    "Normal": DEFAULT_BACKGROUND_DIM,
                },
            },
        },
        "Recording": recording,
    }

    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return settings_path


def build_output_stem(score):
    base = sanitize_name(
        f"{score['artist']} - {score['title']} [{score['difficulty']}] ({score['username']}) [{score['score_id']}]"
    )
    candidate = base
    suffix = 2
    while (OUTPUT_DIR / f"{candidate}.{DEFAULT_CONTAINER}").exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def build_render_metadata(score, danser_install, settings_path):
    settings_payload = json.loads(settings_path.read_text(encoding="utf-8"))
    if not isinstance(settings_payload, dict):
        fail(f"Expected settings JSON object in {settings_path}")

    key_hold_overlay = {
        "enabled": KEY_HOLD_OVERLAY_ENABLED,
        "version": KEY_HOLD_OVERLAY_VERSION,
        "fps": KEY_HOLD_OVERLAY_FPS,
        "size": [KEY_HOLD_OVERLAY_WIDTH, KEY_HOLD_OVERLAY_HEIGHT],
        "window_ms": KEY_HOLD_OVERLAY_WINDOW_MS,
    }
    return {
        "score_id": score["score_id"],
        "danser_version": danser_install["version"],
        "settings": settings_payload,
        "key_hold_overlay": key_hold_overlay,
    }


def find_existing_render(score, render_metadata):
    score_marker = f"[{score['score_id']}]"
    for output_path in OUTPUT_DIR.glob(f"*.{DEFAULT_CONTAINER}"):
        if score_marker not in output_path.stem or not is_cached_file(output_path):
            continue
        if read_render_metadata(output_path) == render_metadata:
            return output_path
    return None


def write_render_metadata(output_path, render_metadata):
    metadata_path = get_render_metadata_path(output_path)
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(render_metadata, indent=2), encoding="utf-8")
    legacy_metadata_path = get_legacy_render_metadata_path(output_path)
    if legacy_metadata_path.exists():
        legacy_metadata_path.unlink()


def read_render_metadata(output_path):
    metadata_paths = (get_render_metadata_path(output_path), get_legacy_render_metadata_path(output_path))
    for metadata_path in metadata_paths:
        if not metadata_path.exists():
            continue

        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue

        if isinstance(payload, dict):
            return payload

    return None


def get_render_metadata_path(output_path):
    return output_path.parent / OUTPUT_METADATA_DIR_NAME / f"{output_path.name}{OUTPUT_METADATA_SUFFIX}"


def get_legacy_render_metadata_path(output_path):
    return output_path.with_suffix(output_path.suffix + OUTPUT_METADATA_SUFFIX)


def render_replay(danser_install, settings_path, replay_path, output_stem, skin_path):
    print("Rendering video with danser...")
    danser_log_path = danser_install["directory"] / "danser.log"
    command = [
        str(danser_install["executable"]),
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

    with RENDER_LOG_PATH.open("w", encoding="utf-8", errors="replace") as log_handle:
        result = subprocess.run(
            command,
            cwd=danser_install["directory"],
            check=False,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    if danser_log_path.exists():
        danser_log_path.unlink()
    if result.returncode != 0:
        fail(
            f"danser exited with code {result.returncode}.\n"
            f"Render log: {RENDER_LOG_PATH}\n"
            f"{read_log_tail(RENDER_LOG_PATH, FAILED_LOG_TAIL_LINES)}"
        )

    print(f"Render log saved to: {RENDER_LOG_PATH}")


def apply_key_hold_overlay(output_path, replay_path, ffmpeg_path):
    if not KEY_HOLD_OVERLAY_ENABLED:
        return
    if ffmpeg_path is None:
        fail("FFmpeg is required to render the key hold overlay.")

    left_intervals, right_intervals = parse_key_hold_intervals(replay_path)
    if not left_intervals and not right_intervals:
        return

    duration = probe_media_duration(output_path, find_existing_ffprobe(ffmpeg_path))
    source_path = output_path.with_name(f"{output_path.stem}.danser{output_path.suffix}")
    if source_path.exists():
        source_path.unlink()
    output_path.replace(source_path)

    filter_graph = (
        f"[0:v][1:v]overlay="
        f"x=W-w-{KEY_HOLD_OVERLAY_MARGIN_X}:"
        f"y=H*0.80-h:"
        f"eof_action=pass:format=auto[v]"
    )
    command = [
        str(ffmpeg_path),
        "-y",
        "-i",
        str(source_path),
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgba",
        "-s",
        f"{KEY_HOLD_OVERLAY_WIDTH}x{KEY_HOLD_OVERLAY_HEIGHT}",
        "-r",
        str(KEY_HOLD_OVERLAY_FPS),
        "-i",
        "-",
        "-filter_complex",
        filter_graph,
        "-map",
        "[v]",
        "-map",
        "0:a?",
        "-c:v",
        "libx264",
        "-preset",
        "faster",
        "-crf",
        "14",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "copy",
        "-movflags",
        "+faststart",
        str(output_path),
    ]

    error = None
    with OVERLAY_LOG_PATH.open("w", encoding="utf-8", errors="replace") as log_handle:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log_handle, stderr=subprocess.STDOUT)
        try:
            write_key_hold_overlay_frames(process.stdin, duration, left_intervals, right_intervals)
        except OSError as exc:
            error = str(exc)
        finally:
            if process.stdin is not None:
                process.stdin.close()
        if process.wait() != 0 and error is None:
            error = f"FFmpeg overlay failed.\nOverlay log: {OVERLAY_LOG_PATH}"

    if error is not None or not output_path.exists():
        if output_path.exists():
            output_path.unlink()
        if source_path.exists():
            source_path.replace(output_path)
        fail(error or f"Overlay output was not created.\nOverlay log: {OVERLAY_LOG_PATH}")

    source_path.unlink()


def find_existing_ffprobe(ffmpeg_path):
    local_path = ffmpeg_path.with_name("ffprobe.exe")
    if local_path.exists():
        return local_path
    path_name = shutil.which("ffprobe")
    if path_name:
        return Path(path_name)
    fail("Could not find ffprobe for the key hold overlay.")


def probe_media_duration(path, ffprobe_path):
    result = subprocess.run(
        [str(ffprobe_path), "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        fail(f"ffprobe could not read {path}.\n{result.stderr.strip()}")
    payload = json.loads(result.stdout or "{}")
    duration = float(as_dict(payload.get("format")).get("duration") or 0)
    if duration <= 0:
        fail(f"ffprobe did not return a valid duration for {path}.")
    return duration


def parse_key_hold_intervals(replay_path):
    left_intervals = []
    right_intervals = []
    left_start = 0
    right_start = 0
    left_down = False
    right_down = False
    raw_time = 0
    current_time = 0
    replay_data = read_replay_data(replay_path)

    for event in replay_data.split(","):
        parts = event.split("|")
        if len(parts) < 4:
            continue
        try:
            delta = int(float(parts[0]))
            keys = int(float(parts[3]))
        except ValueError:
            continue
        if delta == -12345:
            break
        raw_time += delta
        current_time = max(0, raw_time)
        left_now = bool(keys & KEY_HOLD_OVERLAY_LEFT_BITS)
        right_now = bool(keys & KEY_HOLD_OVERLAY_RIGHT_BITS)
        if left_now and not left_down:
            left_start = current_time
        if right_now and not right_down:
            right_start = current_time
        if left_down and not left_now:
            left_intervals.append((left_start, current_time))
        if right_down and not right_now:
            right_intervals.append((right_start, current_time))
        left_down = left_now
        right_down = right_now

    if left_down:
        left_intervals.append((left_start, current_time))
    if right_down:
        right_intervals.append((right_start, current_time))

    all_intervals = left_intervals + right_intervals
    if not all_intervals:
        return left_intervals, right_intervals

    first_press = min(start for start, _ in all_intervals)
    shift = first_press - KEY_HOLD_OVERLAY_FIRST_PRESS_MS
    if shift == 0:
        return left_intervals, right_intervals

    left_intervals = [(start - shift, end - shift) for start, end in left_intervals]
    right_intervals = [(start - shift, end - shift) for start, end in right_intervals]
    return left_intervals, right_intervals


def read_replay_data(replay_path):
    data = replay_path.read_bytes()
    offset = 1 + 4
    for _ in range(3):
        _, offset = read_osu_string(data, offset)
    offset += 6 * 2 + 4 + 2 + 1 + 4
    _, offset = read_osu_string(data, offset)
    offset += 8
    replay_length = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    return lzma.decompress(data[offset:offset + replay_length]).decode("utf-8", errors="replace")


def read_osu_string(data, offset):
    marker = data[offset]
    offset += 1
    if marker == 0:
        return "", offset
    if marker != 0x0B:
        fail(f"Unexpected osu! string marker: {marker}")
    length, offset = read_uleb128(data, offset)
    value = data[offset:offset + length].decode("utf-8", errors="replace")
    return value, offset + length


def read_uleb128(data, offset):
    value = 0
    shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            return value, offset
        shift += 7


def write_key_hold_overlay_frames(pipe, duration, left_intervals, right_intervals):
    base_frame = create_key_hold_overlay_base()
    frame_time_step = 1000 / KEY_HOLD_OVERLAY_FPS
    frame_count = max(1, int(duration * KEY_HOLD_OVERLAY_FPS + 0.999))

    for frame_index in range(frame_count):
        frame_time = frame_index * frame_time_step
        pipe.write(build_key_hold_overlay_frame(base_frame, frame_time, left_intervals, right_intervals))


def create_key_hold_overlay_base():
    frame = bytearray(KEY_HOLD_OVERLAY_WIDTH * KEY_HOLD_OVERLAY_HEIGHT * 4)
    draw_key_hold_lane_base(frame, KEY_HOLD_OVERLAY_ROW_TOPS[0], "Z")
    draw_key_hold_lane_base(frame, KEY_HOLD_OVERLAY_ROW_TOPS[1], "X")
    return bytes(frame)


def build_key_hold_overlay_frame(base_frame, frame_time, left_intervals, right_intervals):
    frame = bytearray(base_frame)
    draw_key_hold_lane(frame, KEY_HOLD_OVERLAY_ROW_TOPS[0], "Z", KEY_HOLD_OVERLAY_LEFT_BAR, left_intervals, frame_time)
    draw_key_hold_lane(frame, KEY_HOLD_OVERLAY_ROW_TOPS[1], "X", KEY_HOLD_OVERLAY_RIGHT_BAR, right_intervals, frame_time)
    return frame


def draw_key_hold_lane_base(frame, top, label):
    key_left = KEY_HOLD_OVERLAY_WIDTH - KEY_HOLD_OVERLAY_KEY_WIDTH
    track_left = key_left - KEY_HOLD_OVERLAY_TRACK_WIDTH
    track_top = top + (KEY_HOLD_OVERLAY_KEY_HEIGHT - KEY_HOLD_OVERLAY_TRACK_HEIGHT) // 2
    fill_rect(frame, track_left, track_top, KEY_HOLD_OVERLAY_TRACK_WIDTH, KEY_HOLD_OVERLAY_TRACK_HEIGHT, KEY_HOLD_OVERLAY_TRACK_BG)
    fill_rect(frame, key_left, top, KEY_HOLD_OVERLAY_KEY_WIDTH, KEY_HOLD_OVERLAY_KEY_HEIGHT, KEY_HOLD_OVERLAY_KEY_IDLE)
    draw_glyph(frame, key_left + 8, top + 3, label, KEY_HOLD_OVERLAY_TEXT, 3)


def draw_key_hold_lane(frame, top, label, color, intervals, frame_time):
    key_left = KEY_HOLD_OVERLAY_WIDTH - KEY_HOLD_OVERLAY_KEY_WIDTH
    track_left = key_left - KEY_HOLD_OVERLAY_TRACK_WIDTH
    track_right = key_left
    track_top = top + (KEY_HOLD_OVERLAY_KEY_HEIGHT - KEY_HOLD_OVERLAY_TRACK_HEIGHT) // 2
    active = False

    for start, end in intervals:
        if frame_time < start:
            break
        if frame_time - end >= KEY_HOLD_OVERLAY_WINDOW_MS:
            continue
        if start <= frame_time < end:
            active = True
        draw_key_hold_note(frame, track_left, track_right, track_top, color, frame_time, start, end)

    if active:
        fill_rect(frame, key_left, top, KEY_HOLD_OVERLAY_KEY_WIDTH, KEY_HOLD_OVERLAY_KEY_HEIGHT, color)
        draw_glyph(frame, key_left + 8, top + 3, label, KEY_HOLD_OVERLAY_TEXT, 3)


def draw_key_hold_note(frame, track_left, track_right, track_top, color, frame_time, start, end):
    px_per_ms = KEY_HOLD_OVERLAY_TRACK_WIDTH / KEY_HOLD_OVERLAY_WINDOW_MS
    left = int(track_right - (frame_time - start) * px_per_ms)
    right_time = end if frame_time >= end else frame_time
    right = int(track_right - (frame_time - right_time) * px_per_ms)
    left = max(track_left, left)
    right = min(track_right, right)
    if right <= left:
        return
    fill_rect(frame, left, track_top, right - left, KEY_HOLD_OVERLAY_TRACK_HEIGHT, color)


def draw_glyph(frame, x, y, glyph, color, scale):
    for row_index, row in enumerate(KEY_HOLD_OVERLAY_FONT[glyph]):
        for col_index, cell in enumerate(row):
            if cell == "1":
                fill_rect(frame, x + col_index * scale, y + row_index * scale, scale, scale, color)


def fill_rect(frame, x, y, width, height, color):
    stride = KEY_HOLD_OVERLAY_WIDTH * 4
    row = bytes(color) * width
    for row_index in range(y, y + height):
        start = row_index * stride + x * 4
        frame[start:start + len(row)] = row


def fetch_json(url, *, headers=None, data=None):
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


def download_to_file(url, destination, *, headers, accepted_content_types):
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


def content_type_matches(content_type, accepted):
    normalized = content_type.lower().strip()
    return any(normalized == value or normalized.startswith(f"{value};") for value in accepted)


def build_official_beatmap_download_url(score, include_video):
    url = OSU_BEATMAP_DOWNLOAD_URL.format(beatmapset_id=score["beatmapset_id"])
    if include_video:
        return url
    return f"{url}?noVideo=1"


def build_catboy_download_url(score, include_video):
    suffix = "" if include_video else "n"
    return f"https://catboy.best/d/{score['beatmapset_id']}{suffix}"


def is_cached_file(path):
    return path.exists() and path.is_file() and path.stat().st_size > 0


def build_archive_cache_payload(archive_path):
    archive_stat = archive_path.stat()
    return {
        "archive_name": archive_path.name,
        "archive_size": archive_stat.st_size,
        "archive_mtime_ns": archive_stat.st_mtime_ns,
    }


def should_reuse_extracted_beatmap(destination, cache_path, archive_cache):
    if not destination.exists() or not destination.is_dir() or not cache_path.exists():
        return False

    try:
        cached_payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    if cached_payload != archive_cache:
        return False

    return any(destination.rglob("*.osu"))


def sanitize_name(raw_value):
    collapsed = re.sub(r"\s+", " ", raw_value).strip()
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", collapsed)
    sanitized = sanitized.rstrip(". ")
    return sanitized or "output"


def read_log_tail(path, line_count):
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return "Could not read the render log."

    tail = lines[-line_count:]
    if not tail:
        return "Render log is empty."

    return "\n".join(tail)


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def as_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return value != 0
    return False


def fail(message):
    print(message, file=sys.stderr)
    raise SystemExit(1)

if __name__ == "__main__":
    sys.exit(main())
