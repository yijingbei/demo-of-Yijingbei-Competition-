"""Public-geometry navigation helpers; no engine or referee inputs."""
import heapq
import math
from .legacy_plugins.response_policy import crosses


def safe_route(start, goal, hazards, margin=45.):
    """Shortest visible path around conservative expanded polygon boxes.

    There is no assumption that all obstacles share a lower detour. Returning
    no route is preferable to claiming an unsafe line is navigable.
    """
    boxes, corners = [], []
    for region in hazards:
        points = region['coordinates_m']
        lo = [min(p[a] for p in points)-margin for a in (0, 1)]
        hi = [max(p[a] for p in points)+margin for a in (0, 1)]
        boxes.append({'coordinates_m': [[lo[0],lo[1]],[hi[0],lo[1]],
                                          [hi[0],hi[1]],[lo[0],hi[1]]]})
        # Extra clearance makes tangent edges unambiguous under inclusive tests.
        corners.extend([(lo[0]-35,lo[1]-35),(hi[0]+35,lo[1]-35),
                        (hi[0]+35,hi[1]+35),(lo[0]-35,hi[1]+35)])
    nodes = [tuple(start[:2]), tuple(goal[:2]), *corners]
    queue, distance, previous = [(0.,0)], {0:0.}, {}
    while queue:
        cost, index = heapq.heappop(queue)
        if cost != distance[index]:
            continue
        if index == 1:
            route = [nodes[1]]
            while index != 0:
                index = previous[index]
                if index != 0:
                    route.append(nodes[index])
            return list(reversed(route)), cost
        for j, point in enumerate(nodes):
            if j == index or any(crosses(nodes[index], point, box) for box in boxes):
                continue
            candidate = cost + math.dist(nodes[index], point)
            if candidate < distance.get(j, math.inf):
                distance[j], previous[j] = candidate, index
                heapq.heappush(queue,(candidate,j))
    return [], math.inf


def navigate(own, goal, maximum_speed, altitude=None, arrival_radius=12.):
    point = own['position_m']
    distance = math.dist(point[:2], goal[:2])
    heading = math.degrees(math.atan2(goal[0]-point[0], goal[1]-point[1])) % 360
    error = (heading-own.get('heading_deg',heading)+180) % 360-180
    # Brake before sharp turns; the native engine still owns actual motion.
    speed = 0. if distance <= arrival_radius or abs(error)>25 else min(maximum_speed,distance/3.)
    payload = {'heading_deg':heading,'speed_mps':speed}
    if altitude is not None:
        payload['altitude_m'] = altitude
    return payload
