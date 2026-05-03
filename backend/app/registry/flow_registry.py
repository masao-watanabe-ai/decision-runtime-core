from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import yaml

from backend.app.models.flow import DecisionFlow, DecisionNode, FlowEdge, NodePosition, NodeType
from backend.app.registry.flow_validator import FlowNotFoundError, FlowValidator


def _semver_key(version: str) -> tuple[int, int, int]:
    """Parse a MAJOR.MINOR.PATCH string into a sortable tuple."""
    parts = version.split(".")
    return (int(parts[0]), int(parts[1]), int(parts[2]))


class FlowRegistry:
    """Loads, validates, and serves DecisionFlow objects from YAML files.

    Flows are cached by (flow_id, version). Calling get() without a version
    returns the highest semantic version available for that flow_id.
    """

    def __init__(self, flow_dir: str) -> None:
        self._flow_dir = Path(flow_dir)
        self._flows: dict[tuple[str, str], DecisionFlow] = {}
        self._validator = FlowValidator()

    def load_all(self) -> None:
        """Discover and load all .yaml/.yml files in the configured flow directory.

        Raises FlowValidationError (from FlowValidator) on the first invalid file.
        """
        if not self._flow_dir.exists():
            return
        paths = sorted(self._flow_dir.glob("*.yaml")) + sorted(self._flow_dir.glob("*.yml"))
        for path in paths:
            self.load_file(path)

    def load_file(self, path: Path) -> DecisionFlow:
        """Parse, validate, and cache a single YAML flow file.

        Returns the resulting DecisionFlow.
        Raises FlowValidationError if the flow fails any validation rule.
        """
        with path.open("r", encoding="utf-8") as fh:
            raw: dict[str, Any] = yaml.safe_load(fh)

        flow = self._parse(raw)
        self._validator.validate(flow)
        self._flows[(flow.flow_id, flow.version)] = flow
        return flow

    def get(self, flow_id: str, version: str | None = None) -> DecisionFlow:
        """Return a loaded flow by ID and optional version.

        When version is omitted, returns the flow with the highest semantic version.
        Raises FlowNotFoundError if no matching flow is cached.
        """
        if version is not None:
            key = (flow_id, version)
            if key not in self._flows:
                raise FlowNotFoundError(
                    f"Flow '{flow_id}' version '{version}' not found in registry"
                )
            return self._flows[key]

        matching = [
            (ver, flow)
            for (fid, ver), flow in self._flows.items()
            if fid == flow_id
        ]
        if not matching:
            raise FlowNotFoundError(f"Flow '{flow_id}' not found in registry")

        _, latest_flow = max(matching, key=lambda pair: _semver_key(pair[0]))
        return latest_flow

    def list_flows(self) -> list[DecisionFlow]:
        """Return all loaded flows in the order they were loaded."""
        return list(self._flows.values())

    # ------------------------------------------------------------------ #
    # Parsing helpers                                                      #
    # ------------------------------------------------------------------ #

    def _parse(self, raw: dict[str, Any]) -> DecisionFlow:
        flow_id: str = raw["flow_id"]
        version: str = raw["version"]

        deterministic_id = uuid.uuid5(uuid.NAMESPACE_DNS, f"{flow_id}:{version}")

        nodes = [self._parse_node(n) for n in raw.get("nodes", [])]
        edges = [self._parse_edge(e) for e in raw.get("edges", [])]

        return DecisionFlow(
            id=deterministic_id,
            flow_id=flow_id,
            name=raw.get("name", flow_id),
            description=raw.get("description"),
            version=version,
            entry_node_id=raw["entry_node_id"],
            nodes=nodes,
            edges=edges,
            is_active=raw.get("is_active", True),
            metadata=raw.get("metadata", {}),
        )

    def _parse_node(self, raw: dict[str, Any]) -> DecisionNode:
        position_raw = raw.get("position")
        position = NodePosition(**position_raw) if position_raw else None

        return DecisionNode(
            id=raw["id"],
            name=raw.get("name", raw["id"]),
            node_type=NodeType(raw["node_type"]),
            condition=raw.get("condition"),
            priority=raw.get("priority", 0),
            severity=raw.get("severity"),
            effect=raw.get("effect"),
            action=raw.get("action"),
            contract_id=None,
            config=raw.get("config", {}),
            position=position,
            description=raw.get("description"),
            is_active=raw.get("is_active", True),
        )

    def _parse_edge(self, raw: dict[str, Any]) -> FlowEdge:
        return FlowEdge(
            id=raw["id"],
            source_node_id=raw["source_node_id"],
            target_node_id=raw["target_node_id"],
            condition_expression=raw.get("condition_expression"),
            label=raw.get("label"),
            priority=raw.get("priority", 0),
        )
