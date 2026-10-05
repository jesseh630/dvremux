from pathlib import Path


def build_plan(analysis, deep_dovi=None):
    video_streams = analysis.get("video", [])
    audio_streams = analysis.get("audio", [])

    if not video_streams:
        return {
            "ready": False,
            "text": "NOT READY\n\nNo video stream detected.",
        }

    video = video_streams[0]
    codec = (video.get("codec") or "").lower()

    if codec != "hevc":
        return {
            "ready": False,
            "text": (
                "NOT READY\n\n"
                f"Video codec: {codec.upper() or 'UNKNOWN'}\n"
                "DV Remux currently requires HEVC video."
            ),
        }

    dovi = video.get("dolby_vision")
    source_profile = None
    dv_type = None

    if deep_dovi:
        source_profile = deep_dovi.get("profile")
        dv_type = deep_dovi.get("type")

    if source_profile is None and dovi:
        source_profile = dovi.get("profile")

    lines = [
        "VIDEO",
        "PRESERVE / STREAM COPY — HEVC video will not be re-encoded",
        "",
        "DOLBY VISION",
    ]

    ready = True

    if source_profile == 7:
        source_name = "Profile 7"

        if dv_type:
            source_name += f" {dv_type}"

        lines.append(
            f"CONVERT — {source_name} → Profile 8.1"
        )

    elif source_profile == 8:
        compat = dovi.get("compatibility_id") if dovi else None

        if compat == 1:
            lines.append(
                "PRESERVE — Dolby Vision Profile 8.1 already suitable"
            )
        else:
            lines.append(
                "VERIFY — Dolby Vision Profile 8 detected; "
                "compatibility must be validated"
            )

    elif source_profile is None:
        lines.append("NOT READY — Dolby Vision not detected")
        ready = False

    else:
        lines.append(
            f"NOT READY — Dolby Vision Profile {source_profile} "
            "is not supported yet"
        )
        ready = False

    lines.extend([
        "",
        "AUDIO",
    ])

    atmos = None
    premium = None

    for audio in audio_streams:
        codec_name = (audio.get("codec") or "").lower()
        profile = audio.get("profile") or ""
        title = audio.get("title") or ""

        combined = f"{profile} {title}".lower()

        if codec_name == "truehd" and "atmos" in combined:
            atmos = audio
            break

        if codec_name in {"truehd", "eac3"} and premium is None:
            premium = audio

    chosen = atmos or premium

    if chosen:
        description = chosen["codec"].upper()

        if chosen.get("profile"):
            description += f' — {chosen["profile"]}'

        # DV Remux outputs two audio tracks:
        # 1. AC-3 5.1 first/default for broad TV/Jellyfin compatibility.
        # 2. The best premium Dolby track preserved behind it.
        existing_ac3_51 = any(
            (a.get("codec") or "").lower() == "ac3"
            and int(a.get("channels") or 0) >= 6
            for a in audio_streams
        )

        if existing_ac3_51:
            lines.append(
                "DEFAULT / COMPATIBILITY — Dolby Digital 5.1 "
                "• stream copy from source"
            )
        else:
            lines.append(
                "DEFAULT / COMPATIBILITY — Dolby Digital 5.1 "
                "• create AC-3 640 kb/s"
            )

        lines.append(
            f"PREMIUM / PRESERVE — {description}"
        )

        lines.append(
            "OUTPUT ORDER — DD 5.1 first/default "
            "• premium Dolby audio second"
        )
    elif audio_streams:
        lines.append(
            "PRESERVE — source audio; final MP4 compatibility "
            "will be validated before export"
        )
    else:
        lines.append("No audio stream detected")

    source_ext = Path(analysis["source"]).suffix.upper().lstrip(".")

    lines.extend([
        "",
        "CONTAINER",
        f"REMUX — {source_ext or 'SOURCE'} → MP4",
        "",
        "ORIGINAL",
        "UNTOUCHED — source file will never be modified or overwritten",
    ])

    heading = "READY FOR DV REMUX" if ready else "NOT READY"

    return {
        "ready": ready,
        "text": heading + "\n\n" + "\n".join(lines),
    }
