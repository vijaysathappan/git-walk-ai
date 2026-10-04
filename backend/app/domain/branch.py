"""Branch ownership and lifecycle policies."""

from dataclasses import dataclass

from .repository import RepositoryPolicy, RepositoryRole


@dataclass(frozen=True)
class BranchPolicy:
    actor_user_id: str
    created_by: str
    branch_type: str
    repository_role: RepositoryRole

    @property
    def is_owner(self) -> bool:
        return self.repository_role is RepositoryRole.OWNER

    @property
    def is_branch_author(self) -> bool:
        return self.actor_user_id == self.created_by

    @property
    def can_edit(self) -> bool:
        policy = RepositoryPolicy(self.repository_role)
        return policy.can_edit and (self.is_owner or self.is_branch_author)

    @property
    def can_delete(self) -> bool:
        return self.branch_type != "MAIN" and (self.is_owner or self.is_branch_author)
