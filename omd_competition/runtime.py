"""Competition assembly through public APIs and the declared independent engine patches."""
from pathlib import Path
import hashlib
import json
import os
import yaml
from openmdbench.catalog.formal_v2 import _registry as formal_model_registry
from openmdbench.catalog.v2 import CatalogV2, CatalogResourceV2, ModelRegistryV2, ModelFactoryMetadataV2
from openmdbench.scenarios.declarative_v2 import ScenarioCompilerV2, ScenarioPackageV2
from openmdbench.sessions.lifecycle_v2 import SessionLifecycleV2, RunnerModeV2
from openmdbench.sessions.gateway_v2 import AgentGatewayV2, AuthenticatedPrincipalV2
from openmdbench.world.factory_v2 import WorldFactoryV2
from openmdbench.dynamics.native_v2 import register_native_dynamics_models_v2
from . import engagement_metrics as interception_metrics
from . import defense_quality
from . import intercept_performance
from .score_evidence import RecordedScorePlugin, ScoreEvidenceRecorder
from .transport_evidence import TransportEvidenceRecorder
from .legacy_plugins import recon_metrics_v1

ROOT=Path(__file__).resolve().parents[1]
ENGINE_ROOT=ROOT/'engine/source_codes'
PACKAGE_VERSION='0.6.0-dev'
SCENE_ROOT=Path(os.environ.get('OPENMD_SUITE_ROOT', str(ROOT))).resolve()
ENGINE_COMMIT='b8c1ad824acb1bd114353147df94847a9152dda2'
def sha256(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def suite(): return json.loads((SCENE_ROOT/'scenarios/suite.json').read_text(encoding='utf-8'))
def entry(scenario):
    return next(row for row in suite()['scenarios'] if row['id']==scenario)
def profile_id(scenario): return scenario
def public_brief(scenario): return json.loads((SCENE_ROOT/entry(scenario)['package']/'public_brief.json').read_text(encoding='utf-8'))
def opponent_plan(scenario): return json.loads((SCENE_ROOT/entry(scenario)['package']/'opponent.json').read_text(encoding='utf-8'))
def validate_initial_combat_resources(resolved):
    """Frozen native combat binds its resource closure at initial world load."""
    roles={'weapons','ammunition','effects','damage_models'}
    registered={(role,b.exact_ref) for e in resolved.entities for role in roles
                for b in e.resource_bindings.get(role,())}
    for event in resolved.events:
        if event.event_type!='spawn':continue
        payload=recon_metrics_v1.plain(event.payload)
        entity=payload['entity']
        bindings=entity.resource_bindings if hasattr(entity,'resource_bindings') else entity['resource_bindings']
        identifier=entity.id if hasattr(entity,'id') else entity['id']
        refs=[(role,b.exact_ref if hasattr(b,'exact_ref') else b['exact_ref'])
              for role in roles for b in bindings.get(role,())]
        missing=sorted(ref for ref in refs if ref not in registered)
        if missing:
            raise ValueError(f'future entity {identifier} needs combat resources absent from initial native registry: {missing}')


def compile_scenario(scenario, *, score_evidence=None):
    row=entry(scenario)
    base_registry = formal_model_registry()
    registry=ModelRegistryV2(interface_version='2.0')
    for metadata in base_registry.snapshot():
        if 'dynamics' in metadata.resource_types: continue
        registry.register(metadata,lambda definition,ref=metadata.exact_ref:base_registry.create(ref,definition))
    register_native_dynamics_models_v2(registry)
    additions=[]
    metric_factories = ((defense_quality,defense_quality.DefenseQualityMetric),
                       (interception_metrics,interception_metrics.InterceptionMetric),
                       (intercept_performance,intercept_performance.InterceptPerformanceMetric),
                       )
    if row['catalog'] == 'ie_set':
        from . import assault_completion, combat_completion
        metric_factories = ((assault_completion, assault_completion.AssaultWithdrawalMetric),
                            (combat_completion, combat_completion.CombatExhaustionMetric),
                            *metric_factories)
    for module,cls in metric_factories:
        registry.register(ModelFactoryMetadataV2(schema_version='2.0',model_id=module.MODEL_ID,version=module.MODEL_VERSION,
            interface_version='2.0',input_schema='catalog-resource@2.0',output_schema='runtime-component@2.0',units={'score':'1'},
            deterministic=True,thread_safe=False,process_safe=True,trusted=True,artifact_sha256='sha256:'+sha256(module.__file__),
            resource_types=('scoring',),field_units={'score':'1'}),
            lambda definition,cls=cls,ref=module.MODEL_REF,artifact='sha256:'+sha256(module.__file__):
                cls() if score_evidence is None else RecordedScorePlugin(cls(),score_evidence,ref,artifact))
        additions.append(CatalogResourceV2(schema_version='2.0',resource_type='scoring',id='scoring.'+module.MODEL_ID.removeprefix('models.'),
            version=module.MODEL_VERSION,engine_compatibility='>=2.0.0,<3.0.0',model_id=module.MODEL_REF,content={'unit':'1','session_local':True}))
    registry.freeze()
    resources=[CatalogResourceV2.model_validate(r) for r in yaml.safe_load((SCENE_ROOT/'scenarios/catalog'/ (row['catalog']+'.yaml')).read_bytes())['resources']]
    catalog=CatalogV2([*resources,*additions],engine_version='2.0.0',model_registry=registry)
    resolved=ScenarioCompilerV2(catalog=catalog).compile(ScenarioPackageV2.from_directory(SCENE_ROOT/row['package']))
    from .factions import validate_scenario
    validate_scenario(resolved, public_brief(scenario))
    validate_initial_combat_resources(resolved)
    from .weather_contract import validate_weather_schedule
    validate_weather_schedule(public_brief(scenario), resolved, catalog)
    return resolved,catalog
class RefereeWorldFactory(WorldFactoryV2):
    """Keep a read-only combat evidence reader for the organizer process.

    The native build method creates the world unchanged. Its public immutable
    combat properties avoid requiring a full, restorable mission checkpoint.
    This reader never enters a participant observation or subprocess.
    """
    def build(self, *args, **kwargs):
        world = super().build(*args, **kwargs)
        self._mission_reader = lambda: world.mission_fact_snapshot(
            tick=world.tick, include_event_receipts=False)
        self._transport_reader = world.event_state_snapshot
        # Referee-only reverse resolution, used solely by opt-in identification.
        self.resolve_contact = world.resolve_controller_contact_handle
        self._projectile_reader = lambda: {
            'tick': world.tick,
            'missile_flights': world.missile_flights,
            'pending_impacts': world.pending_impacts,
            'pending_impact_receipts': world.pending_impact_receipts,
            'missile_terminal_receipts': world.missile_terminal_receipts,
            'combat_activity': world.mission_fact_snapshot(
                tick=world.tick, include_event_receipts=False).combat_activity,
        }
        return world

    def projectile_snapshot(self):
        return self._projectile_reader()

    def mission_snapshot(self):
        """Immutable referee facts, including future and removed entity states."""
        return self._mission_reader()

    def transport_snapshot(self):
        """Public native event state; only the referee recorder projects it."""
        return self._transport_reader()

    def weather_snapshot(self):
        """Read the native global environment; never synthesize capability state."""
        return self._transport_reader().weather_state


def create_session(scenario,seed,session_id,*,referee=False):
    score_evidence = ScoreEvidenceRecorder() if referee and entry(scenario)['track']=='interception' else None
    resolved,catalog=compile_scenario(scenario,score_evidence=score_evidence)
    factory = (RefereeWorldFactory if referee else WorldFactoryV2)(model_registry=catalog.model_registry)
    if referee:
        factory.catalog = catalog
    if score_evidence is not None:
        factory.score_plugin_evidence = score_evidence
    session=SessionLifecycleV2.create(session_id=session_id,seed=seed,resolved=resolved,expected_resolved_hash=resolved.resolved_hash,
        catalog_hash=resolved.catalog_hash,model_registry_hash=resolved.model_registry_hash,world_factory=factory,
        runner_mode=RunnerModeV2.LOCKSTEP,physics_dt_seconds=float(resolved.world.tick_seconds),decision_interval_ticks=1)
    gateway=AgentGatewayV2();gateway.attach(session);gateway.control(session_id,'load');gateway.control(session_id,'start')
    if score_evidence is not None:
        factory.transport_evidence = TransportEvidenceRecorder(factory.transport_snapshot(),
            seed=seed, resolved_hash=resolved.resolved_hash,
            dt_seconds=float(resolved.world.tick_seconds))
    for slot in resolved.controller_slots:
        gateway.grant_principal(session_id,AuthenticatedPrincipalV2(principal_id='principal.'+slot.id,controller_slot_ids=(slot.id,)))
    return session,gateway
