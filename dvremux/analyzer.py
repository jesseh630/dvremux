import json
from pathlib import Path


def _fps(value):
    if not value or value == "0/0":
        return "Unknown"

    try:
        numerator, denominator = value.split("/")
        result = float(numerator) / float(denominator)
        return f"{result:.3f}".rstrip("0").rstrip(".")
    except Exception:
        return value


def _size(value):
    try:
        size = float(value)
    except (TypeError, ValueError):
        return "Unknown"

    units = ["B", "KB", "MB", "GB", "TB"]

    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{size:.0f} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024

    return "Unknown"


def _duration(value):
    try:
        seconds = int(round(float(value)))
    except (TypeError, ValueError):
        return "Unknown"

    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    return f"{hours}:{minutes:02d}:{seconds:02d}"


def _language(stream):
    return stream.get("tags", {}).get("language", "und")


def _title(stream):
    return stream.get("tags", {}).get("title", "")


def analyze_ffprobe_json(raw_json, source_path):
    data = json.loads(raw_json)

    streams = data.get("streams", [])
    container = data.get("format", {})

    video_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "video"
    ]

    audio_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "audio"
    ]

    subtitle_streams = [
        stream for stream in streams
        if stream.get("codec_type") == "subtitle"
    ]

    result = {
        "source": str(Path(source_path)),
        "filename": Path(source_path).name,
        "size": _size(container.get("size")),
        "duration": _duration(container.get("duration")),
        "format": container.get("format_long_name")
                  or container.get("format_name")
                  or "Unknown",
        "video": [],
        "audio": [],
        "subtitles": [],
    }

    for stream in video_streams:
        dovi = None

        for side_data in stream.get("side_data_list", []):
            if side_data.get("side_data_type") == "DOVI configuration record":
                dovi = {
                    "profile": side_data.get("dv_profile"),
                    "level": side_data.get("dv_level"),
                    "compatibility_id":
                        side_data.get("dv_bl_signal_compatibility_id"),
                    "rpu_present":
                        bool(side_data.get("rpu_present_flag")),
                    "el_present":
                        bool(side_data.get("el_present_flag")),
                    "bl_present":
                        bool(side_data.get("bl_present_flag")),
                }
                break

        result["video"].append({
            "index": stream.get("index"),
            "codec": stream.get("codec_name", "Unknown"),
            "profile": stream.get("profile", ""),
            "width": stream.get("width"),
            "height": stream.get("height"),
            "fps": _fps(
                stream.get("avg_frame_rate")
                or stream.get("r_frame_rate")
            ),
            "fps_raw": (
                stream.get("avg_frame_rate")
                or stream.get("r_frame_rate")
                or ""
            ),
            "pixel_format": stream.get("pix_fmt", ""),
            "bit_depth":
                stream.get("bits_per_raw_sample")
                or stream.get("bits_per_sample")
                or "",
            "color_primaries": stream.get("color_primaries", ""),
            "color_transfer": stream.get("color_transfer", ""),
            "color_space": stream.get("color_space", ""),
            "dolby_vision": dovi,
        })

    for stream in audio_streams:
        result["audio"].append({
            "index": stream.get("index"),
            "codec": stream.get("codec_name", "Unknown"),
            "profile": stream.get("profile", ""),
            "channels": stream.get("channels"),
            "layout": stream.get("channel_layout", ""),
            "language": _language(stream),
            "title": _title(stream),
        })

    for stream in subtitle_streams:
        result["subtitles"].append({
            "index": stream.get("index"),
            "codec": stream.get("codec_name", "Unknown"),
            "language": _language(stream),
            "title": _title(stream),
        })

    return result


def build_summary(result):
    lines = []

    lines.append("SOURCE")
    lines.append(result["filename"])
    lines.append(
        f'{result["size"]}   •   {result["duration"]}'
    )

    if result["video"]:
        video = result["video"][0]

        lines.append("")
        lines.append("VIDEO")

        resolution = "Unknown"
        if video["width"] and video["height"]:
            resolution = f'{video["width"]} × {video["height"]}'

        codec = video["codec"].upper()

        description = (
            f'{resolution}   •   {codec}   •   '
            f'{video["fps"]} fps'
        )

        if video["pixel_format"]:
            description += f'   •   {video["pixel_format"]}'

        lines.append(description)

        if video["profile"]:
            lines.append(f'Codec profile: {video["profile"]}')

        dovi = video["dolby_vision"]

        if dovi:
            profile = dovi["profile"]
            level = dovi["level"]
            compatibility = dovi["compatibility_id"]

            lines.append("")
            lines.append("DOLBY VISION")
            lines.append(
                f'Profile {profile}   •   Level {level}   •   '
                f'Compatibility ID {compatibility}'
            )

            structure = []

            if dovi["bl_present"]:
                structure.append("BL")
            if dovi["el_present"]:
                structure.append("EL")
            if dovi["rpu_present"]:
                structure.append("RPU")

            if structure:
                lines.append("Structure: " + " + ".join(structure))
        else:
            lines.append("")
            lines.append("DOLBY VISION")
            lines.append("Not detected by ffprobe")

    lines.append("")
    lines.append(f'AUDIO   ({len(result["audio"])})')

    if result["audio"]:
        for audio in result["audio"]:
            parts = [
                f'#{audio["index"]}',
                audio["codec"].upper(),
            ]

            if audio["profile"]:
                parts.append(audio["profile"])

            if audio["layout"]:
                parts.append(audio["layout"])
            elif audio["channels"]:
                parts.append(f'{audio["channels"]} ch')

            if audio["language"]:
                parts.append(audio["language"])

            if audio["title"]:
                parts.append(audio["title"])

            lines.append("   •   ".join(parts))
    else:
        lines.append("None")

    lines.append("")
    lines.append(f'SUBTITLES   ({len(result["subtitles"])})')

    if result["subtitles"]:
        for subtitle in result["subtitles"]:
            parts = [
                f'#{subtitle["index"]}',
                subtitle["codec"].upper(),
                subtitle["language"],
            ]

            if subtitle["title"]:
                parts.append(subtitle["title"])

            lines.append("   •   ".join(parts))
    else:
        lines.append("None")

    return "\n".join(lines)
