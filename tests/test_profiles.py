import json

from app.models import ProviderConfig, ProviderProfileUpsert
from app.profiles import ProviderProfileStore


def test_profile_keeps_api_key_out_of_files_and_backup_data(monkeypatch, tmp_path) -> None:
    secrets: dict[tuple[str, str], str] = {}
    monkeypatch.setattr("app.profiles.keyring.set_password", lambda service, user, value: secrets.__setitem__((service, user), value))
    monkeypatch.setattr("app.profiles.keyring.get_password", lambda service, user: secrets.get((service, user)))
    monkeypatch.setattr("app.profiles.keyring.delete_password", lambda service, user: secrets.pop((service, user), None))

    path = tmp_path / "provider_profiles.json"
    store = ProviderProfileStore(path)
    saved = store.save(ProviderProfileUpsert(
        name="主模型",
        provider=ProviderConfig(base_url="https://example.test/v1", model="example-model", api_key="super-secret"),
    ))

    on_disk = path.read_text(encoding="utf-8")
    assert "super-secret" not in on_disk
    assert "api_key" not in json.loads(on_disk)["profiles"][0]["provider"]
    assert saved["secret_backend"] == "os_keyring"
    assert store.resolve(saved["id"]).api_key == "super-secret"

    store.delete(saved["id"])
    assert secrets == {}
