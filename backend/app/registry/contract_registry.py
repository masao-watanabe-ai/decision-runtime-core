from __future__ import annotations

import re

from backend.app.models.contract import DecisionContract


class ContractValidationError(Exception):
    """Raised when a DecisionContract fails validation."""


_SEMVER_RE: re.Pattern[str] = re.compile(r"^\d+\.\d+\.\d+$")


class ContractRegistry:
    """Validates DecisionContract objects and provides an extension point for supported types.

    Currently enforces non-empty type and valid semantic version format.
    Future versions can extend this to maintain a registry of known contract types
    and enforce additional business rules per type.
    """

    def validate(self, contract: DecisionContract) -> None:
        """Validate a full DecisionContract object.

        Raises ContractValidationError if type is empty or version is not valid semver.
        """
        if not contract.type or not contract.type.strip():
            raise ContractValidationError(
                f"Contract '{contract.name}' has an empty type; a non-empty type is required"
            )
        if not _SEMVER_RE.match(contract.version):
            raise ContractValidationError(
                f"Contract '{contract.name}' version '{contract.version}' is not valid semver; "
                "expected MAJOR.MINOR.PATCH (e.g. 1.0.0)"
            )

    def validate_inline(self, contract_type: str, contract_version: str) -> None:
        """Validate an inline contract specification embedded in a node's config dict.

        Used by FlowValidator to check decision/boundary/fallback node contracts
        without requiring a fully constructed DecisionContract object.

        Raises ContractValidationError if type is empty or version is not valid semver.
        """
        if not contract_type or not contract_type.strip():
            raise ContractValidationError(
                "Inline contract config is missing a non-empty 'contract_type'"
            )
        if not _SEMVER_RE.match(contract_version):
            raise ContractValidationError(
                f"Inline contract 'contract_version' value '{contract_version}' is not valid semver; "
                "expected MAJOR.MINOR.PATCH (e.g. 1.0.0)"
            )
