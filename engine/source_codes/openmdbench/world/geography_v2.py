"""Scenario-independent coordinate conversion for runtime world systems.

The compiler and runtime deliberately share the same small-angle WGS84/ENU
contract.  Runtime consumers only see immutable, metre-based values; no
scenario document or catalog is retained by this service.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol, Self, cast

CoordinateFrameNameV2 = Literal["local_m", "wgs84", "map"]
Vector3V2 = tuple[float, float, float]

_EARTH_RADIUS_M = 6_378_137.0
_SUPPORTED_FRAMES = frozenset(("local_m", "wgs84", "map"))
_LAYER_KINDS = frozenset(
    ("land", "coast", "island", "no_go", "terrain_height", "bathymetry", "static_obstacles")
)
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


class GeographyErrorV2(ValueError):
    """Stable geography boundary error with machine-readable evidence."""

    def __init__(
        self,
        *,
        code: str,
        path: Sequence[str],
        value: object,
        reason: str,
        suggestion: str,
    ) -> None:
        self.code = code
        self.path = tuple(path)
        self.value = value
        self.reason = reason
        self.suggestion = suggestion
        super().__init__(f"{code} at {'/'.join(self.path)}: {reason}; suggestion: {suggestion}")


def _error(
    code: str,
    path: Sequence[str],
    value: object,
    reason: str,
    suggestion: str,
) -> GeographyErrorV2:
    return GeographyErrorV2(
        code=code,
        path=path,
        value=value,
        reason=reason,
        suggestion=suggestion,
    )


def _vector3(value: object, *, path: Sequence[str]) -> Vector3V2:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 3
        or any(
            not isinstance(axis, (int, float))
            or isinstance(axis, bool)
            or not math.isfinite(float(axis))
            for axis in value
        )
    ):
        raise _error(
            "geography.coordinate_invalid",
            path,
            value,
            "coordinate must contain exactly three finite numeric axes",
            "provide a finite (x, y, z), (longitude, latitude, height), or map tuple",
        )
    return cast(Vector3V2, tuple(float(axis) for axis in value))


def _positive_vector3(value: object, *, path: Sequence[str]) -> Vector3V2:
    result = _vector3(value, path=path)
    if any(axis <= 0.0 for axis in result):
        raise _error(
            "geography.transform_invalid",
            path,
            value,
            "map resolution must be positive on every axis",
            "provide positive finite metres-per-unit values",
        )
    return result


def _bounds(value: object, *, path: Sequence[str]) -> tuple[Vector3V2, Vector3V2]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) != 2
    ):
        raise _error(
            "geography.bounds_invalid",
            path,
            value,
            "bounds must contain lower and upper three-axis vectors",
            "provide ((min_x, min_y, min_z), (max_x, max_y, max_z))",
        )
    lower = _vector3(value[0], path=(*path, "0"))
    upper = _vector3(value[1], path=(*path, "1"))
    if any(lower[index] >= upper[index] for index in range(3)):
        raise _error(
            "geography.bounds_invalid",
            path,
            value,
            "bounds must be strictly ordered on every axis",
            "place every lower bound below its corresponding upper bound",
        )
    return lower, upper


def _point2_runtime(value: object) -> tuple[float, float]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes, bytearray))
        or len(value) < 2
    ):
        raise ValueError("position must contain at least two finite axes")
    point = _vector3((*value[:2], 0.0), path=("position_m",))
    return point[0], point[1]


def _point_segment_distance_squared(
    point: tuple[float, float], first: tuple[float, float], second: tuple[float, float]
) -> float:
    dx = second[0] - first[0]
    dy = second[1] - first[1]
    length = dx * dx + dy * dy
    fraction = (
        0.0
        if length == 0.0
        else min(
            1.0,
            max(0.0, ((point[0] - first[0]) * dx + (point[1] - first[1]) * dy) / length),
        )
    )
    return (point[0] - first[0] - fraction * dx) ** 2 + (point[1] - first[1] - fraction * dy) ** 2


class _CoordinateFrameLike(Protocol):
    canonical_frame: str
    origin_wgs84: Sequence[float]
    axis_orientation: str
    height_semantics: str
    local_bounds_m: Sequence[Sequence[float]]
    tolerance_m: float


class _ResolvedWorldLike(Protocol):
    coordinate_frame: _CoordinateFrameLike | None
    map_transform: Mapping[str, object] | None
    zones: Sequence[_ResolvedZoneLike]
    deployment_excluded_zone_ids: Sequence[str]


class _ResolvedScenarioLike(Protocol):
    world: _ResolvedWorldLike
    entities: Sequence[object]
    events: Sequence[object]
    world_resource_bindings: Sequence[object]

    def validate_integrity(self) -> None: ...


class _ResolvedLayerBindingLike(Protocol):
    exact_ref: str
    resource_type: str
    version: str
    content_hash: str
    content: Mapping[str, object]
    normalized_content: Mapping[str, object]
    engine_compatibility: str
    model_ref: str
    dependencies: Sequence[str]


class _ResolvedGeometryLike(Protocol):
    type: str
    positions_m: Sequence[Sequence[float]]
    center_m: Sequence[float] | None
    radius_m: float | None


class _ResolvedZoneLike(Protocol):
    id: str
    geometry: _ResolvedGeometryLike
    domains: Sequence[str]


@dataclass(frozen=True, slots=True)
class CoordinateV2:
    """A typed immutable coordinate at a public API boundary."""

    frame: CoordinateFrameNameV2
    coordinates: Vector3V2

    def __post_init__(self) -> None:
        if self.frame not in _SUPPORTED_FRAMES:
            raise ValueError("unsupported coordinate frame")
        object.__setattr__(self, "coordinates", _vector3(self.coordinates, path=("coordinates",)))


@dataclass(frozen=True, slots=True)
class GeographyZoneV2:
    zone_id: str
    geometry_type: Literal["polygon", "circle"]
    positions_m: tuple[tuple[float, float], ...] = ()
    center_m: tuple[float, float] | None = None
    radius_m: float | None = None
    domains: tuple[str, ...] = ()
    deployment_excluded: bool = False


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(float(value)):
            raise ValueError("layer content must contain only finite numbers")
        return value
    raise ValueError("layer content must be JSON-compatible")


@dataclass(frozen=True, slots=True)
class GeographyLayerBindingV2:
    """Exact, immutable catalog evidence used to materialize a geography layer."""

    exact_ref: str
    resource_type: str
    version: str
    content_hash: str
    layer_kind: str
    normalized_content: Mapping[str, object]
    hash_scheme: Literal["layer-content", "catalog-resource"] = "layer-content"
    catalog_content: Mapping[str, object] | None = None
    catalog_engine_compatibility: str | None = None
    catalog_model_ref: str | None = None
    catalog_dependencies: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        expected_hash = self.compute_content_hash(
            exact_ref=self.exact_ref,
            resource_type=self.resource_type,
            version=self.version,
            layer_kind=self.layer_kind,
            normalized_content=self.normalized_content,
        )
        if self.hash_scheme == "catalog-resource":
            try:
                from openmdbench.catalog.v2 import CatalogResourceV2

                rebuilt = CatalogResourceV2(
                    schema_version="2.0",
                    resource_type=cast(Any, self.resource_type),
                    id=self.exact_ref.rsplit("@", 1)[0],
                    version=self.version,
                    engine_compatibility=cast(str, self.catalog_engine_compatibility),
                    model_id=cast(str, self.catalog_model_ref),
                    dependencies=self.catalog_dependencies,
                    content=dict(cast(Mapping[str, Any], self.catalog_content)),
                )
                expected_hash = rebuilt.content_hash
            except (TypeError, ValueError) as error:
                raise _error(
                    "geography.layer_binding_integrity_invalid",
                    ("layer_binding", "catalog_content"),
                    type(error).__name__,
                    "resolved catalog layer evidence cannot reproduce its canonical hash",
                    "use an intact ResolvedResourceBindingV2",
                ) from error
        valid_identity = (
            isinstance(self.exact_ref, str)
            and self.exact_ref.endswith(f"@{self.version}")
            and self.resource_type == "environments"
            and isinstance(self.version, str)
            and bool(self.version)
            and isinstance(self.content_hash, str)
            and _SHA256_RE.fullmatch(self.content_hash) is not None
            and self.content_hash == expected_hash
            and (self.layer_kind in _LAYER_KINDS or self.hash_scheme == "catalog-resource")
            and isinstance(self.normalized_content, Mapping)
        )
        if not valid_identity:
            raise _error(
                "geography.layer_binding_integrity_invalid",
                ("layer_binding",),
                self.exact_ref,
                "geography layer binding identity or digest evidence is invalid",
                "use one exact integrity-validated environments catalog binding",
            )
        try:
            frozen = _freeze_json(self.normalized_content)
            frozen_catalog = (
                None if self.catalog_content is None else _freeze_json(self.catalog_content)
            )
        except (TypeError, ValueError) as error:
            raise _error(
                "geography.layer_binding_integrity_invalid",
                ("layer_binding", "normalized_content"),
                type(error).__name__,
                "geography layer normalized content is not finite immutable JSON",
                "use normalized content from the validated resolved binding",
            ) from error
        object.__setattr__(self, "normalized_content", cast(Mapping[str, object], frozen))
        object.__setattr__(
            self,
            "catalog_content",
            cast(Mapping[str, object] | None, frozen_catalog),
        )

    @staticmethod
    def compute_content_hash(
        *,
        exact_ref: str,
        resource_type: str,
        version: str,
        layer_kind: str,
        normalized_content: Mapping[str, object],
    ) -> str:
        payload = {
            "exact_ref": exact_ref,
            "resource_type": resource_type,
            "version": version,
            "layer_kind": layer_kind,
            "normalized_content": normalized_content,
        }

        def plain(value: object) -> object:
            if isinstance(value, Mapping):
                return {str(key): plain(item) for key, item in value.items()}
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                return [plain(item) for item in value]
            return value

        encoded = json.dumps(
            plain(payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_catalog_content(
        cls,
        *,
        exact_ref: str,
        version: str,
        layer_kind: str,
        normalized_content: Mapping[str, object],
        resource_type: str = "environments",
        limits: GeographyLayerLimitsV2 | None = None,
    ) -> Self:
        _validate_layer_content(
            layer_kind,
            normalized_content,
            limits=limits or GeographyLayerLimitsV2(),
        )
        return cls(
            exact_ref=exact_ref,
            resource_type=resource_type,
            version=version,
            content_hash=cls.compute_content_hash(
                exact_ref=exact_ref,
                resource_type=resource_type,
                version=version,
                layer_kind=layer_kind,
                normalized_content=normalized_content,
            ),
            layer_kind=layer_kind,
            normalized_content=normalized_content,
        )

    @classmethod
    def from_unchecked_resolved_payload_for_integrity_test(
        cls,
        *,
        exact_ref: str,
        version: str,
        layer_kind: str,
        normalized_content: Mapping[str, object],
    ) -> Self:
        """Build hash-consistent evidence so resolved semantic checks can be tested."""

        return cls(
            exact_ref=exact_ref,
            resource_type="environments",
            version=version,
            content_hash=cls.compute_content_hash(
                exact_ref=exact_ref,
                resource_type="environments",
                version=version,
                layer_kind=layer_kind,
                normalized_content=normalized_content,
            ),
            layer_kind=layer_kind,
            normalized_content=normalized_content,
        )

    @classmethod
    def from_resolved_binding(cls, binding: _ResolvedLayerBindingLike) -> Self:
        try:
            resource_type = str(binding.resource_type)
            if resource_type != "environments":
                raise ValueError("resolved layer binding is not an environments resource")
            content = binding.content
            normalized = binding.normalized_content
            kind_value = normalized.get("layer_kind", "environment")
            if kind_value in _LAYER_KINDS:
                _validate_layer_content(
                    str(kind_value),
                    normalized,
                    limits=GeographyLayerLimitsV2(),
                )
            return cls(
                exact_ref=str(binding.exact_ref),
                resource_type=resource_type,
                version=str(binding.version),
                content_hash=str(binding.content_hash),
                layer_kind=str(kind_value),
                normalized_content=normalized,
                hash_scheme="catalog-resource",
                catalog_content=content,
                catalog_engine_compatibility=str(binding.engine_compatibility),
                catalog_model_ref=str(binding.model_ref),
                catalog_dependencies=tuple(binding.dependencies),
            )
        except GeographyErrorV2:
            raise
        except (AttributeError, TypeError, ValueError) as error:
            raise _error(
                "geography.layer_binding_integrity_invalid",
                ("resolved_binding",),
                type(error).__name__,
                "resolved catalog binding evidence is incomplete or inconsistent",
                "use an integrity-validated ResolvedResourceBindingV2",
            ) from error


@dataclass(frozen=True, slots=True)
class GeographyLayerLimitsV2:
    max_features: int = 100
    max_vertices: int = 100_000
    max_coordinate_abs_m: float = 100_000_000.0

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_features, int)
            or isinstance(self.max_features, bool)
            or self.max_features <= 0
            or not isinstance(self.max_vertices, int)
            or isinstance(self.max_vertices, bool)
            or self.max_vertices <= 0
            or not math.isfinite(self.max_coordinate_abs_m)
            or self.max_coordinate_abs_m <= 0.0
        ):
            raise ValueError("geography layer limits must be positive and finite")


def _layer_failure(path: Sequence[str], value: object, reason: str) -> GeographyErrorV2:
    return _error(
        "geography.layer_schema_invalid",
        path,
        value,
        reason,
        "provide bounded unique finite geometry matching the controlled layer schema",
    )


def _segments_intersect(
    first: tuple[float, float],
    second: tuple[float, float],
    third: tuple[float, float],
    fourth: tuple[float, float],
) -> bool:
    # Collinear but disjoint edges must not invalidate a simple concave polygon.
    if (max(first[0], second[0]) < min(third[0], fourth[0])
            or max(third[0], fourth[0]) < min(first[0], second[0])
            or max(first[1], second[1]) < min(third[1], fourth[1])
            or max(third[1], fourth[1]) < min(first[1], second[1])):
        return False
    def orientation(
        a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    return orientation(first, second, third) * orientation(first, second, fourth) <= 0.0 and (
        orientation(third, fourth, first) * orientation(third, fourth, second) <= 0.0
    )


def _validate_layer_content(
    layer_kind: str,
    content: Mapping[str, object],
    *,
    limits: GeographyLayerLimitsV2,
) -> None:
    if layer_kind not in _LAYER_KINDS or not isinstance(content, Mapping):
        raise _layer_failure(("layer_kind",), layer_kind, "layer kind or content is invalid")
    controlled_content = {key: value for key, value in content.items() if key != "layer_kind"}
    if set(controlled_content) == {"features"} and controlled_content.get("features") in (
        (),
        [],
    ):
        return
    group = {
        "land": "polygons",
        "coast": "polylines",
        "island": "polygons",
        "no_go": "polygons",
        "terrain_height": "samples",
        "bathymetry": "samples",
        "static_obstacles": "obstacles",
    }[layer_kind]
    if set(controlled_content) != {group} or not isinstance(
        controlled_content.get(group), Sequence
    ):
        raise _layer_failure(("normalized_content",), content, "layer fields are not controlled")
    features = cast(Sequence[object], controlled_content[group])
    if len(features) > limits.max_features:
        raise _layer_failure((group,), len(features), "layer feature quota is exceeded")
    identifiers: set[str] = set()
    vertex_count = 0
    for index, raw in enumerate(features):
        if not isinstance(raw, Mapping):
            raise _layer_failure((group, str(index)), raw, "layer feature must be an object")
        feature = cast(Mapping[str, object], raw)
        if group in {"polygons", "polylines"}:
            if set(feature) != {"id", "vertices_m"}:
                raise _layer_failure((group, str(index)), feature, "geometry fields are invalid")
            identifier = feature["id"]
            vertices_raw = feature["vertices_m"]
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in identifiers
                or not isinstance(vertices_raw, Sequence)
            ):
                raise _layer_failure(
                    (group, str(index), "id"), identifier, "geometry ID is invalid"
                )
            identifiers.add(identifier)
            vertices: list[tuple[float, float]] = []
            for vertex_index, raw_vertex in enumerate(vertices_raw):
                if (
                    not isinstance(raw_vertex, Sequence)
                    or isinstance(raw_vertex, (str, bytes, bytearray))
                    or len(raw_vertex) != 2
                    or any(
                        isinstance(axis, bool)
                        or not isinstance(axis, (int, float))
                        or not math.isfinite(float(axis))
                        or abs(float(axis)) > limits.max_coordinate_abs_m
                        for axis in raw_vertex
                    )
                ):
                    raise _layer_failure(
                        (group, str(index), "vertices_m", str(vertex_index)),
                        raw_vertex,
                        "geometry vertex is not a bounded finite point",
                    )
                vertices.append((float(raw_vertex[0]), float(raw_vertex[1])))
            minimum = 3 if group == "polygons" else 2
            if len(vertices) < minimum or any(
                vertices[item] == vertices[item - 1] for item in range(1, len(vertices))
            ):
                raise _layer_failure(
                    (group, str(index), "vertices_m"), vertices, "geometry is degenerate"
                )
            vertex_count += len(vertices)
            if group == "polygons":
                area = sum(
                    vertices[item][0] * vertices[(item + 1) % len(vertices)][1]
                    - vertices[(item + 1) % len(vertices)][0] * vertices[item][1]
                    for item in range(len(vertices))
                )
                if abs(area) <= 1e-12:
                    raise _layer_failure(
                        (group, str(index), "vertices_m"), vertices, "polygon area is zero"
                    )
                edges = tuple(
                    (vertices[item], vertices[(item + 1) % len(vertices)])
                    for item in range(len(vertices))
                )
                for first_index, first_edge in enumerate(edges):
                    for second_index, second_edge in enumerate(
                        edges[first_index + 1 :], first_index + 1
                    ):
                        if second_index in {
                            first_index,
                            first_index + 1,
                            (first_index - 1) % len(edges),
                        }:
                            continue
                        if _segments_intersect(*first_edge, *second_edge):
                            raise _layer_failure(
                                (group, str(index), "vertices_m"),
                                vertices,
                                "polygon self-intersects",
                            )
        elif group == "samples":
            if set(feature) != {"position_m", "value_m"}:
                raise _layer_failure((group, str(index)), feature, "sample fields are invalid")
            position = feature["position_m"]
            value = feature["value_m"]
            if (
                not isinstance(position, Sequence)
                or isinstance(position, (str, bytes, bytearray))
                or len(position) != 2
                or any(
                    isinstance(axis, bool)
                    or not isinstance(axis, (int, float))
                    or not math.isfinite(float(axis))
                    or abs(float(axis)) > limits.max_coordinate_abs_m
                    for axis in position
                )
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise _layer_failure((group, str(index)), feature, "sample is not finite")
        else:
            if set(feature) != {"id", "shape", "center_m", "radius_m"}:
                raise _layer_failure((group, str(index)), feature, "obstacle fields are invalid")
            identifier = feature["id"]
            center = feature["center_m"]
            radius = feature["radius_m"]
            if (
                not isinstance(identifier, str)
                or not identifier
                or identifier in identifiers
                or feature["shape"] != "sphere"
                or not isinstance(center, Sequence)
                or len(center) != 3
                or any(
                    isinstance(axis, bool)
                    or not isinstance(axis, (int, float))
                    or not math.isfinite(float(axis))
                    for axis in center
                )
                or isinstance(radius, bool)
                or not isinstance(radius, (int, float))
                or not math.isfinite(float(radius))
                or float(radius) <= 0.0
            ):
                raise _layer_failure((group, str(index)), feature, "obstacle is invalid")
            identifiers.add(identifier)
    if vertex_count > limits.max_vertices:
        raise _layer_failure((group,), vertex_count, "layer vertex quota is exceeded")


@dataclass(frozen=True, slots=True)
class GeographyFeatureV2:
    id: str
    layer_kind: str
    distance_m: float


@dataclass(frozen=True, slots=True)
class GeographyLayerV2:
    kind: str
    exact_ref: str
    content_hash: str
    normalized_content: Mapping[str, object]

    @classmethod
    def from_binding(cls, binding: GeographyLayerBindingV2) -> Self:
        if not isinstance(binding, GeographyLayerBindingV2):
            raise _error(
                "geography.layer_binding_integrity_invalid",
                ("layer_binding",),
                type(binding).__name__,
                "geography layer requires typed exact catalog binding evidence",
                "pass GeographyLayerBindingV2 from a validated resolved scenario",
            )
        return cls(
            kind=binding.layer_kind,
            exact_ref=binding.exact_ref,
            content_hash=binding.content_hash,
            normalized_content=binding.normalized_content,
        )

    @classmethod
    def from_resolved_binding(cls, binding: GeographyLayerBindingV2) -> Self:
        """Materialize only after revalidating resolved layer semantics and quotas."""

        if not isinstance(binding, GeographyLayerBindingV2):
            raise _error(
                "geography.layer_binding_integrity_invalid",
                ("layer_binding",),
                type(binding).__name__,
                "resolved layer evidence must use the typed binding boundary",
                "pass GeographyLayerBindingV2 from a validated resolved scenario",
            )
        _validate_layer_content(
            binding.layer_kind,
            binding.normalized_content,
            limits=GeographyLayerLimitsV2(),
        )
        return cls.from_binding(binding)


@dataclass(frozen=True, slots=True)
class SurfaceHeightRegionV2:
    """Explicit flat terrain prism, never inferred from a coastline alone."""

    region_id: str
    vertices_m: tuple[tuple[float, float], ...]
    elevation_m: float

    def __post_init__(self) -> None:
        _validate_layer_content(
            "land", {"polygons": [{"id": self.region_id, "vertices_m": self.vertices_m}]},
            limits=GeographyLayerLimitsV2(),
        )
        if (isinstance(self.elevation_m, bool)
                or not isinstance(self.elevation_m, (int, float))
                or not math.isfinite(self.elevation_m)):
            raise ValueError("surface region elevation must be finite")
        object.__setattr__(self, "vertices_m", tuple(tuple(map(float, p)) for p in self.vertices_m))
        object.__setattr__(self, "elevation_m", float(self.elevation_m))

    def contains(self, position_m: Sequence[float], *, tolerance_m: float = 1e-6) -> bool:
        x, y = position_m[:2]
        inside = False
        for a, b in zip(self.vertices_m, self.vertices_m[1:] + self.vertices_m[:1]):
            if _point_segment_distance_squared((x, y), a, b) <= tolerance_m ** 2:
                return True
            if (a[1] > y) != (b[1] > y) and x < (b[0]-a[0])*(y-a[1])/(b[1]-a[1])+a[0]:
                inside = not inside
        return inside

    def blocks(self, start_m: Vector3V2, end_m: Vector3V2, *, tolerance_m: float) -> bool:
        """Intersect a 3D segment with the closed, downward terrain prism.

        Split at polygon crossings; evaluating each resulting interior interval
        also handles concave footprints, grazing edges and endpoints inside.
        """
        if min(start_m[2], end_m[2]) > self.elevation_m + tolerance_m:
            return False
        dx, dy = end_m[0]-start_m[0], end_m[1]-start_m[1]
        length2 = dx*dx+dy*dy
        if length2 <= tolerance_m ** 2:
            return self.contains(start_m, tolerance_m=tolerance_m)
        fractions = {0.0, 1.0}
        for a, b in zip(self.vertices_m, self.vertices_m[1:] + self.vertices_m[:1]):
            ex, ey = b[0]-a[0], b[1]-a[1]
            ax, ay = a[0]-start_m[0], a[1]-start_m[1]
            cross = dx*ey-dy*ex
            if abs(cross) > 1e-12 * max(1.0, math.sqrt(length2)*math.hypot(ex, ey)):
                t, u = (ax*ey-ay*ex)/cross, (ax*dy-ay*dx)/cross
                if 0.0 <= t <= 1.0 and 0.0 <= u <= 1.0:
                    fractions.add(t)
            elif abs(ax*dy-ay*dx) <= tolerance_m * math.sqrt(length2):
                for p in (a, b):
                    t = ((p[0]-start_m[0])*dx+(p[1]-start_m[1])*dy)/length2
                    if 0.0 <= t <= 1.0:
                        fractions.add(t)
        ordered = sorted(fractions)
        dz = end_m[2]-start_m[2]
        for t in ordered:
            p = (start_m[0]+t*dx, start_m[1]+t*dy)
            if self.contains(p, tolerance_m=tolerance_m) and start_m[2]+t*dz <= self.elevation_m+tolerance_m:
                return True
        for lo, hi in zip(ordered, ordered[1:]):
            mid = (lo+hi)/2.0
            if self.contains((start_m[0]+mid*dx, start_m[1]+mid*dy), tolerance_m=tolerance_m):
                if min(start_m[2]+lo*dz, start_m[2]+hi*dz) <= self.elevation_m+tolerance_m:
                    return True
        return False


@dataclass(frozen=True, slots=True)
class GeographyServiceV2:
    """Immutable WGS84/local/map conversion and navigation service."""

    origin_wgs84: Vector3V2
    local_bounds_m: tuple[Vector3V2, Vector3V2]
    map_resolution_m_per_unit: Vector3V2
    map_origin_local_m: Vector3V2
    map_bounds: tuple[Vector3V2, Vector3V2]
    tolerance_m: float
    spatial_zones: tuple[GeographyZoneV2, ...] = ()
    layers: tuple[GeographyLayerV2, ...] = ()
    surface_elevation_reference: Literal["local_sea_level"] | None = None
    surface_elevation_constant_m: float | None = None
    surface_height_regions: tuple[SurfaceHeightRegionV2, ...] = ()

    def __post_init__(self) -> None:
        origin = _vector3(self.origin_wgs84, path=("origin_wgs84",))
        if not -180.0 <= origin[0] <= 180.0 or not -90.0 <= origin[1] <= 90.0:
            raise _error(
                "geography.origin_invalid",
                ("origin_wgs84",),
                self.origin_wgs84,
                "WGS84 origin longitude or latitude is outside its valid range",
                "use longitude [-180, 180] and latitude [-90, 90]",
            )
        if abs(math.cos(math.radians(origin[1]))) <= 1e-12:
            raise _error(
                "geography.polar_origin_unsupported",
                ("origin_wgs84", "latitude"),
                origin[1],
                "east-west local scale is singular at a geographic pole",
                "choose a finite origin strictly away from latitude +/-90 degrees",
            )
        if (
            not isinstance(self.tolerance_m, (int, float))
            or isinstance(self.tolerance_m, bool)
            or not math.isfinite(float(self.tolerance_m))
            or float(self.tolerance_m) <= 0.0
        ):
            raise _error(
                "geography.tolerance_invalid",
                ("tolerance_m",),
                self.tolerance_m,
                "coordinate tolerance must be positive and finite",
                "provide a positive tolerance in metres",
            )
        object.__setattr__(self, "origin_wgs84", origin)
        object.__setattr__(
            self,
            "local_bounds_m",
            _bounds(self.local_bounds_m, path=("local_bounds_m",)),
        )
        object.__setattr__(
            self,
            "map_resolution_m_per_unit",
            _positive_vector3(
                self.map_resolution_m_per_unit,
                path=("map_transform", "resolution_m_per_unit"),
            ),
        )
        object.__setattr__(
            self,
            "map_origin_local_m",
            _vector3(
                self.map_origin_local_m,
                path=("map_transform", "map_origin_local_m"),
            ),
        )
        object.__setattr__(
            self,
            "map_bounds",
            _bounds(self.map_bounds, path=("map_transform", "map_bounds")),
        )
        object.__setattr__(self, "tolerance_m", float(self.tolerance_m))
        object.__setattr__(
            self,
            "spatial_zones",
            tuple(sorted(self.spatial_zones, key=lambda item: item.zone_id)),
        )
        object.__setattr__(
            self, "layers", tuple(sorted(self.layers, key=lambda item: item.exact_ref))
        )
        regions = tuple(sorted(self.surface_height_regions, key=lambda r: r.region_id))
        if len(regions) > 100 or len({r.region_id for r in regions}) != len(regions):
            raise ValueError("surface height regions must have bounded unique identities")
        if regions and self.surface_elevation_reference is None:
            raise ValueError("surface height regions require an explicit surface reference")
        object.__setattr__(self, "surface_height_regions", regions)
        if self.surface_elevation_reference is None:
            if self.surface_elevation_constant_m is not None:
                raise _error(
                    "geography.surface_elevation_invalid",
                    ("surface_elevation",),
                    self.surface_elevation_constant_m,
                    "a surface elevation value requires an explicit reference",
                    "declare an explicit local surface reference in the map resource",
                )
        elif self.surface_elevation_reference != "local_sea_level":
            raise _error(
                "geography.surface_elevation_invalid",
                ("surface_elevation", "reference"),
                self.surface_elevation_reference,
                "surface elevation reference is unsupported",
                "use the explicit local_sea_level reference or provide a supported geography layer",
            )
        elif (
            isinstance(self.surface_elevation_constant_m, bool)
            or not isinstance(self.surface_elevation_constant_m, (int, float))
            or not math.isfinite(float(self.surface_elevation_constant_m))
        ):
            raise _error(
                "geography.surface_elevation_invalid",
                ("surface_elevation", "elevation_m"),
                self.surface_elevation_constant_m,
                "surface elevation must be finite",
                "provide a finite local sea-level elevation in metres",
            )
        else:
            object.__setattr__(
                self, "surface_elevation_constant_m", float(self.surface_elevation_constant_m)
            )

    @classmethod
    def from_config(
        cls,
        *,
        origin_wgs84: Sequence[float],
        axis_orientation: str,
        tolerance_m: float,
        local_bounds_m: Sequence[Sequence[float]],
        map_resolution_m_per_unit: Sequence[float] = (1.0, 1.0, 1.0),
        map_origin_local_m: Sequence[float] = (0.0, 0.0, 0.0),
        map_bounds: Sequence[Sequence[float]] | None = None,
    ) -> Self:
        """Build a complete service from trusted typed runtime configuration."""

        if axis_orientation != "east_north_up":
            raise _error(
                "geography.axis_orientation_unsupported",
                ("axis_orientation",),
                axis_orientation,
                "runtime geography supports east-north-up axes",
                "use east_north_up or adapt coordinates before this boundary",
            )
        resolved_local_bounds = _bounds(local_bounds_m, path=("local_bounds_m",))
        return cls(
            origin_wgs84=cast(Vector3V2, tuple(origin_wgs84)),
            local_bounds_m=resolved_local_bounds,
            map_resolution_m_per_unit=cast(Vector3V2, tuple(map_resolution_m_per_unit)),
            map_origin_local_m=cast(Vector3V2, tuple(map_origin_local_m)),
            map_bounds=(
                resolved_local_bounds
                if map_bounds is None
                else cast(
                    tuple[Vector3V2, Vector3V2],
                    tuple(tuple(item) for item in map_bounds),
                )
            ),
            tolerance_m=tolerance_m,
        )

    @classmethod
    def from_config_with_layers(
        cls,
        *,
        origin_wgs84: Sequence[float],
        local_bounds_m: Sequence[Sequence[float]],
        layers: Sequence[GeographyLayerV2],
        tolerance_m: float = 1e-6,
    ) -> Self:
        base = cls.from_config(
            origin_wgs84=origin_wgs84,
            axis_orientation="east_north_up",
            tolerance_m=tolerance_m,
            local_bounds_m=local_bounds_m,
        )
        if any(not isinstance(item, GeographyLayerV2) for item in layers):
            raise _error(
                "geography.layer_binding_integrity_invalid",
                ("layers",),
                type(layers).__name__,
                "layer service requires typed immutable layer materializations",
                "materialize every layer through GeographyLayerV2.from_binding",
            )
        return cls(
            origin_wgs84=base.origin_wgs84,
            local_bounds_m=base.local_bounds_m,
            map_resolution_m_per_unit=base.map_resolution_m_per_unit,
            map_origin_local_m=base.map_origin_local_m,
            map_bounds=base.map_bounds,
            tolerance_m=base.tolerance_m,
            layers=tuple(layers),
        )

    def _layer(self, kind: str) -> GeographyLayerV2:
        matches = tuple(item for item in self.layers if item.kind == kind)
        if len(matches) != 1:
            raise _error(
                "geography.layer_unknown",
                ("layer_kind",),
                kind,
                "query requires exactly one materialized layer of this kind",
                "bind one trusted exact geography layer",
            )
        return matches[0]

    def contains(self, *, layer_kind: str, position_m: Sequence[float]) -> bool:
        point = _vector3(position_m, path=("position_m",))
        content = self._layer(layer_kind).normalized_content
        polygons = content.get("polygons", ())
        for feature in cast(Sequence[Mapping[str, object]], polygons):
            vertices = tuple(
                (float(item[0]), float(item[1]))
                for item in cast(Sequence[Sequence[float]], feature["vertices_m"])
            )
            inside = False
            previous = len(vertices) - 1
            for current in range(len(vertices)):
                first, second = vertices[current], vertices[previous]
                if (first[1] > point[1]) != (second[1] > point[1]) and point[0] < (
                    (second[0] - first[0]) * (point[1] - first[1]) / (second[1] - first[1])
                    + first[0]
                ):
                    inside = not inside
                previous = current
            if inside:
                return True
        return False

    def nearest_feature(
        self, *, layer_kind: str, position_m: Sequence[float]
    ) -> GeographyFeatureV2:
        point = _vector3(position_m, path=("position_m",))
        content = self._layer(layer_kind).normalized_content
        candidates: list[GeographyFeatureV2] = []
        for group in ("polylines", "polygons"):
            for feature in cast(Sequence[Mapping[str, object]], content.get(group, ())):
                vertices = tuple(
                    (float(item[0]), float(item[1]))
                    for item in cast(Sequence[Sequence[float]], feature["vertices_m"])
                )
                segments = tuple(zip(vertices, vertices[1:], strict=False))
                distance = min(
                    math.sqrt(_point_segment_distance_squared((point[0], point[1]), first, second))
                    for first, second in segments
                )
                candidates.append(GeographyFeatureV2(str(feature["id"]), layer_kind, distance))
        if not candidates:
            raise _error(
                "geography.feature_missing",
                ("layer_kind",),
                layer_kind,
                "materialized layer contains no queryable features",
                "provide a layer with typed polyline or polygon features",
            )
        return min(candidates, key=lambda item: (item.distance_m, item.id))

    def _sample(self, kind: str, position_m: Sequence[float]) -> float:
        point = _point2_runtime(position_m)
        samples = cast(
            Sequence[Mapping[str, object]], self._layer(kind).normalized_content.get("samples", ())
        )
        if not samples:
            raise _error(
                "geography.sample_missing",
                ("layer_kind",),
                kind,
                "materialized scalar layer contains no samples",
                "provide finite typed geography samples",
            )
        chosen = min(
            samples,
            key=lambda item: math.hypot(
                float(cast(Sequence[float], item["position_m"])[0]) - point[0],
                float(cast(Sequence[float], item["position_m"])[1]) - point[1],
            ),
        )
        return float(cast(float | int, chosen["value_m"]))

    def terrain_height_m(self, position_m: Sequence[float]) -> float:
        return self._sample("terrain_height", position_m)

    def bathymetry_m(self, position_m: Sequence[float]) -> float:
        return self._sample("bathymetry", position_m)

    def surface_elevation_m(self, position_m: Sequence[float]) -> float:
        """Return an explicitly declared local ground or sea reference height.

        AGL consumers must call this boundary instead of treating a local Z or
        a sensor-relative altitude as ground-relative height.
        """

        point = _vector3(position_m, path=("position_m",))
        if (
            self.surface_elevation_reference == "local_sea_level"
            and self.surface_elevation_constant_m is not None
        ):
            return max((self.surface_elevation_constant_m, *(r.elevation_m
                for r in self.surface_height_regions if r.contains(point))))
        raise _error(
            "geography.surface_elevation_unavailable",
            ("surface_elevation",),
            None,
            "the resolved map does not declare a usable ground or sea elevation source",
            "add an explicit surface_elevation declaration before using an AGL sensor profile",
        )

    def visibility_blocker(self, start_m: Vector3V2, end_m: Vector3V2) -> str | None:
        """Return immutable map evidence; no implicit wall at a 2D shoreline."""
        start = _vector3(start_m, path=("visibility", "start_m"))
        end = _vector3(end_m, path=("visibility", "end_m"))
        return next((r.region_id for r in self.surface_height_regions
                     if r.blocks(start, end, tolerance_m=self.tolerance_m)), None)

    def first_terrain_contact(
        self, start_m: Vector3V2, end_m: Vector3V2
    ) -> tuple[float, str] | None:
        """Locate the first penetration of declared terrain by a point segment."""
        start = _vector3(start_m, path=("terrain_contact", "start_m"))
        end = _vector3(end_m, path=("terrain_contact", "end_m"))
        # A point on a roof can move away from it. Entering its interior blocks
        # the segment, including when both endpoints are outside a plateau.
        offset = 2.0 * self.tolerance_m
        shifted_start = (start[0], start[1], start[2] + offset)
        shifted_end = (end[0], end[1], end[2] + offset)
        region = self.visibility_blocker(shifted_start, shifted_end)
        if region is None:
            return None
        lower, upper = 0.0, 1.0
        for _ in range(48):
            middle = (lower + upper) / 2.0
            point = tuple(shifted_start[i] + middle * (shifted_end[i] - shifted_start[i]) for i in range(3))
            blocker = self.visibility_blocker(shifted_start, point)
            if blocker is None:
                lower = middle
            else:
                upper, region = middle, blocker
        return upper, region

    @property
    def static_obstacles(self) -> tuple[object, ...]:
        from openmdbench.world.boundary_v2 import StaticObstacleV2

        layers = tuple(item for item in self.layers if item.kind == "static_obstacles")
        if not layers:
            return ()
        obstacles: list[StaticObstacleV2] = []
        for item in cast(
            Sequence[Mapping[str, object]], layers[0].normalized_content.get("obstacles", ())
        ):
            if item.get("shape") != "sphere":
                raise _error(
                    "geography.static_obstacle_unsupported",
                    ("layers", "static_obstacles", str(item.get("id"))),
                    item.get("shape"),
                    "static geography obstacle shape is unsupported",
                    "use a typed sphere static obstacle",
                )
            center = cast(Sequence[float], item["center_m"])
            obstacles.append(
                StaticObstacleV2.circle(
                    str(item["id"]), center, float(cast(float | int, item["radius_m"]))
                )
            )
        return tuple(sorted(obstacles, key=lambda item: item.obstacle_id))

    @classmethod
    def from_resolved_world(cls, world: _ResolvedWorldLike) -> Self:
        """Build solely from the compiler's frozen resolved-world artifact."""

        frame = getattr(world, "coordinate_frame", None)
        transform = getattr(world, "map_transform", None)
        if frame is None or not isinstance(transform, Mapping):
            raise _error(
                "geography.resolved_world_invalid",
                ("world", "coordinate_frame"),
                None,
                "resolved world does not contain complete coordinate metadata",
                "compile a v2 world with coordinate_frame and map_transform",
            )
        if (
            frame.canonical_frame != "local_m"
            or frame.axis_orientation != "east_north_up"
            or frame.height_semantics != "metres_above_origin"
            or transform.get("axis_orientation") != "east_north_up"
        ):
            raise _error(
                "geography.resolved_world_invalid",
                ("world", "coordinate_frame"),
                frame,
                "resolved coordinate axes or height semantics are unsupported",
                "use canonical local metres with east-north-up axes",
            )
        return cls(
            origin_wgs84=cast(Vector3V2, tuple(frame.origin_wgs84)),
            local_bounds_m=cast(
                tuple[Vector3V2, Vector3V2], tuple(tuple(item) for item in frame.local_bounds_m)
            ),
            map_resolution_m_per_unit=cast(
                Vector3V2, tuple(cast(Sequence[float], transform["resolution_m_per_unit"]))
            ),
            map_origin_local_m=cast(
                Vector3V2, tuple(cast(Sequence[float], transform["map_origin_local_m"]))
            ),
            map_bounds=cast(
                tuple[Vector3V2, Vector3V2],
                tuple(
                    tuple(item) for item in cast(Sequence[Sequence[float]], transform["map_bounds"])
                ),
            ),
            tolerance_m=frame.tolerance_m,
            spatial_zones=tuple(
                GeographyZoneV2(
                    zone_id=zone.id,
                    geometry_type=cast(Literal["polygon", "circle"], zone.geometry.type),
                    positions_m=tuple(
                        (float(point[0]), float(point[1])) for point in zone.geometry.positions_m
                    ),
                    center_m=(
                        None
                        if zone.geometry.center_m is None
                        else (
                            float(zone.geometry.center_m[0]),
                            float(zone.geometry.center_m[1]),
                        )
                    ),
                    radius_m=zone.geometry.radius_m,
                    domains=tuple(zone.domains),
                    deployment_excluded=(zone.id in set(world.deployment_excluded_zone_ids)),
                )
                for zone in world.zones
            ),
        )

    @classmethod
    def from_resolved_scenario(cls, resolved: _ResolvedScenarioLike) -> Self:
        """Materialize runtime geography solely from an integrity-validated artifact."""

        try:
            resolved.validate_integrity()
            service = cls.from_resolved_world(resolved.world)
            map_binding = next(
                (
                    candidate
                    for candidate in getattr(resolved, "world_resource_bindings", ())
                    if getattr(candidate, "resource_type", None) == "maps"
                    and getattr(candidate, "exact_ref", None)
                    == getattr(resolved.world, "map_ref", None)
                ),
                None,
            )
            surface_reference: Literal["local_sea_level"] | None = None
            surface_elevation_m: float | None = None
            surface_regions: tuple[SurfaceHeightRegionV2, ...] = ()
            if map_binding is not None:
                map_content = getattr(map_binding, "normalized_content", None)
                profile = (
                    None
                    if not isinstance(map_content, Mapping)
                    else map_content.get("surface_elevation")
                )
                if profile is not None:
                    if (
                        not isinstance(profile, Mapping)
                        or not {"reference", "elevation_m"} <= set(profile)
                        or set(profile) - {"reference", "elevation_m", "regions"}
                        or profile.get("reference") != "local_sea_level"
                    ):
                        raise _error(
                            "geography.surface_elevation_invalid",
                            ("world", "map_ref", "surface_elevation"),
                            profile,
                            (
                                "map surface elevation must explicitly declare "
                                "local_sea_level and elevation_m"
                            ),
                            "use {reference: local_sea_level, elevation_m: <finite metres>}",
                        )
                    value = profile.get("elevation_m")
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                    ):
                        raise _error(
                            "geography.surface_elevation_invalid",
                            ("world", "map_ref", "surface_elevation", "elevation_m"),
                            value,
                            "map surface elevation must be finite",
                            "provide a finite elevation in local metres",
                        )
                    surface_reference = "local_sea_level"
                    surface_elevation_m = float(value)
                    raw_regions = profile.get("regions", ())
                    if (not isinstance(raw_regions, Sequence)
                            or isinstance(raw_regions, (str, bytes)) or len(raw_regions) > 100):
                        raise ValueError("surface regions must be a bounded sequence")
                    regions = []
                    for region in raw_regions:
                        if not isinstance(region, Mapping) or set(region) != {"id", "vertices_m", "elevation_m"}:
                            raise ValueError("surface region fields are invalid")
                        regions.append(SurfaceHeightRegionV2(
                            region_id=region["id"], vertices_m=region["vertices_m"],
                            elevation_m=region["elevation_m"],
                        ))
                    surface_regions = tuple(regions)
            candidates: dict[str, object] = {}
            for entity in resolved.entities:
                groups = getattr(entity, "resource_bindings", {})
                for binding in groups.get("environments", ()):
                    candidates[str(binding.exact_ref)] = binding
            for event in resolved.events:
                payload = getattr(event, "payload", None)
                binding = getattr(payload, "environment_binding", None)
                if binding is not None:
                    candidates[str(binding.exact_ref)] = binding
            layers: list[GeographyLayerV2] = []
            for candidate in candidates.values():
                binding = cast(_ResolvedLayerBindingLike, candidate)
                content = binding.normalized_content
                kind = content.get("layer_kind") if isinstance(content, Mapping) else None
                if kind not in _LAYER_KINDS:
                    continue
                layers.append(
                    GeographyLayerV2.from_binding(
                        GeographyLayerBindingV2.from_resolved_binding(binding)
                    )
                )
            return cls(
                origin_wgs84=service.origin_wgs84,
                local_bounds_m=service.local_bounds_m,
                map_resolution_m_per_unit=service.map_resolution_m_per_unit,
                map_origin_local_m=service.map_origin_local_m,
                map_bounds=service.map_bounds,
                tolerance_m=service.tolerance_m,
                spatial_zones=service.spatial_zones,
                layers=tuple(layers),
                surface_elevation_reference=surface_reference,
                surface_elevation_constant_m=surface_elevation_m,
                surface_height_regions=surface_regions,
            )
        except GeographyErrorV2:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise _error(
                "geography.resolved_integrity_invalid",
                ("resolved",),
                type(error).__name__,
                "resolved scenario failed geography materialization integrity",
                "use the original integrity-validated ResolvedScenarioV2",
            ) from error

    def _validate_frame(self, frame: str) -> CoordinateFrameNameV2:
        if frame not in _SUPPORTED_FRAMES:
            raise _error(
                "geography.frame_unknown",
                ("frame",),
                frame,
                "coordinate frame is not supported",
                "use local_m, wgs84, or map",
            )
        return cast(CoordinateFrameNameV2, frame)

    def _require_within(
        self,
        coordinates: Vector3V2,
        bounds: tuple[Vector3V2, Vector3V2],
        *,
        path: Sequence[str],
        code: str,
    ) -> None:
        lower, upper = bounds
        if any(
            coordinates[index] < lower[index] - self.tolerance_m
            or coordinates[index] > upper[index] + self.tolerance_m
            for index in range(3)
        ):
            raise _error(
                code,
                path,
                coordinates,
                "coordinate is outside the declared frame bounds",
                "place the coordinate within the resolved world bounds",
            )

    def to_local(self, *, frame: str, coordinates: Sequence[float]) -> Vector3V2:
        """Convert a finite supported-frame coordinate to canonical local metres."""

        source_frame = self._validate_frame(frame)
        source = _vector3(coordinates, path=("coordinates",))
        if source_frame == "local_m":
            local = source
        elif source_frame == "map":
            self._require_within(
                source,
                self.map_bounds,
                path=("coordinates",),
                code="geography.map_out_of_bounds",
            )
            local = cast(
                Vector3V2,
                tuple(
                    self.map_origin_local_m[index]
                    + source[index] * self.map_resolution_m_per_unit[index]
                    for index in range(3)
                ),
            )
        else:
            if not -90.0 <= source[1] <= 90.0:
                raise _error(
                    "geography.wgs84_out_of_bounds",
                    ("coordinates",),
                    source,
                    "longitude or latitude is outside the WGS84 range",
                    "use longitude [-180, 180] and latitude [-90, 90]",
                )
            longitude_delta = (source[0] - self.origin_wgs84[0] + 180.0) % 360.0 - 180.0
            local = (
                math.radians(longitude_delta)
                * _EARTH_RADIUS_M
                * math.cos(math.radians(self.origin_wgs84[1])),
                math.radians(source[1] - self.origin_wgs84[1]) * _EARTH_RADIUS_M,
                source[2] - self.origin_wgs84[2],
            )
        self._require_within(
            local,
            self.local_bounds_m,
            path=("coordinates",),
            code="geography.local_out_of_bounds",
        )
        return local

    def from_local(self, *, frame: str, coordinates_m: Sequence[float]) -> Vector3V2:
        """Convert canonical local metres to one supported external frame."""

        target_frame = self._validate_frame(frame)
        local = _vector3(coordinates_m, path=("coordinates_m",))
        self._require_within(
            local,
            self.local_bounds_m,
            path=("coordinates_m",),
            code="geography.local_out_of_bounds",
        )
        if target_frame == "local_m":
            return local
        if target_frame == "map":
            mapped = cast(
                Vector3V2,
                tuple(
                    (local[index] - self.map_origin_local_m[index])
                    / self.map_resolution_m_per_unit[index]
                    for index in range(3)
                ),
            )
            self._require_within(
                mapped,
                self.map_bounds,
                path=("coordinates_m",),
                code="geography.map_out_of_bounds",
            )
            return mapped
        longitude_scale = _EARTH_RADIUS_M * math.cos(math.radians(self.origin_wgs84[1]))
        longitude = self.origin_wgs84[0] + math.degrees(local[0] / longitude_scale)
        longitude = (longitude + 180.0) % 360.0 - 180.0
        return (
            0.0 if longitude == 0.0 else longitude,
            self.origin_wgs84[1] + math.degrees(local[1] / _EARTH_RADIUS_M),
            self.origin_wgs84[2] + local[2],
        )

    def canonical_local(self, coordinates_m: Sequence[float]) -> Vector3V2:
        """Quantize local metres once and canonicalize all signed zero values."""

        local = _vector3(coordinates_m, path=("coordinates_m",))
        return cast(
            Vector3V2,
            tuple(
                0.0
                if (rounded := round(axis / self.tolerance_m) * self.tolerance_m) == 0.0
                else rounded
                for axis in local
            ),
        )

    def convert(self, coordinate: CoordinateV2, *, frame: str) -> CoordinateV2:
        target_frame = self._validate_frame(frame)
        local = self.to_local(frame=coordinate.frame, coordinates=coordinate.coordinates)
        return CoordinateV2(target_frame, self.from_local(frame=target_frame, coordinates_m=local))

    @staticmethod
    def distance_m(first_m: Sequence[float], second_m: Sequence[float]) -> float:
        first = _vector3(first_m, path=("first_m",))
        second = _vector3(second_m, path=("second_m",))
        return math.sqrt(sum((second[index] - first[index]) ** 2 for index in range(3)))

    @staticmethod
    def heading_deg(first_m: Sequence[float], second_m: Sequence[float]) -> float:
        first = _vector3(first_m, path=("first_m",))
        second = _vector3(second_m, path=("second_m",))
        east = second[0] - first[0]
        north = second[1] - first[1]
        if east == 0.0 and north == 0.0:
            return 0.0
        return math.degrees(math.atan2(east, north)) % 360.0

    def distance_between(
        self,
        *,
        frame: str,
        first: Sequence[float],
        second: Sequence[float],
    ) -> float:
        return self.distance_m(
            self.to_local(frame=frame, coordinates=first),
            self.to_local(frame=frame, coordinates=second),
        )


__all__ = [
    "CoordinateFrameNameV2",
    "CoordinateV2",
    "GeographyErrorV2",
    "GeographyLayerBindingV2",
    "GeographyLayerLimitsV2",
    "GeographyLayerV2",
    "GeographyFeatureV2",
    "GeographyServiceV2",
    "GeographyZoneV2",
    "Vector3V2",
]
