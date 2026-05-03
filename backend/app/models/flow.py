from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class NodeType(str, Enum):
    """Structural roles a node can occupy in a decision flow graph."""

    START = "start"
    END = "end"
    DECISION = "decision"
    BOUNDARY = "boundary"
    HUMAN_GATE = "human_gate"
    FALLBACK = "fallback"
    FORK = "fork"
    MERGE = "merge"
    ACTION = "action"


class NodePosition(BaseModel):
    """Visual coordinates of a node in a flow diagram editor."""

    x: float = Field(0.0, description="Horizontal position in canvas units")
    y: float = Field(0.0, description="Vertical position in canvas units")


class DecisionNode(BaseModel):
    """A single node in a decision flow graph.

    Each node has a structural type and optionally references a DecisionContract
    (for DECISION nodes), a boundary config (for BOUNDARY nodes), etc.
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Node identifier unique within the containing flow",
    )
    name: str = Field(..., min_length=1, max_length=256)
    node_type: NodeType = Field(..., description="Structural role of this node in the graph")
    condition: Optional[str] = Field(
        None,
        max_length=4096,
        description="Boolean expression evaluated against the signal context; DECISION and BOUNDARY nodes",
    )
    priority: int = Field(
        0,
        ge=0,
        description="Resolution priority; higher value wins when multiple nodes match; DECISION nodes only",
    )
    severity: Optional[str] = Field(
        None,
        description="Severity level for boundary nodes: critical/high/medium/low",
    )
    effect: Optional[str] = Field(
        None,
        description="Effect applied when a boundary node triggers: allow/block/override/escalate/redirect",
    )
    action: Optional[dict[str, Any]] = Field(
        None,
        description="Action payload for DECISION, BOUNDARY, and FALLBACK nodes",
    )
    contract_id: Optional[UUID] = Field(
        None,
        description="ID of the DecisionContract to evaluate; applicable to DECISION nodes only",
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Node-type-specific configuration (e.g. timeout for HUMAN_GATE, limit for BOUNDARY)",
    )
    position: Optional[NodePosition] = Field(
        None,
        description="Visual layout hint for diagram editors; not used during execution",
    )
    description: Optional[str] = Field(None, max_length=1024)
    is_active: bool = Field(True, description="Inactive nodes are skipped during traversal")


class FlowEdge(BaseModel):
    """A directed connection between two nodes in a decision flow.

    An optional condition_expression gates traversal; if absent the edge is always followed.
    """

    id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Edge identifier unique within the containing flow",
    )
    source_node_id: str = Field(..., description="ID of the node this edge originates from")
    target_node_id: str = Field(..., description="ID of the node this edge leads to")
    condition_expression: Optional[str] = Field(
        None,
        max_length=2048,
        description="Boolean expression that must evaluate to true for this edge to be traversed",
    )
    label: Optional[str] = Field(None, max_length=256, description="Human-readable edge label")
    priority: int = Field(
        0,
        ge=0,
        description="Traversal priority when multiple edges leave the same node; lower value = higher priority",
    )


class DecisionFlow(BaseModel):
    """An executable directed graph of decision nodes connected by edges.

    The flow defines the full topology of a decision process, from the entry
    node through all branching paths to terminal END nodes.
    """

    id: UUID = Field(default_factory=uuid4)
    flow_id: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="Human-readable identifier used in YAML files and registry lookups",
    )
    name: str = Field(..., min_length=1, max_length=256)
    description: Optional[str] = Field(None, max_length=2048)
    version: str = Field(
        ...,
        pattern=r"^\d+\.\d+\.\d+$",
        description="Semantic version (MAJOR.MINOR.PATCH)",
    )
    entry_node_id: str = Field(
        ...,
        description="ID of the node where execution begins; must reference a node in the nodes list",
    )
    nodes: list[DecisionNode] = Field(
        ...,
        min_length=1,
        description="All nodes that make up this flow",
    )
    edges: list[FlowEdge] = Field(
        default_factory=list,
        description="Directed edges defining valid traversal paths between nodes",
    )
    is_active: bool = Field(True, description="Inactive flows cannot be instantiated")
    metadata: dict[str, Any] = Field(default_factory=dict)
