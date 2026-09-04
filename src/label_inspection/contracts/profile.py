"""Explicit profile identity and business-approval contract."""

from __future__ import annotations

from dataclasses import dataclass

UNAPPROVED = "UNAPPROVED"
APPROVED_FOR_AUTOMATED_PASS = "APPROVED_FOR_AUTOMATED_PASS"
PROFILE_BINDING_VERSION = "profile-binding.v2"
PROFILE_APPROVAL_STATUSES = frozenset(
    {UNAPPROVED, APPROVED_FOR_AUTOMATED_PASS}
)
DOCUMENT_RECOGNITION_STATUSES = frozenset(
    {"KNOWN", "UNKNOWN", "AMBIGUOUS"}
)


@dataclass(frozen=True)
class ProfileBinding:
    """Identity and approval state shared by one processing execution.

    Approval is intentionally explicit.  A named profile with no recorded
    blocker is still unapproved until its binding carries the approved status.
    """

    name: str | None = None
    version: str | None = None
    approval_status: str = UNAPPROVED

    def __post_init__(self) -> None:
        name = self._normalize_name(self.name)
        version = self._normalize_version(self.version)
        if (name is None) != (version is None):
            raise ValueError("profile name and version must be provided together")
        if self.approval_status not in PROFILE_APPROVAL_STATUSES:
            raise ValueError(
                "profile approval status must be UNAPPROVED or "
                "APPROVED_FOR_AUTOMATED_PASS"
            )
        if name is None and self.approval_status == APPROVED_FOR_AUTOMATED_PASS:
            raise ValueError("profile-free binding cannot be approved")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "version", version)

    @staticmethod
    def _normalize_name(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("profile name must be a non-empty string")
        return value.strip().lower().replace("-", "_")

    @staticmethod
    def _normalize_version(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("profile version must be a non-empty string")
        # A version is an opaque identity owned by the profile author. Only
        # surrounding whitespace is insignificant; punctuation and case must
        # survive round trips and compatibility checks unchanged.
        return value.strip()

    @classmethod
    def unprofiled(cls) -> ProfileBinding:
        return cls()

    @classmethod
    def from_legacy(
        cls,
        *,
        name: str | None,
        version: str | None,
        approved: bool = False,
    ) -> ProfileBinding:
        if not isinstance(approved, bool):
            raise TypeError("profile approval must be boolean")
        return cls(
            name=name,
            version=version,
            approval_status=(
                APPROVED_FOR_AUTOMATED_PASS if approved else UNAPPROVED
            ),
        )

    @property
    def is_profile_free(self) -> bool:
        return self.name is None

    @property
    def allows_automated_pass(self) -> bool:
        return self.approval_status == APPROVED_FOR_AUTOMATED_PASS

    def to_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "version": self.version,
            "approval_status": self.approval_status,
        }


@dataclass(frozen=True)
class DocumentRecognitionResult:
    """A recognized-document result supplied by a future trusted boundary.

    This is deliberately only a contract.  It does not classify documents or
    select a model.  The current runtime supplies ``None`` because it has no
    production document recognizer; therefore it cannot enable semantic PASS.
    """

    status: str
    profile_binding: ProfileBinding
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in DOCUMENT_RECOGNITION_STATUSES:
            raise ValueError(
                "document recognition status must be KNOWN, UNKNOWN, or AMBIGUOUS"
            )
        if not isinstance(self.profile_binding, ProfileBinding):
            raise TypeError("document recognition must carry a ProfileBinding")
        if self.reason is not None:
            if not isinstance(self.reason, str) or not self.reason.strip():
                raise ValueError("document recognition reason must be non-empty")
            object.__setattr__(self, "reason", self.reason.strip())

    @classmethod
    def known(cls, profile_binding: ProfileBinding) -> DocumentRecognitionResult:
        return cls(status="KNOWN", profile_binding=profile_binding)

    @property
    def is_known(self) -> bool:
        return self.status == "KNOWN"

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "profile_binding": self.profile_binding.to_dict(),
            "reason": self.reason,
        }
