from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from backend.app.registry.contract_registry import ContractRegistry, ContractValidationError
from backend.app.registry.flow_registry import FlowRegistry
from backend.app.registry.flow_validator import FlowValidationError
from backend.app.models.contract import DecisionContract


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #

def _write_flow(path: Path, data: dict) -> Path:
    """Write a flow dict as YAML to the given directory and return the file path."""
    file_path = path / f"{data['flow_id']}_{data['version'].replace('.', '_')}.yaml"
    file_path.write_text(yaml.dump(data), encoding="utf-8")
    return file_path


def _minimal_valid_flow(
    flow_id: str = "test_flow",
    version: str = "1.0.0",
    extra_metadata: dict | None = None,
) -> dict:
    """Return a minimal flow dict that passes all validation rules."""
    return {
        "flow_id": flow_id,
        "name": "Test Flow",
        "version": version,
        "entry_node_id": "decision_1",
        "metadata": extra_metadata or {},
        "nodes": [
            {
                "id": "decision_1",
                "name": "Decision Node",
                "node_type": "decision",
                "config": {
                    "contract_type": "test_contract",
                    "contract_version": "1.0.0",
                },
            },
            {
                "id": "end_1",
                "name": "End",
                "node_type": "end",
            },
            {
                "id": "fallback_1",
                "name": "Fallback",
                "node_type": "fallback",
                "config": {
                    "contract_type": "fallback_contract",
                    "contract_version": "1.0.0",
                },
            },
        ],
        "edges": [
            {
                "id": "e_decision_to_end",
                "source_node_id": "decision_1",
                "target_node_id": "end_1",
                "priority": 0,
            },
            {
                "id": "e_decision_to_fallback",
                "source_node_id": "decision_1",
                "target_node_id": "fallback_1",
                "priority": 1,
            },
        ],
    }


# ------------------------------------------------------------------ #
# Registry loading                                                     #
# ------------------------------------------------------------------ #

def test_flow_registry_loads_valid_flows(tmp_path: Path) -> None:
    """FlowRegistry.load_all() successfully parses and caches a valid YAML flow."""
    _write_flow(tmp_path, _minimal_valid_flow())

    registry = FlowRegistry(str(tmp_path))
    registry.load_all()

    flows = registry.list_flows()
    assert len(flows) == 1
    assert flows[0].flow_id == "test_flow"
    assert flows[0].version == "1.0.0"


def test_flow_registry_get_latest_version(tmp_path: Path) -> None:
    """get() without a version argument returns the highest semantic version."""
    _write_flow(tmp_path, _minimal_valid_flow(version="1.0.0"))
    _write_flow(tmp_path, _minimal_valid_flow(version="2.3.1"))
    _write_flow(tmp_path, _minimal_valid_flow(version="1.9.0"))

    registry = FlowRegistry(str(tmp_path))
    registry.load_all()

    latest = registry.get("test_flow")
    assert latest.version == "2.3.1"


# ------------------------------------------------------------------ #
# Fallback node rules                                                  #
# ------------------------------------------------------------------ #

def test_flow_validation_requires_fallback(tmp_path: Path) -> None:
    """A flow with no fallback node must fail validation."""
    flow = _minimal_valid_flow()
    flow["nodes"] = [
        n for n in flow["nodes"] if n["node_type"] != "fallback"
    ]
    flow["edges"] = [
        e for e in flow["edges"] if e["target_node_id"] != "fallback_1"
    ]
    _write_flow(tmp_path, flow)

    registry = FlowRegistry(str(tmp_path))
    with pytest.raises(FlowValidationError, match="exactly one fallback"):
        registry.load_all()


# ------------------------------------------------------------------ #
# Node uniqueness                                                      #
# ------------------------------------------------------------------ #

def test_flow_validation_rejects_duplicate_node_ids(tmp_path: Path) -> None:
    """A flow that declares two nodes with the same ID must fail validation."""
    flow = _minimal_valid_flow()
    flow["nodes"].append(
        {
            "id": "decision_1",  # duplicate of existing node
            "name": "Duplicate Decision",
            "node_type": "decision",
            "config": {
                "contract_type": "another_contract",
                "contract_version": "1.0.0",
            },
        }
    )
    _write_flow(tmp_path, flow)

    registry = FlowRegistry(str(tmp_path))
    with pytest.raises(FlowValidationError, match="duplicate node ID"):
        registry.load_all()


