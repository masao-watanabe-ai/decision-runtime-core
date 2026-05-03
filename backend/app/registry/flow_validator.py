from __future__ import annotations

from backend.app.models.flow import DecisionFlow, NodeType


class FlowValidationError(Exception):
    """Raised when a flow fails structural or semantic validation."""


class FlowNotFoundError(Exception):
    """Raised when a requested flow cannot be found in the registry."""


_CONTRACT_REQUIRED_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.DECISION, NodeType.BOUNDARY, NodeType.FALLBACK}
)


class FlowValidator:
    """Validates DecisionFlow objects against the specification rules.

    Args:
        validate_conditions: When ``True``, edge ``condition_expression`` strings
            are parsed and checked for safety using ``ConditionEvaluator``.
            Defaults to ``False`` because edge conditions may reference runtime
            variables (e.g. ``outcome``) that are not in the evaluator's
            allowed-variable set.  Enable this flag when all edge conditions
            are known to use only the standard context variables.
    """

    def __init__(self, validate_conditions: bool = False) -> None:
        self._validate_conditions = validate_conditions

    def validate(self, flow: DecisionFlow) -> None:
        """Run all validation rules against the flow; raise FlowValidationError on first failure."""
        self._check_has_nodes(flow)
        self._check_unique_node_ids(flow)
        self._check_unknown_node_types(flow)
        self._check_entry_node_exists(flow)
        self._check_priority_is_integer(flow)
        self._check_edge_references(flow)
        self._check_exactly_one_fallback(flow)
        self._check_no_orphan_nodes(flow)
        self._check_cycles(flow)
        self._check_contract_nodes(flow)
        if self._validate_conditions:
            self._check_edge_conditions(flow)
            self._check_node_conditions(flow)

    # ------------------------------------------------------------------ #
    # Individual rule checks                                               #
    # ------------------------------------------------------------------ #

    def _check_has_nodes(self, flow: DecisionFlow) -> None:
        if not flow.nodes:
            raise FlowValidationError(
                f"Flow '{flow.flow_id}' has no nodes; at least one node is required"
            )

    def _check_unique_node_ids(self, flow: DecisionFlow) -> None:
        seen: set[str] = set()
        for node in flow.nodes:
            if node.id in seen:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' contains duplicate node ID '{node.id}'"
                )
            seen.add(node.id)

    def _check_unknown_node_types(self, flow: DecisionFlow) -> None:
        valid_values = {t.value for t in NodeType}
        for node in flow.nodes:
            if node.node_type.value not in valid_values:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' node '{node.id}' has unknown type '{node.node_type.value}'"
                )

    def _check_entry_node_exists(self, flow: DecisionFlow) -> None:
        node_ids = {n.id for n in flow.nodes}
        if flow.entry_node_id not in node_ids:
            raise FlowValidationError(
                f"Flow '{flow.flow_id}' entry_node_id '{flow.entry_node_id}' does not reference a known node"
            )

    def _check_priority_is_integer(self, flow: DecisionFlow) -> None:
        for edge in flow.edges:
            if not isinstance(edge.priority, int):
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' edge '{edge.id}' priority must be an integer, "
                    f"got {type(edge.priority).__name__}"
                )

    def _check_edge_references(self, flow: DecisionFlow) -> None:
        node_ids = {n.id for n in flow.nodes}
        for edge in flow.edges:
            if edge.source_node_id not in node_ids:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' edge '{edge.id}' references unknown source node "
                    f"'{edge.source_node_id}'"
                )
            if edge.target_node_id not in node_ids:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' edge '{edge.id}' references unknown target node "
                    f"'{edge.target_node_id}'"
                )

    def _check_exactly_one_fallback(self, flow: DecisionFlow) -> None:
        fallbacks = [n for n in flow.nodes if n.node_type == NodeType.FALLBACK]
        if len(fallbacks) != 1:
            raise FlowValidationError(
                f"Flow '{flow.flow_id}' must have exactly one fallback node, found {len(fallbacks)}"
            )

    def _check_no_orphan_nodes(self, flow: DecisionFlow) -> None:
        """All non-fallback nodes must be reachable from entry_node_id via directed edges."""
        reachable = self._find_reachable(flow)
        fallback_ids = {n.id for n in flow.nodes if n.node_type == NodeType.FALLBACK}
        for node in flow.nodes:
            if node.id not in reachable and node.id not in fallback_ids:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' node '{node.id}' ({node.node_type.value}) is unreachable "
                    f"from entry node '{flow.entry_node_id}'; remove it or add a connecting edge"
                )

    def _check_cycles(self, flow: DecisionFlow) -> None:
        if flow.metadata.get("allow_cycles") is True:
            return
        if self._has_cycle(flow):
            raise FlowValidationError(
                f"Flow '{flow.flow_id}' contains a directed cycle; "
                "set metadata.allow_cycles=true to permit cycles"
            )

    def _check_contract_nodes(self, flow: DecisionFlow) -> None:
        """Decision, boundary, and fallback nodes must declare a valid inline contract."""
        from backend.app.registry.contract_registry import ContractRegistry, ContractValidationError

        registry = ContractRegistry()
        for node in flow.nodes:
            if node.node_type not in _CONTRACT_REQUIRED_NODE_TYPES:
                continue
            contract_type: str = node.config.get("contract_type", "")
            contract_version: str = node.config.get("contract_version", "")
            try:
                registry.validate_inline(contract_type, contract_version)
            except ContractValidationError as exc:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' node '{node.id}' ({node.node_type.value}) "
                    f"has invalid inline contract: {exc}"
                ) from exc

    def _check_node_conditions(self, flow: DecisionFlow) -> None:
        """Validate that all DECISION node condition strings are syntactically safe.

        Only runs when validate_conditions=True.  Skips nodes with no condition.
        """
        from backend.app.runtime.condition_evaluator import (
            ConditionEvaluationError,
            ConditionEvaluator,
        )

        evaluator = ConditionEvaluator()
        for node in flow.nodes:
            if node.node_type != NodeType.DECISION:
                continue
            if not node.condition:
                continue
            try:
                evaluator.validate_syntax(node.condition)
            except ConditionEvaluationError as exc:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' node '{node.id}' has an invalid condition: {exc}"
                ) from exc

    def _check_edge_conditions(self, flow: DecisionFlow) -> None:
        """Validate that all edge condition_expression strings are syntactically safe.

        Only runs when validate_conditions=True.  Skips edges with no condition.
        """
        from backend.app.runtime.condition_evaluator import (
            ConditionEvaluationError,
            ConditionEvaluator,
        )

        evaluator = ConditionEvaluator()
        for edge in flow.edges:
            if not edge.condition_expression:
                continue
            try:
                evaluator.validate_syntax(edge.condition_expression)
            except ConditionEvaluationError as exc:
                raise FlowValidationError(
                    f"Flow '{flow.flow_id}' edge '{edge.id}' has an invalid "
                    f"condition_expression: {exc}"
                ) from exc

    # ------------------------------------------------------------------ #
    # Graph algorithms                                                     #
    # ------------------------------------------------------------------ #

    def _find_reachable(self, flow: DecisionFlow) -> set[str]:
        """BFS from entry_node_id; return the set of reachable node IDs."""
        adj: dict[str, list[str]] = {n.id: [] for n in flow.nodes}
        for edge in flow.edges:
            if edge.source_node_id in adj:
                adj[edge.source_node_id].append(edge.target_node_id)

        reachable: set[str] = set()
        queue: list[str] = [flow.entry_node_id]
        while queue:
            node_id = queue.pop(0)
            if node_id in reachable:
                continue
            reachable.add(node_id)
            for neighbor in adj.get(node_id, []):
                if neighbor not in reachable:
                    queue.append(neighbor)
        return reachable

    def _has_cycle(self, flow: DecisionFlow) -> bool:
        """DFS-based cycle detection; returns True if the graph contains a directed cycle."""
        adj: dict[str, list[str]] = {n.id: [] for n in flow.nodes}
        for edge in flow.edges:
            if edge.source_node_id in adj:
                adj[edge.source_node_id].append(edge.target_node_id)

        visited: set[str] = set()
        in_stack: set[str] = set()

        def dfs(node_id: str) -> bool:
            visited.add(node_id)
            in_stack.add(node_id)
            for neighbor in adj.get(node_id, []):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in in_stack:
                    return True
            in_stack.discard(node_id)
            return False

        for node in flow.nodes:
            if node.id not in visited:
                if dfs(node.id):
                    return True
        return False
