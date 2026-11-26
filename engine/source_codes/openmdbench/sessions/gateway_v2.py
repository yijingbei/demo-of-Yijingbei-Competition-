"""One transport-independent schema-v2 agent authority boundary."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Any

from openmdbench.schemas.core_v2 import ObservationV2
from openmdbench.schemas.interface_v2 import ActionBatchV2
from openmdbench.sessions.lifecycle_v2 import (
    ActionApplyReceiptV2,
    ActionReceiptV2,
    SessionCheckpointV2,
    SessionLifecycleV2,
    SessionStateV2,
)
from openmdbench.visualization.v2 import FrameBuilderV2

SessionFactoryV2 = Callable[[str, int], SessionLifecycleV2]
SessionRestoreFactoryV2 = Callable[[SessionCheckpointV2], SessionLifecycleV2]
VisualizationFactoryV2 = Callable[[SessionLifecycleV2, str], Any]


class AuthorizationFailureV2(PermissionError):
    """Stable public authorization failure without object-existence detail."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class GatewayContractFailureV2(ValueError):
    """Stable validation failure for the authenticated public gateway."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipalV2:
    principal_id: str
    controller_slot_ids: tuple[str, ...] = ()
    faction_ids: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.principal_id, str)
            or not self.principal_id
            or any(
                not isinstance(value, str) or not value
                for value in (
                    *self.controller_slot_ids,
                    *self.faction_ids,
                    *self.roles,
                )
            )
        ):
            raise ValueError("invalid_credentials")

    def permits_controller(self, slot_id: str) -> bool:
        return "referee" in self.roles or slot_id in self.controller_slot_ids

    def permits_faction(self, faction_id: str) -> bool:
        return "referee" in self.roles or faction_id in self.faction_ids


class AgentGatewayV2:
    """Own V2 sessions without exposing their World or queue writer."""

    def __init__(
        self,
        *,
        session_factory: SessionFactoryV2 | None = None,
        restore_factory: SessionRestoreFactoryV2 | None = None,
        visualization_factory: VisualizationFactoryV2 | None = None,
    ) -> None:
        self._sessions: dict[str, SessionLifecycleV2] = {}
        self._session_factory = session_factory
        self._restore_factory = restore_factory
        self._visualization_factory = visualization_factory
        self._principals: dict[str, dict[str, AuthenticatedPrincipalV2]] = {}
        # Only digests of bearer credentials live in the process.  The mapping
        # is intentionally session-local and never serialised into checkpoints.
        self._credential_digests: dict[str, dict[str, str]] = {}
        # A duplicate idempotency key is scoped to the authenticated caller,
        # preventing an existing pipeline receipt from crossing principals.
        self._authenticated_idempotency: dict[str, dict[str, str]] = {}
        self._lock = RLock()

    @property
    def session_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._sessions))

    def attach(self, session: SessionLifecycleV2) -> str:
        if (
            not isinstance(session, SessionLifecycleV2)
            or session.state is not SessionStateV2.CREATED
        ):
            raise ValueError("gateway accepts one uninitialized V2 session authority")
        with self._lock:
            if session.session_id in self._sessions:
                raise ValueError("gateway session identity already exists")
            self._sessions[session.session_id] = session
            self._principals[session.session_id] = {}
            self._credential_digests[session.session_id] = {}
            self._authenticated_idempotency[session.session_id] = {}
            return session.session_id

    def grant_principal(self, session_id: str, principal: AuthenticatedPrincipalV2) -> None:
        if not principal.principal_id:
            raise ValueError("invalid_credentials")
        self._get(session_id)
        with self._lock:
            self._principals.setdefault(session_id, {})[principal.principal_id] = principal

    def register_principal(
        self,
        session_id: str,
        principal: AuthenticatedPrincipalV2,
        *,
        credential: str | None = None,
    ) -> str:
        """Register a deployment-authenticated principal and issue one bearer secret.

        This method is a server-side provisioning boundary.  It is deliberately
        not exposed by the REST application, so an unauthenticated client
        cannot mint a role or controller grant for itself.
        """

        self.grant_principal(session_id, principal)
        value = secrets.token_urlsafe(32) if credential is None else credential
        if not isinstance(value, str) or len(value) < 16:
            raise ValueError("invalid_credentials")
        digest = hashlib.sha256(value.encode()).hexdigest()
        with self._lock:
            credentials = self._credential_digests.setdefault(session_id, {})
            existing = credentials.get(digest)
            if existing is not None and existing != principal.principal_id:
                raise ValueError("invalid_credentials")
            credentials[digest] = principal.principal_id
        return value

    def _principal(self, session_id: str, *, principal_id: str) -> AuthenticatedPrincipalV2:
        self._get(session_id)
        with self._lock:
            principal = self._principals.get(session_id, {}).get(principal_id)
        if principal is None:
            raise AuthorizationFailureV2("authentication_required")
        return principal

    def authenticate(self, session_id: str, *, credential: str | None) -> AuthenticatedPrincipalV2:
        self._get(session_id)
        if credential is None or not isinstance(credential, str) or not credential:
            raise AuthorizationFailureV2("authentication_required")
        candidate = hashlib.sha256(credential.encode()).hexdigest()
        with self._lock:
            values = tuple(self._credential_digests.get(session_id, {}).items())
            principal_map = self._principals.get(session_id, {})
        principal_id = next(
            (
                mapped_principal_id
                for digest, mapped_principal_id in values
                if hmac.compare_digest(digest, candidate)
            ),
            None,
        )
        principal = None if principal_id is None else principal_map.get(principal_id)
        if principal is None:
            raise AuthorizationFailureV2("invalid_credentials")
        return principal

    def controller_observation(
        self, session_id: str, *, principal_id: str, controller_slot_id: str
    ) -> ObservationV2:
        principal = self._principal(session_id, principal_id=principal_id)
        if not principal.permits_controller(controller_slot_id):
            raise AuthorizationFailureV2("observation_scope_forbidden")
        return self._get(session_id).world_view.controller_observation(
            controller_slot_id=controller_slot_id
        )

    def authenticated_controller_observation(
        self, session_id: str, *, credential: str | None, controller_slot_id: str
    ) -> ObservationV2:
        principal = self.authenticate(session_id, credential=credential)
        if not principal.permits_controller(controller_slot_id):
            raise AuthorizationFailureV2("observation_scope_forbidden")
        return self._get(session_id).world_view.controller_observation(
            controller_slot_id=controller_slot_id
        )

    def referee_observation(
        self, session_id: str, *, principal_id: str, faction_id: str
    ) -> ObservationV2:
        principal = self._principal(session_id, principal_id=principal_id)
        if "referee" not in principal.roles:
            raise AuthorizationFailureV2("referee_role_required")
        return self._get(session_id).world_view.observation(observer_faction_id=faction_id)

    def create(self, *, session_id: str, seed: int) -> str:
        if self._session_factory is None:
            raise RuntimeError("gateway session factory is not configured")
        return self.attach(self._session_factory(session_id, seed))

    def restore(self, checkpoint: SessionCheckpointV2) -> str:
        if self._restore_factory is None:
            raise RuntimeError("gateway restore factory is not configured")
        session = self._restore_factory(checkpoint)
        if not isinstance(session, SessionLifecycleV2) or session.state in {
            SessionStateV2.CREATED,
            SessionStateV2.CLOSED,
        }:
            raise ValueError("gateway restore factory returned an invalid restored session")
        restored_checkpoint = session.checkpoint()
        if (
            restored_checkpoint.session_id != checkpoint.session_id
            or restored_checkpoint.checkpoint_hash != checkpoint.checkpoint_hash
        ):
            if session.state in {
                SessionStateV2.RUNNING,
                SessionStateV2.LOADED,
                SessionStateV2.PAUSED,
            }:
                session.stop()
            session.close()
            raise ValueError("gateway restore factory returned a different checkpoint authority")
        with self._lock:
            if session.session_id in self._sessions:
                raise ValueError("gateway session identity already exists")
            self._sessions[session.session_id] = session
            self._principals[session.session_id] = {}
            self._credential_digests[session.session_id] = {}
            self._authenticated_idempotency[session.session_id] = {}
        return session.session_id

    def _get(self, session_id: str) -> SessionLifecycleV2:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as error:
                raise KeyError(f"unknown session: {session_id}") from error

    def control(self, session_id: str, operation: str) -> SessionStateV2:
        session = self._get(session_id)
        if operation == "load":
            session.load()
        elif operation == "start":
            session.start()
        elif operation == "pause":
            session.pause()
        elif operation == "resume":
            session.resume()
        elif operation == "stop":
            session.stop()
        else:
            raise ValueError("unsupported session control operation")
        return session.state

    def observation(self, session_id: str, *, faction_id: str) -> ObservationV2:
        return self._get(session_id).world_view.observation(observer_faction_id=faction_id)

    def submit(
        self,
        session_id: str,
        *,
        batch: ActionBatchV2,
        authority_token: str,
        operation_id: str,
        expected_tick: int,
    ) -> ActionReceiptV2:
        if batch.session_id != session_id:
            raise ValueError("action batch session differs from gateway route")
        return self._get(session_id).submit_actions(
            batch=batch,
            authority_token=authority_token,
            operation_id=operation_id,
            expected_tick=expected_tick,
        )

    def submit_as_principal(
        self,
        session_id: str,
        *,
        principal_id: str,
        controller_slot_id: str,
        batch: ActionBatchV2,
        operation_id: str,
        expected_tick: int,
    ) -> ActionReceiptV2:
        """Submit through a principal/claim boundary without exposing World grants."""

        principal = self._principal(session_id, principal_id=principal_id)
        if not principal.permits_controller(controller_slot_id):
            raise AuthorizationFailureV2("controller_not_authorized")
        session = self._get(session_id)
        try:
            claim = session.world_view.controller_claim(controller_slot_id)
        except KeyError as error:
            raise AuthorizationFailureV2("controller_not_authorized") from error
        children = (*batch.persistent_commands, *batch.discrete_actions)
        if (
            batch.session_id != session_id
            or batch.faction_id != claim.faction_id
            or any(
                item.entity_id not in claim.entity_ids or item.faction_id != claim.faction_id
                for item in children
            )
        ):
            raise AuthorizationFailureV2("entity_not_in_claim")
        if any(
            item.action_type == "fire_weapon" and "target_id" in item.payload
            for item in batch.discrete_actions
        ):
            # Authenticated callers can only select an agent-visible opaque
            # contact.  The legacy trusted-local adapter retains its explicit
            # compatibility path for historical action logs.
            raise GatewayContractFailureV2("opaque_contact_required")
        with self._lock:
            seen_principal = self._authenticated_idempotency.setdefault(session_id, {}).get(
                batch.idempotency_key
            )
            if seen_principal is not None and seen_principal != principal.principal_id:
                # Do not surface either a cached receipt or the identity of the
                # caller that already used this key.
                raise AuthorizationFailureV2("controller_not_authorized")
        authority_token = next(
            (
                token
                for token, grant in session.world_view.authority_tokens.items()
                if grant.controller_id == claim.controller_id
                and grant.entity_ids == claim.entity_ids
            ),
            None,
        )
        if authority_token is None:
            raise AuthorizationFailureV2("controller_not_authorized")
        receipt = session.submit_actions(
            batch=batch,
            authority_token=authority_token,
            operation_id=operation_id,
            expected_tick=expected_tick,
        )
        with self._lock:
            self._authenticated_idempotency.setdefault(session_id, {})[
                batch.idempotency_key
            ] = principal.principal_id
        return receipt

    def authenticated_submit(
        self,
        session_id: str,
        *,
        credential: str | None,
        controller_slot_id: str,
        batch: ActionBatchV2,
        operation_id: str,
        expected_tick: int,
    ) -> ActionReceiptV2:
        principal = self.authenticate(session_id, credential=credential)
        return self.submit_as_principal(
            session_id,
            principal_id=principal.principal_id,
            controller_slot_id=controller_slot_id,
            batch=batch,
            operation_id=operation_id,
            expected_tick=expected_tick,
        )

    def authenticated_step(
        self,
        session_id: str,
        *,
        credential: str | None,
        controller_slot_id: str,
        operation_id: str,
        expected_tick: int,
    ) -> ActionApplyReceiptV2:
        """Advance one lockstep tick for an authenticated controller scope."""

        principal = self.authenticate(session_id, credential=credential)
        if not principal.permits_controller(controller_slot_id):
            raise AuthorizationFailureV2("controller_not_authorized")
        try:
            self._get(session_id).world_view.controller_claim(controller_slot_id)
        except KeyError as error:
            raise AuthorizationFailureV2("controller_not_authorized") from error
        return self.step(
            session_id,
            operation_id=operation_id,
            expected_tick=expected_tick,
        )

    def step(
        self, session_id: str, *, operation_id: str, expected_tick: int
    ) -> ActionApplyReceiptV2:
        return self._get(session_id).step(operation_id=operation_id, expected_tick=expected_tick)

    def checkpoint(self, session_id: str) -> SessionCheckpointV2:
        return self._get(session_id).checkpoint()

    def command_status(self, session_id: str, child_id: str) -> str:
        statuses = self._get(session_id).action_status_view.statuses
        try:
            return statuses[child_id]
        except KeyError as error:
            raise KeyError(f"unknown command or action: {child_id}") from error

    def events(self, session_id: str) -> tuple[Mapping[str, Any], ...]:
        world = self._get(session_id).world_view.checkpoint()
        values: list[Mapping[str, Any]] = []
        for tick in world.world_tick_ledger:
            values.extend(tick.event_receipts)
        return tuple(MappingProxyType(dict(item)) for item in values)

    def result(self, session_id: str) -> Mapping[str, Any]:
        mission = self._get(session_id).world_view.checkpoint().mission_scoring_checkpoint
        if mission is None:
            return MappingProxyType({"terminal_result": None, "score_state": {}})
        return MappingProxyType(
            {
                "terminal_result": mission.get("terminal_result"),
                "score_state": mission.get("score_state", {}),
            }
        )

    def authenticated_terminal_result(
        self, session_id: str, *, credential: str | None
    ) -> Mapping[str, Any]:
        """Return only public terminal evidence after authenticating a participant."""

        self.authenticate(session_id, credential=credential)
        return MappingProxyType({"terminal_result": self.result(session_id)["terminal_result"]})

    def visualization(self, session_id: str, *, faction_id: str) -> Any:
        session = self._get(session_id)
        if self._visualization_factory is not None:
            return self._visualization_factory(session, faction_id)
        return FrameBuilderV2.from_observation(
            session.world_view.observation(observer_faction_id=faction_id),
            scenario_id=session.resolved.scenario_id,
        )

    def replay_artifact(self, session_id: str, *, faction_id: str) -> Mapping[str, Any]:
        session = self._get(session_id)
        checkpoint = session.checkpoint()
        frame = self.visualization(session_id, faction_id=faction_id)
        return MappingProxyType(
            {
                "schema_version": "gateway-replay@2.0",
                "session_id": session_id,
                "resolved_hash": checkpoint.resolved_hash,
                "catalog_hash": checkpoint.catalog_hash,
                "model_registry_hash": checkpoint.model_registry_hash,
                "seed": checkpoint.seed,
                "records": (
                    {
                        "tick": frame.tick,
                        "frame": frame.model_dump(mode="json"),
                        "world_checkpoint_hash": checkpoint.world_checkpoint_hash,
                    },
                ),
            }
        )

    def close(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(session_id)
            self._principals.pop(session_id, None)
            self._credential_digests.pop(session_id, None)
            self._authenticated_idempotency.pop(session_id, None)
        if session.state in {
            SessionStateV2.RUNNING,
            SessionStateV2.LOADED,
            SessionStateV2.PAUSED,
        }:
            session.stop()
        session.close()


class StructuredPythonAdapterV2:
    def __init__(self, gateway: AgentGatewayV2, *, session_id: str, faction_id: str) -> None:
        self.gateway = gateway
        self.session_id = session_id
        self.faction_id = faction_id

    def observation(self) -> ObservationV2:
        return self.gateway.observation(self.session_id, faction_id=self.faction_id)

    def submit(
        self, batch: ActionBatchV2, *, authority_token: str, operation_id: str
    ) -> ActionReceiptV2:
        return self.gateway.submit(
            self.session_id,
            batch=batch,
            authority_token=authority_token,
            operation_id=operation_id,
            expected_tick=self.observation().tick,
        )

    def step(self, *, operation_id: str) -> ActionApplyReceiptV2:
        return self.gateway.step(
            self.session_id, operation_id=operation_id, expected_tick=self.observation().tick
        )


class AuthenticatedControllerAdapterV2:
    """Formal Python/Gym adapter bound to one principal and controller claim."""

    def __init__(
        self,
        gateway: AgentGatewayV2,
        *,
        session_id: str,
        principal_id: str,
        controller_slot_id: str,
    ) -> None:
        self.gateway = gateway
        self.session_id = session_id
        self.principal_id = principal_id
        self.controller_slot_id = controller_slot_id

    def observation(self) -> ObservationV2:
        return self.gateway.controller_observation(
            self.session_id,
            principal_id=self.principal_id,
            controller_slot_id=self.controller_slot_id,
        )

    def submit(
        self,
        batch: ActionBatchV2,
        *,
        authority_token: str | None = None,
        operation_id: str,
    ) -> ActionReceiptV2:
        if authority_token is not None:
            raise GatewayContractFailureV2("authenticated_adapter_uses_principal_context")
        return self.gateway.submit_as_principal(
            self.session_id,
            principal_id=self.principal_id,
            controller_slot_id=self.controller_slot_id,
            batch=batch,
            operation_id=operation_id,
            expected_tick=self.observation().tick,
        )

    def step(self, *, operation_id: str) -> ActionApplyReceiptV2:
        return self.gateway.step(
            self.session_id, operation_id=operation_id, expected_tick=self.observation().tick
        )


class GymnasiumAdapterV2:
    """Gymnasium-compatible signatures without owning simulation semantics."""

    def __init__(
        self,
        adapter: StructuredPythonAdapterV2 | AuthenticatedControllerAdapterV2,
        *,
        authority_token: str | None = None,
    ) -> None:
        if isinstance(adapter, StructuredPythonAdapterV2) and authority_token is None:
            raise GatewayContractFailureV2("legacy_adapter_requires_authority_token")
        self.adapter = adapter
        self._authority_token = authority_token

    def reset(self) -> tuple[Mapping[str, object], Mapping[str, object]]:
        observation = self.adapter.observation()
        return MappingProxyType(observation.model_dump(mode="json")), MappingProxyType({})

    def step(
        self, action: ActionBatchV2 | None
    ) -> tuple[Mapping[str, object], float, bool, bool, Mapping[str, object]]:
        if action is not None:
            if isinstance(self.adapter, StructuredPythonAdapterV2):
                authority_token = self._authority_token
                if authority_token is None:
                    raise GatewayContractFailureV2("legacy_adapter_requires_authority_token")
                self.adapter.submit(
                    action,
                    authority_token=authority_token,
                    operation_id=f"gym.submit.{action.idempotency_key}",
                )
            else:
                self.adapter.submit(
                    action,
                    operation_id=f"gym.submit.{action.idempotency_key}",
                )
        receipt = self.adapter.step(operation_id=f"gym.tick.{self.adapter.observation().tick}")
        observation = self.adapter.observation()
        mission_receipts = tuple(getattr(receipt.world_receipt, "mission_receipts", ()))
        terminal_result = (
            None if not mission_receipts else mission_receipts[-1].terminal_result
        )
        outcome = None if terminal_result is None else terminal_result.outcome
        terminated = outcome is not None and outcome != "timeout"
        truncated = outcome == "timeout"
        terminal_payload = (
            None
            if terminal_result is None
            else {
                "rule_id": terminal_result.rule_id,
                "outcome": terminal_result.outcome,
                "priority": terminal_result.priority,
                "tick": terminal_result.tick,
                "latched": terminal_result.latched,
                "trigger_evidence": dict(terminal_result.trigger_evidence),
                "ranking": dict(terminal_result.ranking),
            }
        )
        return (
            MappingProxyType(observation.model_dump(mode="json")),
            0.0,
            terminated,
            truncated,
            MappingProxyType(
                {
                    "receipt_hash": receipt.receipt_hash,
                    "terminal_result": terminal_payload,
                }
            ),
        )


class VectorEnvAdapterV2:
    def __init__(self, environments: Sequence[GymnasiumAdapterV2]) -> None:
        if not environments or len({item.adapter.session_id for item in environments}) != len(
            environments
        ):
            raise ValueError("vector environments require unique nonempty sessions")
        self.environments = tuple(environments)

    def reset(self) -> tuple[tuple[Mapping[str, object], ...], tuple[Mapping[str, object], ...]]:
        values = tuple(item.reset() for item in self.environments)
        return tuple(item[0] for item in values), tuple(item[1] for item in values)

    def step(
        self, actions: Sequence[ActionBatchV2 | None]
    ) -> tuple[tuple[Mapping[str, object], float, bool, bool, Mapping[str, object]], ...]:
        if len(actions) != len(self.environments):
            raise ValueError("vector action cardinality mismatch")
        return tuple(
            environment.step(action)
            for environment, action in zip(self.environments, actions, strict=True)
        )


__all__ = [
    "AgentGatewayV2",
    "AuthenticatedControllerAdapterV2",
    "AuthenticatedPrincipalV2",
    "AuthorizationFailureV2",
    "GatewayContractFailureV2",
    "GymnasiumAdapterV2",
    "StructuredPythonAdapterV2",
    "VectorEnvAdapterV2",
    "SessionFactoryV2",
    "SessionRestoreFactoryV2",
    "VisualizationFactoryV2",
]
