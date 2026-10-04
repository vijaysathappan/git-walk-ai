"""Repository authorization rules kept independent from HTTP and SQLite."""

from dataclasses import dataclass
from enum import StrEnum


class RepositoryRole(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


@dataclass(frozen=True)
class RepositoryPolicy:
    role: RepositoryRole

    @classmethod
    def from_value(cls, value: str | None) -> "RepositoryPolicy | None":
        try:
            return cls(RepositoryRole(str(value).lower()))
        except (TypeError, ValueError):
            return None

    @property
    def can_view(self) -> bool:
        return True

    @property
    def can_edit(self) -> bool:
        return self.role in {RepositoryRole.OWNER, RepositoryRole.EDITOR}

    @property
    def can_manage_access(self) -> bool:
        return self.role is RepositoryRole.OWNER

    @property
    def can_review(self) -> bool:
        return self.role is RepositoryRole.OWNER

    @property
    def can_merge(self) -> bool:
        return self.role is RepositoryRole.OWNER

    @property
    def can_delete_repository(self) -> bool:
        return self.role is RepositoryRole.OWNER

    def capabilities(self) -> dict[str, bool]:
        return {
            "view": self.can_view,
            "edit": self.can_edit,
            "manage_access": self.can_manage_access,
            "review": self.can_review,
            "merge": self.can_merge,
            "delete_repository": self.can_delete_repository,
        }
