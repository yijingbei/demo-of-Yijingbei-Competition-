"""Versioned scenario schema and loader."""

from openmdbench.scenarios.declarative_v2 import (
    COMPILER_STAGES_V2,
    CompilerErrorV2,
    PackageLimitsV2,
    ResolvedScenarioV2,
    ScenarioCompilerV2,
    ScenarioPackageV2,
    StageRecordV2,
)
from openmdbench.scenarios.schema import Scenario

__all__ = [
    "COMPILER_STAGES_V2",
    "CompilerErrorV2",
    "PackageLimitsV2",
    "ResolvedScenarioV2",
    "Scenario",
    "ScenarioCompilerV2",
    "ScenarioPackageV2",
    "StageRecordV2",
]
