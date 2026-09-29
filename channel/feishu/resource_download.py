"""Bounded downloads for inbound Feishu files and voice messages."""

import os
import tempfile

import requests

from common.log import logger


MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_AUDIO_BYTES = 20 * 1024 * 1024


def download_resource(url, headers, params, path, max_bytes):
    """Atomically save a resource and return whether it completed within the limit."""
    temporary_path = None
    try:
        response = requests.get(
            url=url, headers=headers, params=params, stream=True, timeout=(5, 30)
        )
        try:
            if response.status_code != 200:
                logger.warning(f"[FeiShu] Resource download failed: status={response.status_code}")
                return False
            length = response.headers.get("Content-Length")
            try:
                declared_size = int(length) if length is not None else None
            except (TypeError, ValueError):
                declared_size = None
            if declared_size is not None and declared_size > max_bytes:
                raise ValueError("Feishu resource exceeds download limit")

            with tempfile.NamedTemporaryFile(
                mode="wb", dir=os.path.dirname(path), prefix=".feishu_", delete=False
            ) as handle:
                temporary_path = handle.name
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    size += len(chunk)
                    if size > max_bytes:
                        raise ValueError("Feishu resource exceeds download limit")
                    handle.write(chunk)
            os.replace(temporary_path, path)
            temporary_path = None
            return True
        finally:
            response.close()
    except Exception as exc:
        logger.warning(f"[FeiShu] Resource download failed: {exc}")
        return False
    finally:
        if temporary_path and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                logger.warning(f"[FeiShu] Could not remove incomplete download: {temporary_path}")
