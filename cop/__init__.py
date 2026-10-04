"""cop —— 群管插件私有子包（重构分层目录）。

分层约定（只允许上层依赖下层，同层不互相 import）：
    L0 constants / compat / text_utils
    L1 json_store / config_store
    L2 permissions / message_parse / onebot_api / runtime
    L3 history / stats / messaging / moderation / conversational / join_review
    L4 main.py（门面：保留 @register 类与全部 @filter 入口）

阶段 1 落地 L0 / L1，阶段 2 落地 L2（permissions / message_parse / onebot_api /
runtime）。子模块之间保持零耦合：L2 同层不互相 import，跨层依赖（如 permissions 需要
raw 解析、onebot_api 需要 config_store）一律由 main.py 门面在构造时以实例注入；
config_store 作为最底层公共依赖，不被反向依赖。main.py 内所有被迁移方法保留同名薄包装
委托，外部调用点零改动。

近期物理归并（不改对外契约、不破坏分层）：
    target_resolver.py → message_parse.py（同层 L2「报文/目标解析」，TargetResolver 归入）
    colloquial.py + dup_face.py → conversational.py（同层 L3b「群消息即时处理」域）
"""
