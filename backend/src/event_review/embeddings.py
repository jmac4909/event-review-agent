"""A pinned local embedding model. Downloads happen only in the setup command."""

import argparse
import hashlib
from pathlib import Path
from threading import Lock
from urllib.request import urlopen

from .settings import ROOT


MODEL = "minishlab/potion-base-8M"
REVISION = "bf8b056651a2c21b8d2565580b8569da283cab23"
MODEL_DIRECTORY = ROOT / ".runtime" / "embedding"
FILES = {
    "config.json": "2a6ac0e9aaa356a68a5688070db78fc3a464fefe85d2f06a1905ce3718687553",
    "model.safetensors": "f65d0f325faadc1e121c319e2faa41170d3fa07d8c89abd48ca5358d9a223de2",
    "tokenizer.json": "e67e803f624fb4d67dea1c730d06e1067e1b14d830e2c2202569e3ef0f70bb50",
    "README.md": "de8ec91bf63c5f4c0e20751c227b2d049953e1cab5f8d5d44211c59a44795bdd",
}


def verify_model(directory: Path) -> None:
    for name, digest in FILES.items():
        path = directory / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Local embedding files are missing or changed. Run python -m event_review.embeddings --download.")


def download_model(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name, digest in FILES.items():
        target = directory / name
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest() == digest:
            continue
        with urlopen(f"https://huggingface.co/{MODEL}/resolve/{REVISION}/{name}", timeout=60) as response:
            content = response.read(40_000_001)
        if len(content) > 40_000_000 or hashlib.sha256(content).hexdigest() != digest:
            raise ValueError(f"Embedding download did not match its pinned checksum: {name}")
        temporary = directory / f"{name}.part"
        temporary.write_bytes(content)
        temporary.replace(target)
    verify_model(directory)


class LocalEncoder:
    model_name = MODEL
    revision = REVISION

    def __init__(self, directory: Path = MODEL_DIRECTORY):
        # Check before calling the library, so a missing path cannot trigger its
        # automatic Hub download. No runtime network or credentials are needed.
        verify_model(directory)
        from model2vec import StaticModel

        self.model = StaticModel.from_pretrained(directory, normalize=True, max_length=512)
        self.lock = Lock()

    def encode(self, texts: list[str]) -> list[list[float]]:
        if len(texts) > 65:
            raise ValueError("Embedding batches are limited to one query and 64 candidates.")
        # All workers share one model. Small serialized batches bound memory and
        # keep the tokenizer's mutable configuration out of concurrent calls.
        with self.lock:
            return self.model.encode(texts, normalize=True, batch_size=8,
                                     use_multiprocessing=False).tolist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", required=True)
    parser.add_argument("--directory", type=Path, default=MODEL_DIRECTORY)
    args = parser.parse_args()
    download_model(args.directory)
    print(f"Verified local model: {MODEL} at revision {REVISION}")


if __name__ == "__main__":
    main()
