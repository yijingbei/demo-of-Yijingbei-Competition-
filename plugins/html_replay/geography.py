"""Read the map's declared visualization layers without loading the simulation engine."""
import hashlib
import json
import math
from pathlib import Path


def load_geography(source_root, scenario_id, map_ref, *, engine_root=None):
    if not map_ref or source_root is None:
        return None
    import yaml
    root = Path(source_root).resolve()
    suite_path = root/'scenarios/suite.json'
    if not suite_path.is_file():
        return None
    suite = json.loads(suite_path.read_text(encoding='utf-8-sig'))
    entry = next((r for r in suite['scenarios'] if r['id'] == scenario_id), None)
    if entry is None:
        return None
    catalog_path = (root/'scenarios/catalog'/(entry['catalog']+'.yaml')).resolve()
    if not catalog_path.is_relative_to(root/'scenarios/catalog'):
        raise ValueError('Map catalog path escapes source root')
    catalog = yaml.safe_load(catalog_path.read_text(encoding='utf-8-sig'))
    resource = next((r for r in catalog['resources'] if r.get('resource_type') == 'maps'
                     and r['id']+'@'+r['version'] == map_ref), None)
    if resource is None:
        raise ValueError(f'Map {map_ref} absent from scenario catalog')
    return map_layers(resource, engine_root if engine_root is not None else root/'engine/source_codes')


def map_layers(resource, engine_root):
    content = resource['content']
    source = content.get('visualization_source')
    if not source:
        return terrain_layers(resource, engine_root)
    if source.get('format') != 'weihai-raw-lonlat@1.0':
        raise ValueError('Unsupported map visualization source format')
    root = Path(engine_root).resolve()
    path = (root/source['path']).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError('Map visualization source path escapes engine root')
    raw = path.read_bytes()
    digest = 'sha256:'+hashlib.sha256(raw).hexdigest()
    if digest != source['sha256']:
        raise ValueError('Map visualization source SHA-256 mismatch')
    document = json.loads(raw)
    lo, hi, end = (source[k] for k in ('coastline_start','coastline_end','harbor_end'))
    if not all(type(n) is int for n in (lo,hi,end)) or not 0 <= lo <= hi <= end <= len(document):
        raise ValueError('Invalid map visualization layer ranges')
    origin = content['origin_wgs84']
    if len(origin) != 3 or not all(math.isfinite(v) for v in origin):
        raise ValueError('Invalid map projection origin')
    def project(p):
        if len(p) < 2 or not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in p[:2]):
            raise ValueError('Invalid geographic coastline vertex')
        if not -180 <= p[0] <= 180 or not -90 <= p[1] <= 90:
            raise ValueError('Coastline outside WGS84 bounds')
        # Match GeographyServiceV2 / map_terrain_v2 exactly (east, north, metres).
        return [math.radians((p[0]-origin[0]+180)%360-180)*6378137*math.cos(math.radians(origin[1])),
                math.radians(p[1]-origin[1])*6378137]
    def line(index, kind):
        points = [project(p) for p in document[index]]
        if len(points) < 2:
            raise ValueError('Map coastline path has fewer than two vertices')
        return dict(id=f'map.{kind}.{index:03}', coordinates_m=points,
                    closed=len(points) >= 4 and points[0] == points[-1])
    coasts = [line(i,'coast') for i in range(lo,hi)]
    harbors = [line(i,'harbor') for i in range(hi,end)]
    return dict(mapRef=resource['id']+'@'+resource['version'], sourceSha256=digest,
                originWgs84=origin, coastlineLines=coasts, harborLines=harbors,
                backgroundLand=[dict(id=p['id'],coordinates_m=p['coordinates_m']) for p in coasts if p['closed']],
                openLinesPreserved=True, bathymetryAvailable=False)


