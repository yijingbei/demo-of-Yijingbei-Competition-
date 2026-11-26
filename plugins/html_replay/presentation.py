"""Display-only theme and recorded strategy declarations; never alter evidence."""
import hashlib
import json

TERMS = {
    '自杀艇战斗部':'船艇碰撞组件', '空空拦截弹':'空中拦阻道具', '对海导弹':'水面拦阻道具',
    '制导导弹':'追踪道具', '巡飞弹':'巡航道具', '武装威胁':'待处置目标',
    '威胁中和':'目标处置', '岸防指挥所':'岸上调度中心', '指挥所':'调度中心',
    '拦截交战':'海警缉私', '作战单元':'执法单元', '作战':'执法', '导弹':'拦阻道具',
    '武器':'处置装备', '弹药':'道具余量', '交战':'互动', '交火':'互动', '战损':'受损',
    '毁伤':'损伤', '摧毁':'失效', '自爆':'碰撞触发', '自杀':'碰撞触发', '战斗':'互动',
    '战区':'任务区', '空战':'空中互动', '海战':'水面互动', '敌情':'目标信息',
    '敌人':'对方单位', '敌方':'对方', '威胁':'目标', '开火':'投放', '射击':'处置',
    '发射':'投放', '射程':'作用距离', '射距':'作用距离', '弹道':'道具轨迹',
    '弹种':'道具类型', '进攻':'行动', '攻击':'干扰', '防御':'警戒', '侦察':'巡查',
    '打击':'处置', '歼灭':'处置', '杀伤':'损伤', '战术':'行为规则',
    '战场':'任务区域', '击毁':'失效', '击落':'停用', '弹头':'作用组件',
    '拦截弹':'拦阻道具', '火控':'处置控制', '防空':'空中管控', '兵力':'单位配置',
}
TERMS.update({'岸防指挥所': '岸上调度中心', '岸防': '岸上管控', '指挥中心': '调度中心', '指挥所': '调度中心', '指挥部': '调度中心', '指挥官': '调度员', '指挥': '调度', '相控阵雷达': '阵列感知设备', '火控雷达': '处置感知设备', '雷达站': '监测站', '雷达': '感知设备', '防空导弹': '空中拦阻道具', '反舰导弹': '水面拦阻道具', '地空导弹': '空中拦阻道具', '巡航导弹': '巡航拦阻道具', '弹药库': '物资库', '航空母舰': '大型保障船', '驱逐舰': '大型巡逻船', '护卫舰': '巡逻船', '导弹艇': '巡逻艇', '军舰': '巡逻船', '舰艇': '船艇', '舰载': '船载', '舰': '船', '歼击机': '航空器', '歼轰机': '航空器', '战斗机': '航空器', '轰炸机': '航空器', '攻击机': '航空器', '预警机': '监测航空器', '空军': '空中巡查队', '海军': '海上巡查队', '陆军': '岸上巡查队', '军用': '专用', '军事': '海事', '军队': '巡查队', '军方': '管理方', '部队': '队伍', '兵种': '单位类型', '兵力': '单位配置', '战略': '整体方案', '战术': '行为规则', '战备': '值守', '战斗部': '作用组件', '战斗群': '协同组', '战机': '航空器', '战舰': '巡逻船', '武装': '配置装备', '无武装': '未配置处置装备', '武力': '处置能力', '火力': '处置能力', '火控': '处置控制', '火箭': '拦阻道具', '火炮': '处置设备', '机炮': '处置设备', '炮台': '处置站', '炮弹': '拦阻道具', '炮': '处置设备', '鱼雷': '水下拦阻道具', '水雷': '水域警示装置', '地雷': '区域警示装置', '炸弹': '干扰道具', '炸药': '作用材料', '弹药': '道具余量', '弹舱': '道具仓', '弹丸': '拦阻道具', '弹体': '道具本体', '弹库': '物资库', '弹幕': '密集道具轨迹', '节弹': '节省道具', '实弹': '实体道具', '弹': '道具', '自杀艇': '高速目标艇', '自爆艇': '高速目标艇', '爆炸': '碰撞触发效果', '引爆': '触发作用', '爆破': '场景触发', '爆轰': '触发作用', '爆': '触发效果', '摧毁': '失效', '击沉': '停用', '击杀': '停用', '杀伤': '损伤', '歼灭': '处置', '轰炸': '集中干扰', '轰击': '干扰', '击毙': '停用', '阵亡': '停用', '偷袭': '绕行接近', '突袭': '快速接近', '空袭': '空中接近', '突击': '集中接近', '突防': '越界通行', '佯攻': '诱导行动', '主攻': '主要行动', '攻势': '行动', '进攻': '行动', '攻击': '干扰', '攻防': '管控与通行', '防守': '管控', '防御': '警戒', '防线': '管控线', '防区': '管控区', '敌我': '双方', '敌对': '对方', '敌载体': '目标载体', '敌机': '对方航空器', '敌艇': '对方船艇', '敌舰': '对方船艇', '敌': '对方', '战场': '任务区域', '战区': '任务区', '战损': '受损', '战况': '任务进展', '战役': '任务', '战争': '海事任务', '战': '任务', '前线': '外围区域', '阵地': '执勤点', '驻军': '执勤队', '营地': '值守点', '大本营': '核心设施', '基地': '保障站', '封锁': '区域管控', '制空': '空域管控', '制海': '水域管控', '接敌': '接近目标', '交火': '互动', '交战': '互动', '交锋': '互动', '齐射': '集中投放', '连射': '连续投放', '扫射': '连续处置', '射速': '投放速率', '射界': '作用范围', '射手': '操作单元', '射击': '处置', '饱和': '密集', '伏击': '隐蔽等待', '伏兵': '等待单元', '兵': '单位', '侵袭': '进入', '入侵': '越界', '轰': '集中干扰'})
DISPLAY_PATTERNS = [('(?i)command[ _-]?post', 'coordination-center'), ('(?i)combined[ _-]?arms', 'multi-domain'), ('(?i)surface[ _-]?raid', 'surface-control'), ('(?i)deep[ _-]?strike', 'dual-area-control'), ('(?i)island[ _-]?strike', 'island-control'), ('(?i)dual[ _-]?axis[ _-]?pincer', 'two-axis-control'), ('(?i)(?<![a-z])(?:radar|sam|aam|asm|ciws)(?![a-z])', 'sensor-device'), ('(?i)(?<![a-z])(?:headquarters|hq|arsenal)(?![a-z])', 'coordination-center'), ('(?i)(?<![a-z])(?:missiles?|warheads?|weapons?|munitions?|ammo|ammunition|torpedoes?|bombs?|cannons?|artillery)(?![a-z])', 'response-item'), ('(?i)(?<![a-z])(?:suicide|kamikaze|military|combat|warfare|battle|strike|raid|attack|attacker|threat|saturation|destroyer|frigate|fighter|bomber)(?![a-z])', 'patrol-event')]


