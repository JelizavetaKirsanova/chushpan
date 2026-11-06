"""Password-encrypted local memory snapshots; never persist the passphrase."""

import base64
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


def cipher(password, salt):
    if not isinstance(password, str) or not 12 <= len(password) <= 512:
        raise ValueError("Для копии нужна парольная фраза от 12 символов.")
    return Fernet(
        base64.urlsafe_b64encode(
            Scrypt(salt=salt, length=32, n=2**14, r=8, p=1).derive(password.encode())
        )
    )


def create(folder: Path, document: dict, password: str):
    salt = os.urandom(16)
    encrypted = cipher(password, salt).encrypt(json.dumps(document, ensure_ascii=False).encode())
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    name = "memory-" + datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f") + ".enc"
    path = folder / name
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(b"CHUSHMEM1" + salt + encrypted)
    return name


def read(folder, name, password):
    path = selected(folder, name)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Слишком большая копия.")
    data = path.read_bytes()
    if not data.startswith(b"CHUSHMEM1"):
        raise ValueError("Неизвестный формат копии.")
    try:
        return json.loads(cipher(password, data[9:25]).decrypt(data[25:]))
    except (InvalidToken, ValueError):
        raise ValueError("Не удалось открыть копию. Проверь парольную фразу.") from None


def selected(folder, name):
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".enc"):
        raise ValueError("Некорректное имя копии.")
    path = folder / name
    if not path.is_file() or path.is_symlink():
        raise ValueError("Копия не найдена.")
    return path


def listing(folder):
    if not folder.is_dir():
        return []
    return [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(folder.glob("memory-*.enc"), reverse=True)[:50]
        if p.is_file() and not p.is_symlink()
    ]