def terrain_layers(resource, engine_root):
    """Snapshot declared collision land when a private map has no visual export.

    Read the exact native terrain source, never a different deployment or the
    participant navigation brief. No engine import is needed by the replay plugin.
    """
    content = resource['content']
    source = content.get('terrain_source')
    if source is None:
        return None
    if not isinstance(source, dict) or source.get('format') != 'raw-polygon-list-wgs84@1.0':
        raise ValueError('Unsupported map terrain source format')
    root = Path(engine_root).resolve()
    relative = source.get('path')
    if not isinstance(relative, str) or not relative:
        raise ValueError('Map terrain source path missing')
    path = (root/relative).resolve()
    if not path.is_relative_to(root) or path.is_symlink():
        raise ValueError('Map terrain source path escapes engine root')
    raw = path.read_bytes()
    digest = 'sha256:'+hashlib.sha256(raw).hexdigest()
    if digest != source.get('sha256'):
        raise ValueError('Map terrain source SHA-256 mismatch')
    document = json.loads(raw)
    if not isinstance(document, list) or any(not isinstance(ring, list) for ring in document):
        raise ValueError('Map terrain source geometry must be polygon rings')
    origin = content.get('origin_wgs84')
    if (not isinstance(origin, (list, tuple)) or len(origin) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) for v in origin)
            or not -180 <= origin[0] <= 180 or not -90 <= origin[1] <= 90):
        raise ValueError('Invalid map terrain projection origin')
    indexes = source.get('polygon_indices', [])
    closed_range = source.get('closed_polygon_range', [0, 0])
    if (not isinstance(indexes, (list, tuple))
            or any(type(i) is not int or not 0 <= i < len(document) for i in indexes)
            or not isinstance(closed_range, (list, tuple)) or len(closed_range) != 2
            or any(type(i) is not int for i in closed_range)
            or not 0 <= closed_range[0] <= closed_range[1] <= len(document)):
        raise ValueError('Map terrain polygon indexes invalid')
    chosen = list(indexes)+[i for i in range(*closed_range)
                           if len(document[i]) >= 4 and document[i][0] == document[i][-1]]
    if not chosen:
        raise ValueError('Map terrain source declares no land polygons')
    polygons = []
    for index in dict.fromkeys(chosen):
        ring = []
        for point in document[index]:
            if (not isinstance(point, (list, tuple)) or len(point) < 2
                    or any(type(v) not in (int, float) or not math.isfinite(v) for v in point[:2])
                    or not -180 <= point[0] <= 180 or not -90 <= point[1] <= 90):
                raise ValueError('Invalid map terrain vertex')
            ring.append((math.radians((point[0]-origin[0]+180)%360-180)*6378137
                         *math.cos(math.radians(origin[1])),
                         math.radians(point[1]-origin[1])*6378137))
        # Match native source canonicalization, including export-noise removal.
        ring = list(dict.fromkeys(ring))
        changed = True
        while changed and len(ring) >= 3:
            changed = False
            for position, current in enumerate(ring):
                previous, following = ring[position-1], ring[(position+1)%len(ring)]
                incoming = (current[0]-previous[0], current[1]-previous[1])
                outgoing = (following[0]-current[0], following[1]-current[1])
                if abs(incoming[0]*outgoing[1]-incoming[1]*outgoing[0]) > 1e-9:
                    continue
                if incoming[0]*outgoing[0]+incoming[1]*outgoing[1] < 0:
                    ring.pop(position)
                    changed = True
                    break
                raise ValueError('Map terrain polygon retraces an edge')
        if len(ring) < 3:
            raise ValueError('Map terrain polygon is degenerate')
        polygons.append(dict(id=f'map.terrain.{index:03}', coordinates_m=[list(p) for p in ring]))
    return dict(mapRef=resource['id']+'@'+resource['version'], sourceSha256=digest,
                sourceKind='native-terrain', originWgs84=origin,
                coastlineLines=[], harborLines=[], backgroundLand=polygons,
                openLinesPreserved=True, bathymetryAvailable=False)