# ------------------------------------------------------------------ #
# Edge reference integrity                                             #
# ------------------------------------------------------------------ #

def test_flow_validation_rejects_invalid_edge_reference(tmp_path: Path) -> None:
    """An edge that references a non-existent node ID must fail validation."""
    flow = _minimal_valid_flow()
    flow["edges"].append(
        {
            "id": "bad_edge",
            "source_node_id": "nonexistent_node",
            "target_node_id": "decision_1",
            "priority": 0,
        }
    )
    _write_flow(tmp_path, flow)

    registry = FlowRegistry(str(tmp_path))
    with pytest.raises(FlowValidationError, match="unknown source node"):
        registry.load_all()


# ------------------------------------------------------------------ #
# Cycle detection                                                      #
# ------------------------------------------------------------------ #

def _cyclic_flow(allow_cycles: bool = False) -> dict:
    """A flow with a directed cycle between node_a and node_b."""
    metadata: dict = {}
    if allow_cycles:
        metadata["allow_cycles"] = True
    return {
        "flow_id": "cycle_flow",
        "name": "Cycle Flow",
        "version": "1.0.0",
        "entry_node_id": "node_a",
        "metadata": metadata,
        "nodes": [
            {
                "id": "node_a",
                "name": "Node A",
                "node_type": "decision",
                "config": {"contract_type": "contract_a", "contract_version": "1.0.0"},
            },
            {
                "id": "node_b",
                "name": "Node B",
                "node_type": "decision",
                "config": {"contract_type": "contract_b", "contract_version": "1.0.0"},
            },
            {
                "id": "fallback_1",
                "name": "Fallback",
                "node_type": "fallback",
                "config": {"contract_type": "fb_contract", "contract_version": "1.0.0"},
            },
        ],
        "edges": [
            {
                "id": "a_to_b",
                "source_node_id": "node_a",
                "target_node_id": "node_b",
                "priority": 0,
            },
            {
                "id": "b_to_a",
                "source_node_id": "node_b",
                "target_node_id": "node_a",  # creates cycle
                "priority": 0,
            },
            {
                "id": "a_to_fallback",
                "source_node_id": "node_a",
                "target_node_id": "fallback_1",
                "priority": 1,
            },
        ],
    }


def test_flow_validation_rejects_cycle_by_default(tmp_path: Path) -> None:
    """A flow with a directed cycle and no allow_cycles flag must fail validation."""
    _write_flow(tmp_path, _cyclic_flow(allow_cycles=False))

    registry = FlowRegistry(str(tmp_path))
    with pytest.raises(FlowValidationError, match="cycle"):
        registry.load_all()


def test_flow_validation_allows_cycle_when_metadata_allow_cycles_true(tmp_path: Path) -> None:
    """A flow with allow_cycles=true in metadata must pass cycle validation."""
    _write_flow(tmp_path, _cyclic_flow(allow_cycles=True))

    registry = FlowRegistry(str(tmp_path))
    registry.load_all()  # must not raise

    flows = registry.list_flows()
    assert len(flows) == 1
    assert flows[0].metadata.get("allow_cycles") is True


# ------------------------------------------------------------------ #
# Contract registry                                                    #
# ------------------------------------------------------------------ #

def test_contract_validation_rejects_empty_type() -> None:
    """ContractRegistry.validate() must raise for a contract with an empty type field."""
    contract = DecisionContract.model_construct(
        id=uuid4(),
        type="",
        name="empty-type-contract",
        version="1.0.0",
    )
    registry = ContractRegistry()
    with pytest.raises(ContractValidationError, match="empty type"):
        registry.validate(contract)


def test_contract_validation_rejects_invalid_version() -> None:
    """ContractRegistry.validate() must raise for a contract with a non-semver version string."""
    contract = DecisionContract.model_construct(
        id=uuid4(),
        type="valid_type",
        name="bad-version-contract",
        version="not-a-version",
    )
    registry = ContractRegistry()
    with pytest.raises(ContractValidationError, match="not valid semver"):
        registry.validate(contract)
