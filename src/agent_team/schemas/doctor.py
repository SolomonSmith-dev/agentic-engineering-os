"""The `agent-team doctor` report (build-order step 3).

Every field is a fact about the host, never a credential. Auth is a two-value
state and versions are regex-extracted, so no free-form probe output can carry
a secret into the report.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

AuthState = Literal["present", "absent"]


class DoctorModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HostReport(DoctorModel):
    os: str
    os_version: str
    arch: str
    python: str
    uv: str | None


class RuntimeReport(DoctorModel):
    name: str
    present: bool
    version: str | None


class AuthReport(DoctorModel):
    name: str
    state: AuthState


class EgressReport(DoctorModel):
    url: str
    reachable: bool


class ManagedSettingsReport(DoctorModel):
    path: str | None
    present: bool


class DoctorReport(DoctorModel):
    host: HostReport
    runtimes: list[RuntimeReport]
    auth: list[AuthReport]
    egress: EgressReport
    managed_settings: ManagedSettingsReport
