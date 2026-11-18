"""Model profile persistence with OS-backed secret storage."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

import keyring
from keyring.errors import KeyringError

from .errors import FlowChatError
from .models import ProviderConfig, ProviderProfileUpsert, utc_now
from .storage import _atomic_json


SERVICE_NAME = "FeiYu-FlowChat"  # Stable credential namespace; display name is 肥鱼 FlowChat.


class ProviderProfileStore:
    """Store non-secret settings on disk and API keys in the OS vault.

    Some headless systems expose no usable keyring.  In that case the key is
    held only in ``_session_secrets`` and disappears when the process exits.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._session_secrets: dict[str, str] = {}

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": 1, "profiles": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("profiles"), list) else {"version": 1, "profiles": []}
        except (OSError, ValueError):
            return {"version": 1, "profiles": []}

    def _secret(self, profile_id: str) -> str:
        try:
            return keyring.get_password(SERVICE_NAME, profile_id) or self._session_secrets.get(profile_id, "")
        except KeyringError:
            return self._session_secrets.get(profile_id, "")

    def _set_secret(self, profile_id: str, secret: str) -> str:
        if not secret:
            return "unchanged"
        try:
            keyring.set_password(SERVICE_NAME, profile_id, secret)
            self._session_secrets.pop(profile_id, None)
            return "os_keyring"
        except KeyringError:
            self._session_secrets[profile_id] = secret
            return "session_only"

    def list(self) -> list[dict[str, Any]]:
        profiles = self._load()["profiles"]
        return [{**item, "key_stored": bool(self._secret(str(item.get("id", ""))))} for item in profiles]

    def save(self, body: ProviderProfileUpsert) -> dict[str, Any]:
        with self._lock:
            data = self._load()
            profile_id = body.id or str(uuid4())
            existing = next((item for item in data["profiles"] if item.get("id") == profile_id), None)
            provider = body.provider.model_dump(exclude_none=True)
            secret = provider.pop("api_key", "")
            record = {
                "id": profile_id,
                "name": body.name.strip(),
                "provider": provider,
                "created_at": existing.get("created_at") if existing else utc_now(),
                "updated_at": utc_now(),
            }
            if existing:
                data["profiles"][data["profiles"].index(existing)] = record
            else:
                data["profiles"].append(record)
            _atomic_json(self.path, data)
            backend = self._set_secret(profile_id, secret)
            return {**record, "key_stored": bool(self._secret(profile_id)), "secret_backend": backend}

    def resolve(self, profile_id: str) -> ProviderConfig:
        item = next((profile for profile in self._load()["profiles"] if profile.get("id") == profile_id), None)
        if not item:
            raise FlowChatError("找不到该模型配置档案", status_code=404, code="profile_not_found")
        config = dict(item.get("provider") or {})
        config["api_key"] = self._secret(profile_id)
        return ProviderConfig.model_validate(config)

    def delete(self, profile_id: str) -> None:
        with self._lock:
            data = self._load()
            before = len(data["profiles"])
            data["profiles"] = [item for item in data["profiles"] if item.get("id") != profile_id]
            if len(data["profiles"]) == before:
                raise FlowChatError("找不到该模型配置档案", status_code=404, code="profile_not_found")
            _atomic_json(self.path, data)
            self._session_secrets.pop(profile_id, None)
            try:
                keyring.delete_password(SERVICE_NAME, profile_id)
            except KeyringError:
                pass

    def clear_all_keys(self) -> int:
        count = 0
        for item in self._load()["profiles"]:
            profile_id = str(item.get("id", ""))
            if self._secret(profile_id):
                count += 1
            self._session_secrets.pop(profile_id, None)
            try:
                keyring.delete_password(SERVICE_NAME, profile_id)
            except KeyringError:
                pass
        return count