def display_text(text, english=True):
    import re
    text = str(text)
    for old in sorted(TERMS, key=len, reverse=True):
        text = text.replace(old, TERMS[old])
    if english:
        for pattern, replacement in DISPLAY_PATTERNS:
            text = re.sub(pattern, replacement, text)
    return text

FIELDS = ('kind', 'mode', 'driver', 'difficulty', 'large_model', 'small_model')
BASELINES = {'idle', 'naive', 'coordinated', 'greedy', 'adaptive'}

def terminal_reason(result):
    if result.get('status') != 'completed':
        return '对局未自然完成：' + str(result.get('error') or result.get('status'))
    terminal = result.get('terminal') or {}
    rule = terminal.get('rule_id')
    if rule == 'application.engagement-compliance':
        audit = result.get('engagement_audit', {})
        native = result.get('native_terminal', {}).get('outcome')
        return (f"违规攻击请求 {audit.get('violation_request_count', 0)} 次，涉及 "
                f"{len(audit.get('violated_entity_ids', []))} 个真实实体；原生防御"
                + ('已完成' if native == 'objective_complete' else '未完成'))
    if result.get('task_success') is True:
        if result.get('engagement_audit',{}).get('schema_version')=='autonomous-engagement-audit@2':
            return '原生IE防御成功：真实威胁全部解除、无突破、设施达到门槛且结算完成；误射仅作诊断。'
        return '原IE防御条件达成，且没有违规攻击决策。'
    if rule == 'rule.assets-lost':
        return '保护设施全部失守。'
    if rule == 'rule.timeout':
        metrics = {m['metric_id']:m['value'] for m in result.get('score_audit', {}).get('metrics', [])}
        return (f"截止时防御条件未达成：真实威胁中和比例 {metrics.get('metric.neutralization', '未记录')}，"
                f"设施完好比例 {metrics.get('metric.facilities', '未记录')}。")
    return '原生裁判判负：' + str(rule or terminal.get('outcome', '未记录'))


