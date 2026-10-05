from pathlib import Path


def default_output_path(source):
    source = Path(source)

    name = source.stem

    suffixes = (
        " - DV Remux",
        ".DVRemux",
        " DV Remux",
    )

    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[:-len(suffix)]

    return source.with_name(f"{name} - DV Remux.mp4")


def path_is_safe(source, output):
    source = Path(source).resolve()
    output = Path(output).resolve()

    if source == output:
        return False, "Output cannot overwrite the source."

    if output.exists():
        return False, "Output file already exists."

    if output.suffix.lower() != ".mp4":
        return False, "DV Remux output must be an MP4 file."

    if not output.parent.exists():
        return False, "Destination folder does not exist."

    return True, ""
