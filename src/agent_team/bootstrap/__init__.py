"""Repository bootstrap (build-order step 8): `init` discovers, `configure` writes.

`init` is read-only apart from `.agent-team/proposal.yaml`. `configure` is the
only command that writes into a target repository outside `.agent-team/`, and
only the files it showed in its diff.
"""

from agent_team.bootstrap.configure import (
    AGENT_MARKER,
    BEGIN_MARKER,
    CONFIG_NAME,
    END_MARKER,
    Plan,
    PlannedWrite,
    apply_plan,
    extend_with_block,
    plan_configure,
)
from agent_team.bootstrap.discovery import (
    PROPOSAL_NAME,
    BootstrapError,
    discover,
    dump_proposal,
    render_summary,
    write_proposal,
)

__all__ = [
    "AGENT_MARKER",
    "BEGIN_MARKER",
    "CONFIG_NAME",
    "END_MARKER",
    "PROPOSAL_NAME",
    "BootstrapError",
    "Plan",
    "PlannedWrite",
    "apply_plan",
    "discover",
    "dump_proposal",
    "extend_with_block",
    "plan_configure",
    "render_summary",
    "write_proposal",
]
