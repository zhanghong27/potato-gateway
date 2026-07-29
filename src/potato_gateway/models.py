from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


ServiceStatus = Literal["running"]
IntegrationStatus = Literal["unknown"]
AgentStatus = Literal["unknown"]


class HealthResponse(BaseModel):
    status: Literal["ok"]


class ServiceInfo(BaseModel):
    name: str
    status: ServiceStatus
    version: str


class PotatoHubInfo(BaseModel):
    status: IntegrationStatus
    message: str


class AgentInfo(BaseModel):
    id: str
    display_name: str
    status: AgentStatus


class StatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    service: ServiceInfo
    potato_hub: PotatoHubInfo
    agents: list[AgentInfo]
