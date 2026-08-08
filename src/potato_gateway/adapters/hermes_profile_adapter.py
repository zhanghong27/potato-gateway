from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from potato_gateway.models import HermesProfileInfo, PromptVersionInfo


LOGGER = logging.getLogger("potato_gateway.hermes_profile")
MAX_SOURCE_BYTES = 2 * 1024 * 1024
REGISTERED_AGENT_IDS = {"researcher", "creator", "critic", "engineer"}
REQUIRED_AGENT_IDS = {"researcher", "creator", "critic"}
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+:-]{0,199}$")


class AgentNotRegisteredError(Exception):
    pass


class HermesProfileSourceError(Exception):
    pass


def _validate_safe_relative_path(value: str, *, allow_dot: bool = False) -> str:
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("path must be a safe relative path")
    if not allow_dot and (value in {"", "."} or candidate.name in {"", "."}):
        raise ValueError("path must name a file")
    return value


class AgentRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str = Field(min_length=1)
    role: str = Field(min_length=1)
    profile_name: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    hermes_profile: str
    prompt_files: list[str] = Field(min_length=1)
    prompt_metadata_file: str | None = None

    @field_validator("hermes_profile")
    @classmethod
    def validate_profile_path(cls, value: str) -> str:
        return _validate_safe_relative_path(value, allow_dot=True)

    @field_validator("prompt_files")
    @classmethod
    def validate_prompt_files(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("prompt_files must be unique")
        return [_validate_safe_relative_path(value) for value in values]

    @field_validator("prompt_metadata_file")
    @classmethod
    def validate_metadata_file(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_safe_relative_path(value)


class AgentRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    agents: dict[str, AgentRegistration]

    @model_validator(mode="after")
    def validate_fixed_agent_ids(self) -> AgentRegistry:
        configured = set(self.agents)
        if not REQUIRED_AGENT_IDS <= configured or not configured <= REGISTERED_AGENT_IDS:
            raise ValueError("registry must contain the required supported agent IDs")
        return self


class PromptVersionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    version: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime
    source_files: list[str] = Field(min_length=1)


class HermesProfileAdapter:
    def __init__(self, hermes_home: Path, registry_path: Path) -> None:
        self.hermes_home = hermes_home.expanduser().resolve()
        self.registry_path = self._resolve_configured_path(registry_path)

    def get_registration(self, agent_id: str) -> AgentRegistration:
        registry = self._load_registry()
        registration = registry.agents.get(agent_id)
        if registration is None:
            raise AgentNotRegisteredError(agent_id)
        return registration

    def read_profile(
        self, registration: AgentRegistration
    ) -> tuple[HermesProfileInfo, PromptVersionInfo]:
        try:
            profile_root = self._resolve_within(
                self.hermes_home, registration.hermes_profile
            )
            if not profile_root.is_dir():
                raise HermesProfileSourceError("profile directory is unavailable")

            config_path = self._resolve_within(profile_root, "config.yaml")
            config = self._read_yaml_mapping(config_path)
            model = config.get("model", {})
            memory = config.get("memory", {})
            if not isinstance(model, dict) or not isinstance(memory, dict):
                raise HermesProfileSourceError("profile configuration is invalid")

            profile = HermesProfileInfo(
                provider="hermes",
                profile_name=registration.profile_name,
                load_status="loaded",
                model_provider=self._safe_identifier(model.get("provider")),
                model_name=self._safe_identifier(model.get("default")),
                skills=self._read_skill_names(profile_root),
                memory_enabled=memory.get("memory_enabled") is True,
            )
            prompt = self._read_prompt_version(profile_root, registration)
            return profile, prompt
        except HermesProfileSourceError:
            raise
        except (OSError, UnicodeError, ValidationError, yaml.YAMLError, ValueError):
            LOGGER.error("Hermes profile data is invalid or unavailable")
            raise HermesProfileSourceError("profile data is unavailable") from None

    def read_primary_prompt(self, agent_id: str) -> tuple[Path, str]:
        registration = self.get_registration(agent_id)
        if len(registration.prompt_files) != 1:
            raise HermesProfileSourceError("managed Prompt must have exactly one source file")
        try:
            profile_root = self._resolve_within(
                self.hermes_home, registration.hermes_profile
            )
            prompt_path = self._resolve_within(
                profile_root, registration.prompt_files[0]
            )
            content = self._read_limited_bytes(prompt_path).decode("utf-8")
            return prompt_path, content
        except (OSError, UnicodeError, ValueError):
            raise HermesProfileSourceError("Prompt source is unavailable") from None

    def _load_registry(self) -> AgentRegistry:
        try:
            payload = self._read_yaml_mapping(self.registry_path)
            return AgentRegistry.model_validate(payload)
        except (OSError, UnicodeError, ValidationError, yaml.YAMLError, ValueError):
            LOGGER.error("Agent registry is invalid or unavailable")
            raise HermesProfileSourceError("agent registry is unavailable") from None

    def _read_prompt_version(
        self, profile_root: Path, registration: AgentRegistration
    ) -> PromptVersionInfo:
        digest = hashlib.sha256()
        updated_timestamps: list[float] = []
        source_files = sorted(registration.prompt_files)

        for source_file in source_files:
            prompt_path = self._resolve_within(profile_root, source_file)
            content = self._read_limited_bytes(prompt_path).decode("utf-8")
            normalized = unicodedata.normalize(
                "NFC", content.replace("\r\n", "\n").replace("\r", "\n")
            )
            digest.update(source_file.encode("utf-8"))
            digest.update(b"\0")
            digest.update(normalized.encode("utf-8"))
            digest.update(b"\0")
            updated_timestamps.append(prompt_path.stat().st_mtime)

        full_hash = digest.hexdigest()
        version = f"sha256:{full_hash[:12]}"
        version_source: Literal["metadata", "content_hash"] = "content_hash"
        updated_at = datetime.fromtimestamp(max(updated_timestamps), tz=timezone.utc)

        if registration.prompt_metadata_file is not None:
            metadata_path = self._resolve_within(
                profile_root, registration.prompt_metadata_file
            )
            metadata = PromptVersionMetadata.model_validate(
                self._read_yaml_mapping(metadata_path)
            )
            if metadata.content_sha256 != full_hash:
                raise HermesProfileSourceError("prompt metadata hash does not match")
            if sorted(metadata.source_files) != source_files:
                raise HermesProfileSourceError("prompt metadata sources do not match")
            version = metadata.version
            version_source = "metadata"
            updated_at = metadata.updated_at

        return PromptVersionInfo(
            version=version,
            version_source=version_source,
            content_sha256=full_hash[:12],
            updated_at=updated_at,
            source_files=source_files,
        )

    def _read_skill_names(self, profile_root: Path) -> list[str]:
        skills_root = self._resolve_within(profile_root, "skills")
        if not skills_root.exists():
            return []
        if not skills_root.is_dir():
            raise HermesProfileSourceError("skills source is invalid")

        names: list[str] = []
        for entry in skills_root.iterdir():
            if entry.name.startswith(".") or not entry.is_dir():
                continue
            resolved = entry.resolve()
            if self._is_within(resolved, self.hermes_home):
                names.append(entry.name)
        return sorted(names)

    def _read_yaml_mapping(self, path: Path) -> dict:
        content = self._read_limited_bytes(path).decode("utf-8")
        payload = yaml.safe_load(content)
        if not isinstance(payload, dict):
            raise ValueError("expected a mapping")
        return payload

    def _read_limited_bytes(self, path: Path) -> bytes:
        resolved = path.resolve()
        if not self._is_within(resolved, self.hermes_home):
            raise HermesProfileSourceError("source path is outside the allowed root")
        if not resolved.is_file() or resolved.stat().st_size > MAX_SOURCE_BYTES:
            raise HermesProfileSourceError("source file is unavailable")
        return resolved.read_bytes()

    def _resolve_configured_path(self, path: Path) -> Path:
        expanded = path.expanduser()
        candidate = expanded if expanded.is_absolute() else Path.cwd() / expanded
        resolved = candidate.resolve()
        if not self._is_within(resolved, self.hermes_home):
            raise HermesProfileSourceError("configured path is outside the allowed root")
        return resolved

    def _resolve_within(self, root: Path, relative_path: str) -> Path:
        candidate = (root / relative_path).resolve()
        if not self._is_within(candidate, root) or not self._is_within(
            candidate, self.hermes_home
        ):
            raise HermesProfileSourceError("source path is outside the allowed root")
        return candidate

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
        except ValueError:
            return False
        return True

    @staticmethod
    def _safe_identifier(value: object) -> str | None:
        if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
            return None
        if "://" in value or value.startswith(("/", "~")):
            return None
        return value
