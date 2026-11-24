# -*- coding: utf-8 -*-
"""兼容再导出：路由已迁至顶级 ``opcodes.index``。"""
from opcodes.index import GameRouteResult, route_dynamic_game_packet

__all__ = ["GameRouteResult", "route_dynamic_game_packet"]
