"""Opt-in return-point settling from owned measurements, without target truth."""
import math


class SurfaceArrival:
    def __init__(self, config):
        self.config=dict(config)
        required=set(('capture_radius_m settled_radius_m release_radius_m capture_speed_mps '
            'settled_speed_mps settled_yaw_rate_deg_s stable_ticks steering_speed_mps braking_margin_m '
            'command_delay_ticks braking_drag_per_m speed_hysteresis_mps turn_tolerance_deg '
            'tracking_yaw_rate_deg_s yaw_lead_ticks heading_correction_deg linear_drag_per_s '
            'low_speed_damping_mps maximum_turning_speed_mps control_radius_m').split())
        if set(self.config)!=required:
            raise ValueError('surface arrival needs the complete explicit control profile')
        if (any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in self.config.values())
                or not self.config['capture_radius_m']<self.config['settled_radius_m']<self.config['release_radius_m']
                or self.config['control_radius_m']<self.config['release_radius_m']
                or not self.config['settled_speed_mps']<self.config['capture_speed_mps']<self.config['low_speed_damping_mps']
                or self.config['stable_ticks']!=int(self.config['stable_ticks'])):
            raise ValueError('invalid surface arrival control')
        self.states={};self.history={};self.diagnostics={}

    def should_control(self,own,goal,cruise_speed):
        """Keep original transit navigation outside the stopping preview.

        Once local arrival has started, retain it through a possible overshoot;
        changing back to the transit controller would recreate the on/off loop.
        """
        p=self.config;speed=max(math.hypot(*own['velocity_mps'][:2]),cruise_speed)
        braking=(self.coast_distance(speed)+speed*p['command_delay_ticks']
                 +p['braking_margin_m']+p['capture_radius_m'])
        captured=self.states.get(own['entity_id'],{}).get('goal')==tuple(goal[:2])
        return captured or math.dist(own['position_m'][:2],goal[:2])<=max(p['control_radius_m'],braking)

    def coast_distance(self,speed):
        """Integrate dv/dt=-k*v**2, plus the published low-speed viscous term.

        k is calibrated against zero-thrust native MMG, not a forced speed cap.
        Actual native speed is checked again on every authorized observation.
        """
        p=self.config;k=p['braking_drag_per_m'];c=p['linear_drag_per_s']
        low=p['low_speed_damping_mps'];stop=p['settled_speed_mps']
        slow=math.log((c+k*max(stop,min(speed,low)))/(c+k*stop))/k
        return slow+math.log(max(1.,speed/low))/k

    def supported_speed(self,distance):
        p=self.config;k=p['braking_drag_per_m'];c=p['linear_drag_per_s']
        low=p['low_speed_damping_mps'];stop=p['settled_speed_mps']
        threshold=self.coast_distance(low)
        if distance<=threshold:return ((c+k*stop)*math.exp(k*distance)-c)/k
        return low*math.exp(min(40.,k*(distance-threshold)))

    def command(self, own, goal, waypoint, cruise_speed, tick):
        p=self.config;entity=own['entity_id'];position=own['position_m']
        heading=own['heading_deg'];speed=math.hypot(*own['velocity_mps'][:2])
        previous=self.history.get(entity)
        elapsed=tick-previous[0] if previous else 0
        yaw=(((heading-previous[1]+180.)%360.-180.)/elapsed if elapsed>0 else 0.)
        self.history[entity]=(tick,heading)
        key=tuple(goal[:2]);state=self.states.get(entity)
        if state is None or state['goal']!=key:
            state=dict(goal=key,phase='approach',stable_ticks=0.)
            self.states[entity]=state
        distance=math.dist(position[:2],key)
        release=(p['release_radius_m'] if state['phase']=='hold' else p['settled_radius_m'])
        if state['phase'] in {'settling','hold'} and distance>release:
            state.update(phase='approach',stable_ticks=0.)
        coast=self.coast_distance(speed)
        predicted=[position[i]+own['velocity_mps'][i]*coast/speed for i in (0,1)] if speed else position[:2]
        if (state['phase']=='approach' and distance<=p['capture_radius_m']
                and speed<=p['capture_speed_mps']
                and math.dist(predicted,key)<=p['settled_radius_m']):
            state.update(phase='settling',hold_heading=heading,stable_ticks=0.)
        # A missing observation cannot count as continuously verified stability.
        stable=(distance<=p['settled_radius_m'] and speed<=p['settled_speed_mps']
                and abs(yaw)<=p['settled_yaw_rate_deg_s'] and elapsed==1)
        if state['phase'] in {'settling','hold'}:
            state['stable_ticks']=state['stable_ticks']+elapsed if stable else 0.
            if state['stable_ticks']>=p['stable_ticks']:state['phase']='hold'
            target=state['hold_heading']
            command_speed=0.
            # Captured motion dissipates at zero thrust. A steering pulse here
            # would add surge while turning and drive a slow hull out again.
        else:
            target=math.degrees(math.atan2(waypoint[0]-position[0],waypoint[1]-position[1]))%360.
            available=max(0.,distance-p['capture_radius_m']-p['braking_margin_m']-speed*p['command_delay_ticks'])
            cap=self.supported_speed(available)
            command_speed=min(cruise_speed,cap)
            if speed>cap+p['speed_hysteresis_mps']:command_speed=0.
            error=abs((target-heading+180.)%360.-180.)
            if error>p['turn_tolerance_deg'] or abs(yaw)>p['tracking_yaw_rate_deg_s']:
                turn_speed=p['steering_speed_mps']
                # Keep small corrections gentle; use the existing navigation
                # 25-degree large-turn threshold only in the far approach.
                # Increasing every small correction excites the MMG yaw loop.
                if error<=25. or distance<=2.*p['release_radius_m']:
                    state['transit_turn']=False
                elif speed<=p['low_speed_damping_mps']:
                    state['transit_turn']=True
                if state.get('transit_turn'):
                    turn_speed=min(p['maximum_turning_speed_mps'],cap)
                command_speed=min(cruise_speed,turn_speed)
        correction=(target-heading+180.)%360.-180.-p['yaw_lead_ticks']*yaw
        # A bounded heading request prevents hard-over rudder reversals while
        # the measured turn still carries the hull towards its previous heading.
        target=(heading+max(-p['heading_correction_deg'],min(p['heading_correction_deg'],correction)))%360.
        braking=self.coast_distance(speed)+speed*p['command_delay_ticks']+p['braking_margin_m']
        result=dict(speed_mps=command_speed,heading_deg=target,altitude_m=position[2])
        self.diagnostics[entity]=dict(tick=tick,phase=state['phase'],goal_m=list(key),distance_m=distance,
            actual_speed_mps=speed,measured_yaw_rate_deg_s=yaw,braking_distance_m=braking,
            stable_ticks=state['stable_ticks'],parking_stable=stable,command=dict(result))
        return result
