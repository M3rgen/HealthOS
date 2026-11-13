"""Create a private .env from the repository template without replacing anything."""

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEYS = ("APP_UI_TOKEN", "POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD")
EXISTS = (
    ".env already exists; nothing was changed. Edit it by hand, "
    "or move it aside and run this again."
)
SUCCESS = (
    "wrote .env (mode 600): APP_UI_TOKEN, POSTGRES_PASSWORD, POSTGRES_APP_PASSWORD "
    "generated; values are not shown"
)


def render(template: Path) -> bytes:
    """Validate all required fields before generating or writing any secrets."""
    lines = template.read_bytes().decode("utf-8").splitlines(keepends=True)
    for key in KEYS:
        matches = [line for line in lines if line.startswith(key + "=")]
        if len(matches) != 1 or matches[0].rstrip("\r\n") != key + "=":
            raise ValueError("template must contain each required key exactly once and empty")
    values = {
        "APP_UI_TOKEN": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "POSTGRES_APP_PASSWORD": secrets.token_hex(32),
    }
    for index, line in enumerate(lines):
        key = line.partition("=")[0]
        if key in values:
            lines[index] = key + "=" + values[key] + line[len(line.rstrip("\r\n")) :]
    return "".join(lines).encode("utf-8")


def create(output: Path, content: bytes) -> None:
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
    except BaseException:
        output.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, default=ROOT / ".env.example")
    parser.add_argument("--output", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    if os.path.lexists(args.output):
        print(EXISTS, file=sys.stderr)
        return 2
    try:
        create(args.output, render(args.template))
    except FileExistsError:
        print(EXISTS, file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("could not create .env; check the template and output permissions", file=sys.stderr)
        return 1
    print(SUCCESS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
