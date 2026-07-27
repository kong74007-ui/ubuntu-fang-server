# -*- coding: utf-8 -*-
"""AI 智能剪辑第一阶段支持的服务端导演风格。"""
import copy


_STYLES = (
    {
        "id": "knowledge_dynamic",
        "name": "知识快讲",
        "description": "快速提炼观点，用标题卡和关键词卡强化信息密度。",
        "director_rules": (
            "保留口播主体连续性；开头三秒给出结论；每个核心观点使用短标题卡；"
            "镜头节奏快但不得删改原意；补充素材只用于解释观点。"
        ),
    },
    {
        "id": "product_story",
        "name": "产品故事",
        "description": "围绕痛点、证据和结果组织产品展示。",
        "director_rules": (
            "按用户痛点、产品证据、使用过程、结果与行动引导排序；"
            "产品实拍素材优先；信息卡必须引用口播事实，不编造功效。"
        ),
    },
    {
        "id": "story_broll",
        "name": "故事画面",
        "description": "用全画幅情绪素材承接叙事，适合音频或故事类口播。",
        "director_rules": (
            "以完整叙事和情绪转折为主；用覆盖全画面的 B-roll 表达场景；"
            "画面切换跟随语义段落，音频必须连续且全程有视觉素材。"
        ),
    },
)


def list_styles():
    return copy.deepcopy(list(_STYLES))
