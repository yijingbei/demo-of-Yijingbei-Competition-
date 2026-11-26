"""Visibility routes around public coastline polygons with hull clearance."""
import heapq
import math


def _distance(point, a, b):
    delta=[b[i]-a[i] for i in (0,1)]
    square=sum(v*v for v in delta)
    fraction=max(0.,min(1.,sum((point[i]-a[i])*delta[i] for i in (0,1))/square)) if square else 0.
    return math.hypot(*(point[i]-a[i]-fraction*delta[i] for i in (0,1)))


def _inside(point, polygon):
    result=False
    for a,b in zip(polygon,polygon[1:]+polygon[:1]):
        if (a[1]>point[1])!=(b[1]>point[1]):
            crossing=(b[0]-a[0])*(point[1]-a[1])/(b[1]-a[1])+a[0]
            if point[0]<crossing:result=not result
    return result


def _intersect(a,b,c,d):
    def cross(p,q,r):
        return (q[0]-p[0])*(r[1]-p[1])-(q[1]-p[1])*(r[0]-p[0])
    return (cross(a,b,c)*cross(a,b,d)<0 and cross(c,d,a)*cross(c,d,b)<0)


class CoastalRouter:
    def __init__(self, regions, clearance=70.):
        self.polygons=[list(map(tuple,r['coordinates_m'])) for r in regions]
        self.polygon_boxes=[(min(p[0] for p in poly),min(p[1] for p in poly),max(p[0] for p in poly),max(p[1] for p in poly)) for poly in self.polygons]
        self.edges=[(a,b) for p in self.polygons for a,b in zip(p,p[1:]+p[:1])]
        self.edge_boxes=[(min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1])) for a,b in self.edges]
        self.clearance=clearance
        self.nodes=None
        self.links=None
        self.grid_points={}
        self.grid_edges={}
        self.grid_routes={}

    def clear_point(self, point):
        if any(box[0]<=point[0]<=box[2] and box[1]<=point[1]<=box[3] and _inside(point,p)
               for p,box in zip(self.polygons,self.polygon_boxes)):return False
        c=self.clearance
        return all(_distance(point,a,b)>=c for (a,b),box in zip(self.edges,self.edge_boxes)
                   if box[0]-c<=point[0]<=box[2]+c and box[1]-c<=point[1]<=box[3]+c)

    def clear_segment(self, start, goal):
        if not self.clear_point(start) or not self.clear_point(goal):return False
        lo=[min(start[i],goal[i])-self.clearance for i in (0,1)]
        hi=[max(start[i],goal[i])+self.clearance for i in (0,1)]
        for (a,b),box in zip(self.edges,self.edge_boxes):
            if box[2]<lo[0] or box[0]>hi[0] or box[3]<lo[1] or box[1]>hi[1]:continue
            if _intersect(start,goal,a,b):return False
            if min(_distance(start,a,b),_distance(goal,a,b),
                   _distance(a,start,goal),_distance(b,start,goal))<self.clearance:
                return False
        return True

    def _grid_route(self,start,goal):
        """Lazy visibility-checked A* for detailed mainland coastlines.

        The small-island visibility graph is quadratic in coastline vertices.
        A fixed public lattice caches only visited water nodes/edges, and every
        returned segment is checked against the original land geometry.
        """
        step=300.
        cell=lambda p:(round(p[0]/step),round(p[1]/step))
        point=lambda k:(k[0]*step,k[1]*step)
        key=(cell(start),goal)
        cached=self.grid_routes.get(key)
        if cached and self.clear_segment(start,cached[0]):
            return list(cached),sum(math.dist(a,b) for a,b in zip([start,*cached[:-1]],cached))
        def valid(k):
            if k not in self.grid_points:self.grid_points[k]=self.clear_point(point(k))
            return self.grid_points[k]
        starts=[]
        origin=cell(start)
        for dx in range(-2,3):
            for dy in range(-2,3):
                k=(origin[0]+dx,origin[1]+dy)
                if valid(k) and self.clear_segment(start,point(k)):starts.append(k)
        costs={k:math.dist(start,point(k)) for k in starts};previous={k:None for k in starts}
        queue=[(v+math.dist(point(k),goal),v,k) for k,v in costs.items()];heapq.heapify(queue)
        # Bounded search cannot claim an unsafe fallback. Native geography
        # remains authoritative if a controller cannot find a route.
        visited=0;found=None
        while queue and visited<12000:
            _,cost,k=heapq.heappop(queue)
            if cost!=costs[k]:continue
            visited+=1;p=point(k)
            if math.dist(p,goal)<=step*2 and self.clear_segment(p,goal):found=k;break
            for dx,dy in ((1,0),(-1,0),(0,1),(0,-1),(1,1),(1,-1),(-1,1),(-1,-1)):
                n=(k[0]+dx,k[1]+dy);q=point(n)
                if math.dist(q,start)+math.dist(q,goal)>math.dist(start,goal)+20000.:continue
                if not valid(n):continue
                edge=tuple(sorted((k,n)))
                if edge not in self.grid_edges:self.grid_edges[edge]=self.clear_segment(p,q)
                if not self.grid_edges[edge]:continue
                value=cost+math.dist(p,q)
                if value>=costs.get(n,math.inf):continue
                costs[n]=value;previous[n]=k
                heapq.heappush(queue,(value+math.dist(q,goal),value,n))
        if found is None:return [],math.inf
        raw=[goal]
        while found is not None:raw.append(point(found));found=previous[found]
        raw.reverse();route=[];p=start
        while raw:
            index=next((i for i in range(len(raw)-1,-1,-1) if self.clear_segment(p,raw[i])),None)
            if index is None:return [],math.inf
            p=raw[index];route.append(p);raw=raw[index+1:]
        self.grid_routes[key]=tuple(route)
        if len(self.grid_routes)>512:self.grid_routes.pop(next(iter(self.grid_routes)))
        return route,sum(math.dist(a,b) for a,b in zip([start,*route[:-1]],route))

    def _build(self):
        # Cache the coast-only graph; per-tick work connects just start and goal.
        points=set()
        radius=2*self.clearance+30.
        for polygon in self.polygons:
            for vertex in polygon:
                for n in range(12):
                    angle=n*math.pi/6
                    point=(vertex[0]+radius*math.cos(angle),vertex[1]+radius*math.sin(angle))
                    if self.clear_point(point):points.add(point)
        self.nodes=sorted(points)
        self.links=[[] for _ in self.nodes]
        for i,a in enumerate(self.nodes):
            for j in range(i):
                b=self.nodes[j]
                if self.clear_segment(a,b):
                    cost=math.dist(a,b)
                    self.links[i].append((j,cost));self.links[j].append((i,cost))

    def route(self, start, goal):
        start,goal=tuple(start[:2]),tuple(goal[:2])
        if self.clear_segment(start,goal):return [goal],math.dist(start,goal)
        if not self.clear_point(start) or not self.clear_point(goal):return [],math.inf
        if len(self.edges)>80:return self._grid_route(start,goal)
        if self.nodes is None:self._build()
        points=[*self.nodes,start,goal]
        links=[list(v) for v in self.links]+[[],[]]
        first,last=len(points)-2,len(points)-1
        for index in (first,last):
            for j in range(len(self.nodes)):
                if self.clear_segment(points[index],points[j]):
                    cost=math.dist(points[index],points[j])
                    links[index].append((j,cost));links[j].append((index,cost))
        costs={first:0.};previous={};queue=[(0.,first)]
        while queue:
            cost,index=heapq.heappop(queue)
            if cost!=costs[index]:continue
            if index==last:
                route=[goal]
                while index!=first:
                    index=previous[index]
                    if index!=first:route.append(points[index])
                return list(reversed(route)),cost
            for j,step in links[index]:
                candidate=cost+step
                if candidate<costs.get(j,math.inf):
                    costs[j]=candidate;previous[j]=index
                    heapq.heappush(queue,(candidate,j))
        return [],math.inf