def strategy_metadata(folder, result, scene, brief, participant_side, opponent=None, opponent_source=None):
    """Unknown fields stay unknown. A declaration is not inference-call evidence."""
    sides = {s:dict(kind='unknown', mode='unknown', difficulty='unknown',
                    driver='unknown', source='not_recorded') for s in ('red','blue')}
    if isinstance(opponent,dict):
        sides['blue'].update(kind='script', mode=str(opponent.get('kind','unknown')),
                             driver=str(opponent.get('kind','unknown')), source=opponent_source)
        difficulty=opponent.get('difficulty', (opponent.get('attack') or {}).get('difficulty'))
        if isinstance(difficulty,str):
            sides['blue']['difficulty']=difficulty
    policy = result.get('policy')
    if isinstance(policy, str) and policy:
        sides[participant_side].update(mode=policy,
            kind='rule' if policy in BASELINES else 'unknown', source='result.policy')
    slots = scene.get('controller_slots', [])
    if isinstance(slots, list):
        sides['blue']['controller_count'] = sum(
            isinstance(s,dict) and str(s.get('controller_id','')).startswith(('agent.blue.', 'agent.intruder.'))
            for s in slots)
    if result.get('opponent_kind'):
        sides['blue'].update(kind=str(result['opponent_kind']), source='result.opponent_kind')
    for field in ('mode','difficulty'):
        value = result.get('opponent_'+field)
        if isinstance(value,str):
            sides['blue'][field] = value
            sides['blue']['source'] = 'result.opponent_'+field
    declaration = result.get('replay_strategy_metadata')
    origin = 'result.replay_strategy_metadata'
    sidecar = folder/'replay-strategies.json'
    receipt = None
    if sidecar.is_file():
        raw = sidecar.read_bytes()
        declaration = json.loads(raw.decode('utf-8-sig'))
        receipt = hashlib.sha256(raw).hexdigest()
        origin = 'replay-strategies.json (display declaration)'
    if declaration is not None:
        if not isinstance(declaration,dict) or declaration.get('schema_version') != 'competition-replay-strategies@1':
            raise ValueError('Invalid replay strategy declaration schema')
        for side in ('red','blue'):
            row = declaration.get(side, {})
            if not isinstance(row,dict):
                raise ValueError('Strategy side must be an object')
            for name in FIELDS:
                if name in row:
                    value = row[name]
                    if not isinstance(value,str) or len(value)>256:
                        raise ValueError('Strategy fields must be strings of at most 256 characters')
                    sides[side][name] = value
            if row:
                sides[side]['source'] = origin
                sides[side]['declaration_only'] = True
    return dict(schema_version='competition-replay-strategies@1', **sides,
                scenario_difficulty=brief.get('difficulty','unknown'),
                declaration_sha256=receipt, inference_calls_verified=False)
