from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

from potato_gateway.adapters import HermesProfileAdapter, HermesProfileSourceError
from potato_gateway.models import (
    CreatePromptCandidateRequest,
    GeneratePromptCandidateRequest,
    PromptVersionListResponse,
    PromptVersionSummary,
)
from potato_gateway.repositories import (
    CalibrationReviewPersistenceError,
    CalibrationReviewRepository,
    CalibrationPersistenceError,
    CalibrationSessionNotFoundError,
    CalibrationSessionRepository,
    PromptVersionConflictError,
    PromptVersionNotFoundError,
    PromptVersionPersistenceError,
    PromptVersionRecord,
    PromptVersionRepository,
)


class PromptVersionServiceUnavailableError(Exception):
    pass


MANAGED_ADDENDUM_START = "<!-- POTATO CALIBRATION ADDENDUM START -->"
MANAGED_ADDENDUM_END = "<!-- POTATO CALIBRATION ADDENDUM END -->"


class PromptVersionService:
    def __init__(
        self,
        repository: PromptVersionRepository,
        profile_adapter: HermesProfileAdapter,
        review_repository: CalibrationReviewRepository | None = None,
        session_repository: CalibrationSessionRepository | None = None,
    ) -> None:
        self.repository = repository
        self.profile_adapter = profile_adapter
        self.review_repository = review_repository
        self.session_repository = session_repository

    def create_candidate(
        self, agent_id: str, request: CreatePromptCandidateRequest
    ) -> tuple[PromptVersionSummary, bool]:
        try:
            active = self._ensure_snapshot(agent_id)
            record, created = self.repository.create_candidate(
                client_request_id=request.client_request_id,
                agent_id=agent_id,
                content=request.content,
                base_content_sha256=active.base_content_sha256,
                change_summary=request.change_summary,
                calibration_session_id=request.calibration_session_id or "",
            )
            return self._summary(record), created
        except (PromptVersionConflictError, PromptVersionNotFoundError):
            raise
        except (PromptVersionPersistenceError, HermesProfileSourceError, OSError):
            raise PromptVersionServiceUnavailableError from None

    def list_versions(self, agent_id: str, limit: int) -> PromptVersionListResponse:
        try:
            self._ensure_snapshot(agent_id)
            return PromptVersionListResponse(
                agent_id=agent_id,
                versions=[self._summary(item) for item in self.repository.list(agent_id, limit)],
            )
        except (PromptVersionPersistenceError, HermesProfileSourceError, OSError):
            raise PromptVersionServiceUnavailableError from None

    def generate_candidate(
        self,
        agent_id: str,
        session_id: str,
        request: GeneratePromptCandidateRequest,
    ) -> tuple[PromptVersionSummary, bool]:
        if self.session_repository is None or self.review_repository is None:
            raise PromptVersionServiceUnavailableError
        try:
            session = self.session_repository.get_session(session_id)
            if session is None or session.agent_id != agent_id:
                raise CalibrationSessionNotFoundError(session_id)
            active = self._ensure_snapshot(agent_id)
            reviews = [
                review
                for review in self.review_repository.list_for_session(session_id)
                if review.status == "completed"
            ]
            turns = self.session_repository.list_turns(session_id)
            addendum, summary = self._build_addendum(
                session.goal,
                session.acceptance_criteria,
                reviews,
                turns,
                request.additional_guidance,
            )
            content = self._with_managed_addendum(active.content, addendum)
            record, created = self.repository.create_candidate(
                client_request_id=request.client_request_id,
                agent_id=agent_id,
                content=content,
                base_content_sha256=active.content_sha256,
                change_summary=summary,
                calibration_session_id=session_id,
            )
            return self._summary(record), created
        except (CalibrationSessionNotFoundError, PromptVersionConflictError):
            raise
        except (
            PromptVersionPersistenceError,
            CalibrationReviewPersistenceError,
            CalibrationPersistenceError,
            HermesProfileSourceError,
            OSError,
        ):
            raise PromptVersionServiceUnavailableError from None

    def prepare_test(
        self, agent_id: str, session_id: str, prompt_version_id: str
    ) -> tuple[PromptVersionSummary, str]:
        if self.session_repository is None:
            raise PromptVersionServiceUnavailableError
        try:
            session = self.session_repository.get_session(session_id)
            candidate = self.repository.get(prompt_version_id)
            if (
                session is None
                or session.agent_id != agent_id
                or candidate.agent_id != agent_id
                or candidate.calibration_session_id != session_id
            ):
                raise PromptVersionNotFoundError(prompt_version_id)
            if candidate.status not in {"draft", "testing"}:
                raise PromptVersionConflictError("only a draft candidate can be tested")
            profile_name = self._materialize_testing_profile(agent_id, candidate)
            testing = self.repository.mark_testing(prompt_version_id)
            return self._summary(testing), profile_name
        except (
            PromptVersionNotFoundError,
            PromptVersionConflictError,
        ):
            raise
        except (
            PromptVersionPersistenceError,
            CalibrationPersistenceError,
            HermesProfileSourceError,
            OSError,
        ):
            raise PromptVersionServiceUnavailableError from None

    def promote(
        self, agent_id: str, prompt_version_id: str, confirm_content_sha256: str
    ) -> PromptVersionSummary:
        try:
            candidate = self.repository.get(prompt_version_id)
            if candidate.agent_id != agent_id:
                raise PromptVersionNotFoundError(prompt_version_id)
            if candidate.content_sha256 != confirm_content_sha256:
                raise PromptVersionConflictError("confirmation hash does not match")
            if candidate.calibration_session_id and self.review_repository is not None:
                blocker = self.review_repository.prompt_version_activation_blocker(
                    candidate.prompt_version_id,
                    candidate.calibration_session_id,
                    require_review=agent_id == "creator",
                )
                if blocker:
                    raise PromptVersionConflictError(blocker)
            prompt_path, original = self.profile_adapter.read_primary_prompt(agent_id)
            self._atomic_write(prompt_path, candidate.content)
            try:
                activated = self.repository.activate(prompt_version_id)
            except Exception:
                self._atomic_write(prompt_path, original)
                raise
            return self._summary(activated)
        except (PromptVersionConflictError, PromptVersionNotFoundError):
            raise
        except (PromptVersionPersistenceError, CalibrationReviewPersistenceError, HermesProfileSourceError, OSError):
            raise PromptVersionServiceUnavailableError from None

    def mark_testing(self, agent_id: str, prompt_version_id: str) -> PromptVersionSummary:
        try:
            candidate = self.repository.get(prompt_version_id)
            if candidate.agent_id != agent_id:
                raise PromptVersionNotFoundError(prompt_version_id)
            return self._summary(self.repository.mark_testing(prompt_version_id))
        except (PromptVersionConflictError, PromptVersionNotFoundError):
            raise
        except PromptVersionPersistenceError:
            raise PromptVersionServiceUnavailableError from None

    def _ensure_snapshot(self, agent_id: str) -> PromptVersionRecord:
        registration = self.profile_adapter.get_registration(agent_id)
        profile, prompt = self.profile_adapter.read_profile(registration)
        _path, content = self.profile_adapter.read_primary_prompt(agent_id)
        return self.repository.ensure_active_snapshot(
            agent_id=agent_id,
            content=content,
            profile_content_sha256=prompt.content_sha256,
        )

    def _materialize_testing_profile(
        self, agent_id: str, candidate: PromptVersionRecord
    ) -> str:
        prompt_path, _content = self.profile_adapter.read_primary_prompt(agent_id)
        source_root = prompt_path.parent.resolve()
        profile_name = f"potato-cal-{agent_id}-{candidate.content_sha256[:12]}"
        target_root = (self.profile_adapter.hermes_home / "profiles" / profile_name).resolve()
        target_root.relative_to(self.profile_adapter.hermes_home)
        target_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (
            "memories",
            "sessions",
            "skills",
            "skins",
            "logs",
            "plans",
            "workspace",
            "cron",
            "home",
        ):
            (target_root / directory).mkdir(exist_ok=True)
        for filename in ("config.yaml", ".env", "auth.json"):
            source = source_root / filename
            if source.is_file():
                destination = target_root / filename
                shutil.copy2(source, destination)
                if filename in {".env", "auth.json"}:
                    destination.chmod(0o600)
        for filename in ("MEMORY.md", "USER.md"):
            source = source_root / "memories" / filename
            if source.is_file():
                shutil.copy2(source, target_root / "memories" / filename)
        source_skills = source_root / "skills"
        target_skills = target_root / "skills"
        if source_skills.is_dir() and not any(target_skills.iterdir()):
            target_skills.rmdir()
            target_skills.symlink_to(source_skills, target_is_directory=True)
        self._atomic_write(target_root / "SOUL.md", candidate.content)
        return profile_name

    @staticmethod
    def _build_addendum(
        goal: str,
        criteria: list[str],
        reviews: list,
        turns: list,
        additional_guidance: str,
    ) -> tuple[str, str]:
        user_feedback = [
            turn.content.strip()
            for turn in turns
            if turn.actor == "user" and turn.kind in {"critique", "note"} and turn.content.strip()
        ][-8:]
        critic_rules: list[str] = []
        hard_error_rules: list[str] = []
        for review in reviews[-5:]:
            report = review.report if isinstance(review.report, dict) else {}
            for item in report.get("revision_requirements", []):
                if isinstance(item, str) and item.strip():
                    critic_rules.append(item.strip())
            for item in report.get("style_findings", []):
                if isinstance(item, dict):
                    value = str(item.get("recommendation") or item.get("observation") or "").strip()
                    if value:
                        critic_rules.append(value)
            for item in report.get("hard_errors", []):
                if isinstance(item, dict):
                    value = str(item.get("fix") or item.get("problem") or "").strip()
                    if value:
                        hard_error_rules.append(value)

        def unique(values: list[str], limit: int) -> list[str]:
            result: list[str] = []
            seen: set[str] = set()
            for value in values:
                clean = value.replace(MANAGED_ADDENDUM_START, "").replace(
                    MANAGED_ADDENDUM_END, ""
                )
                clean = re.sub(r"\s+", " ", clean).strip(" -")[:1200]
                key = clean.casefold()
                if clean and key not in seen:
                    result.append(clean)
                    seen.add(key)
                if len(result) >= limit:
                    break
            return result

        required = unique([*criteria, *hard_error_rules, *critic_rules], 18)
        feedback = unique(user_feedback, 8)
        guidance = unique([additional_guidance], 1)
        clean_goal = re.sub(r"\s+", " ", goal).strip()
        lines = [
            MANAGED_ADDENDUM_START,
            "# Current calibration addendum",
            "",
            "Treat these rules as mandatory additions to the stable role definition above.",
            f"Calibration objective: {clean_goal}",
        ]
        if required:
            lines.extend(["", "## Required behavior", *[f"- {item}" for item in required]])
        if feedback:
            lines.extend(["", "## User quality direction", *[f"- {item}" for item in feedback]])
        if guidance:
            lines.extend(["", "## Candidate focus", *[f"- {item}" for item in guidance]])
        lines.extend(
            [
                "",
                "## Before delivery",
                "- Verify the output against every rule in this addendum and report any unmet requirement honestly.",
                "- Do not claim that an asset, check, or tool result exists unless it was actually produced or inspected.",
                MANAGED_ADDENDUM_END,
            ]
        )
        summary = (
            f"校准闭环：{goal[:120]}；纳入 {len(required)} 条评审规则、"
            f"{len(feedback)} 条用户反馈"
        )
        return "\n".join(lines), summary

    @staticmethod
    def _with_managed_addendum(content: str, addendum: str) -> str:
        pattern = re.compile(
            rf"\n*{re.escape(MANAGED_ADDENDUM_START)}.*?{re.escape(MANAGED_ADDENDUM_END)}\n*",
            re.DOTALL,
        )
        stable = pattern.sub("\n", content).rstrip()
        return f"{stable}\n\n{addendum}\n"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path = path.resolve()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, path.stat().st_mode if path.exists() else 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _summary(record: PromptVersionRecord) -> PromptVersionSummary:
        match = re.search(
            rf"{re.escape(MANAGED_ADDENDUM_START)}.*?{re.escape(MANAGED_ADDENDUM_END)}",
            record.content,
            re.DOTALL,
        )
        return PromptVersionSummary(
            prompt_version_id=record.prompt_version_id,
            agent_id=record.agent_id,
            status=record.status,
            content_sha256=record.content_sha256,
            base_content_sha256=record.base_content_sha256,
            change_summary=record.change_summary,
            managed_addendum=match.group(0) if match else None,
            calibration_session_id=record.calibration_session_id or None,
            created_at=record.created_at,
            updated_at=record.updated_at,
            activated_at=record.activated_at or None,
        )
