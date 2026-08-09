from __future__ import annotations

from pydantic import ValidationError

from potato_gateway.adapters import HubClient, HubUnavailableError
from potato_gateway.models import CalibrationExecutionListResponse, CalibrationExecutionResponse, ExecuteCalibrationTurnRequest, TestPromptCandidateRequest
from potato_gateway.repositories import (
    CalibrationExecutionConflictError,
    CalibrationExecutionNotFoundError,
    CalibrationExecutionPersistenceError,
    CalibrationExecutionRecord,
    CalibrationExecutionRepository,
    CalibrationSessionNotFoundError,
    CalibrationSessionRepository,
    CalibrationTurnConflictError,
)


class CalibrationExecutionServiceUnavailableError(Exception):
    pass


class CalibrationExecutionService:
    def __init__(
        self,
        execution_repository: CalibrationExecutionRepository,
        session_repository: CalibrationSessionRepository,
        hub_client: HubClient,
    ) -> None:
        self.execution_repository = execution_repository
        self.session_repository = session_repository
        self.hub_client = hub_client

    def build_candidate_test_request(
        self,
        session_id: str,
        request: TestPromptCandidateRequest,
    ) -> ExecuteCalibrationTurnRequest:
        try:
            session = self.session_repository.get_session(session_id)
            if session is None:
                raise CalibrationSessionNotFoundError(session_id)
            baseline = next(
                (
                    item
                    for item in self.execution_repository.list_for_session(session_id)
                    if not item.prompt_version_id
                ),
                None,
            )
            role_task = {
                "creator": "自主选择一个能充分检验目标的具体题材，生成可播放的视频和必要交付件。",
                "researcher": "自主选择一个能充分检验目标的具体题材，完成带来源和待确认事实的研究包。",
                "critic": "使用 Session 中已有材料完成一次独立、可追溯的结构化评审。",
            }.get(session.agent_id, "完成一次能够充分检验目标的真实交付。")
            lines = [
                "这是候选 Prompt 的隔离复测。请真实执行完整任务，不要只解释做法。",
                f"校准目标：{session.goal}",
            ]
            if session.acceptance_criteria:
                lines.extend(
                    [
                        "验收标准：",
                        *[f"- {item}" for item in session.acceptance_criteria],
                    ]
                )
            if baseline is not None:
                lines.extend(
                    [
                        "为保证前后结果可比较，请重新执行下面的原始基准任务：",
                        baseline.instruction,
                    ]
                )
            else:
                lines.extend(["本 Session 没有现场基准任务。", role_task])
            if request.instruction.strip():
                lines.extend(
                    [
                        "用户本轮追加要求：",
                        request.instruction.strip(),
                    ]
                )
            return ExecuteCalibrationTurnRequest(
                client_turn_id=request.client_turn_id,
                instruction="\n".join(lines),
            )
        except CalibrationSessionNotFoundError:
            raise
        except CalibrationExecutionPersistenceError:
            raise CalibrationExecutionServiceUnavailableError from None

    def execute(
        self,
        session_id: str,
        request: ExecuteCalibrationTurnRequest,
        *,
        prompt_version_id: str = "",
        profile_override: str = "",
    ) -> tuple[CalibrationExecutionResponse, bool]:
        try:
            session = self.session_repository.get_session(session_id)
            if session is None:
                raise CalibrationSessionNotFoundError(session_id)
            record, created = self.execution_repository.create(
                session_id=session_id,
                client_turn_id=request.client_turn_id,
                agent_id=session.agent_id,
                instruction=request.instruction,
                prompt_version_id=prompt_version_id,
            )
            if not created:
                return self._response(record), False
            self.session_repository.create_turn(
                session_id=session_id,
                client_turn_id=f"{record.execution_id}.instruction",
                actor="commander",
                kind="instruction",
                content=request.instruction,
            )
            try:
                result = self.hub_client.request(
                    "POST",
                    "/api/calibration-jobs",
                    {
                        "client_request_id": record.execution_id,
                        "session_id": session_id,
                        "agent_id": session.agent_id,
                        "instruction": request.instruction,
                        "prompt_version_id": prompt_version_id,
                        "profile_override": profile_override,
                    },
                    sanitize=False,
                )
                hub_job_id = str(result["calibration_job"]["job_id"])
                record = self.execution_repository.attach_hub_job(
                    record.execution_id, hub_job_id
                )
            except (HubUnavailableError, KeyError, TypeError, ValueError):
                record = self.execution_repository.sync(
                    record.execution_id,
                    status="failed",
                    error="Potato Hub could not queue the calibration turn",
                )
            return self._response(record), True
        except (
            CalibrationSessionNotFoundError,
            CalibrationExecutionConflictError,
        ):
            raise
        except (
            CalibrationExecutionPersistenceError,
            CalibrationTurnConflictError,
            ValidationError,
        ):
            raise CalibrationExecutionServiceUnavailableError from None

    def get(self, session_id: str, execution_id: str) -> CalibrationExecutionResponse:
        try:
            record = self.execution_repository.get(execution_id)
            if record.session_id != session_id:
                raise CalibrationExecutionNotFoundError(execution_id)
            if record.hub_job_id and record.status not in {"completed", "failed"}:
                try:
                    result = self.hub_client.request(
                        "GET",
                        f"/api/calibration-jobs/{record.hub_job_id}",
                    )
                    job = result["calibration_job"]
                    status = str(job.get("status") or record.status)
                    if status in {"queued", "running", "completed", "failed"}:
                        record = self.execution_repository.sync(
                            execution_id,
                            status=status,
                            response=str(job.get("response") or ""),
                            asset_ids=job.get("asset_ids")
                            if isinstance(job.get("asset_ids"), list)
                            else [],
                            error=str(job.get("error") or ""),
                        )
                except (HubUnavailableError, KeyError, TypeError, ValueError):
                    pass
            if record.status == "completed" and record.response:
                self.session_repository.create_turn(
                    session_id=session_id,
                    client_turn_id=f"{record.execution_id}.response",
                    actor="agent",
                    kind="response",
                    content=record.response,
                )
            return self._response(record)
        except (CalibrationExecutionNotFoundError, CalibrationTurnConflictError):
            raise
        except (
            CalibrationExecutionPersistenceError,
            ValidationError,
        ):
            raise CalibrationExecutionServiceUnavailableError from None

    def list_for_session(self, session_id: str) -> CalibrationExecutionListResponse:
        try:
            return CalibrationExecutionListResponse(
                session_id=session_id,
                executions=[self.get(session_id, item.execution_id) for item in self.execution_repository.list_for_session(session_id)],
            )
        except CalibrationExecutionNotFoundError:
            raise
        except CalibrationExecutionPersistenceError:
            raise CalibrationExecutionServiceUnavailableError from None

    @staticmethod
    def _response(record: CalibrationExecutionRecord) -> CalibrationExecutionResponse:
        return CalibrationExecutionResponse(
            execution_id=record.execution_id,
            session_id=record.session_id,
            client_turn_id=record.client_turn_id,
            agent_id=record.agent_id,
            status=record.status,
            instruction=record.instruction,
            response=record.response or None,
            asset_ids=record.asset_ids,
            error=record.error or None,
            hub_job_id=record.hub_job_id or None,
            prompt_version_id=record.prompt_version_id or None,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
