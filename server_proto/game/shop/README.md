# `game/shop/` - 商城领域

全局规则见 [`../../README.md`](../../README.md)。

## 职责

- 从共享预加载的 `USDOF/Script.pvf` 动态建立商城商品索引。
- 商品按 `commodity_id` 查找。若 PVF 后续行重复同一 ID，保留首条并跳过后续条目；启动日志会写出保留项、跳过项和原因。
- 数量按 section schema 解释：普通道具/材料保留脚本数量，时装、礼包和月度商品发一个实例。
- 发货容器、堆叠、职业限制和期限由商品对应的物品 PVF 元数据决定。
- 职业限制同时校验物品脚本的 `[usable job]` 与 `[suitable job]`。
- C1/0040 的 +2 是购物车商品记录数，不是购买倍数；单件原生 writer 固定写入 1，批量购物车后跟同数的 8B 商品记录。每条记录只购买一份商品，发货数量与点券价格只取当前商品记录。
- `[usable cera package]` 的购买与开启是两步：当前 PVF 的商城商品与礼包源物品 ID 可解析时，`0040` 即允许购买并把礼包箱放进 `list0`，不得因礼包奖励索引不完整而在购买阶段拒绝；`0207` 再独立校验内容、消费礼包并发放奖励。
- `premiumlist_new.etc` 中受支持的账号合约在 `0040` 结算时直接激活，不进入任何背包；同类型从现有到期时间续期，过期后从当前时间重新起算。
- 合约期限与所有日/分钟别名只认当前运行 PVF。进房 START 同步全部未过期账号合约，历史误入 `list0` 的合约按原 `created_at` 吸收，已经过期的只清理背包记录。
- 晶体契约使用 PVF `premium_type=92`，商城购买后随 `0042` 直接激活并发送 `S0/0383` 当前角色选择，不生成背包实例。仓库选择走 `C1/S1 0218`，C1 正文固定为 `00 + (0..5|FF)`；选择按角色持久化，进房在 USERINFO1 后以 `01 + selection` 恢复，契约未激活时下发 `01 FF` 清客户端状态。
- Neo Premium Basic 与 Plus 独立保存：Basic 在 Plus 之后排队；续购 Plus 时，尚未结束的 Basic 同步顺延，确保 Plus 始终优先生效。
- Neo Premium Plus 的 3 天/7 天 Cera booster 外壳沿当前 PVF 的确定性奖励链归一到 type 118；商城购买直接激活，历史背包外壳按完整期限续到现有 Plus 末尾并同步顺延 Basic，不先落成中间 booster 或契约物品。
- 霸王契约只把装备使用等级门放宽 10 级，覆盖普通/时装/宠物装备与 Blade Dancer 主副武器换位；职业、槽位和物品类型限制保持不变。达人契约把手动学习与自动学习的等级门放宽 5 级（90 级角色按有效 95 级学习），角色等级、SP/TP 总账和 PVF 技能绝对上限保持不变。
- Neo Premium Plus 武器租赁只读取当前 PVF 的 `[Rental Service] 3` 武器；C1/`04B5` 的 group `0` 对鬼剑士、黑暗武士、女鬼剑士按固定 subtype 顺序 `(0, 3, 2, 1, 5)` 选型，其他职业沿用 subtype 升序，并按角色等级（含霸王契约 +10）发放不高于有效等级的最高模板。再次选择时先原子撤回背包中的旧租赁实例；若唯一旧租赁正在穿戴，则在原穿戴槽直接替换成新模板。S1/`04B5` 先写通用 `success=1`，再写撤回位置行；穿戴替换必须带一行 `list_type=3 + slot=12`，由 Stage3 原生 reader 撤下旧对象，随后用 `000E list3` 同步新装备。永久自有武器不参与回收。领取当下仍要求 Plus 有效，租赁实例固定在下一次北京时间 09:00 日切到期，不跟随 Plus 总期限。
- Neo Premium Plus 每日礼包由 C1/`04B6` 领取；奖励完整读取 boot-warm 后 `premium_contract_index` 的 type 118 `[items]` 有序 `item_id, count` 对，不再固定为 `50002547`。账号每天中国时间 09:00 重置一次，全部奖励和领取记录在同一事务提交；每项按目标物品的当前 PVF 元数据进入 Main/Avatar/Pet/Guild Medal 容器，并统一在下次重置时到期，任一项无元数据或容量不足都会整体回滚。
- 所有 `expire_time` 已到的背包或穿戴实例在背包快照、角色重登和物品读取前清理；在线领取跨过期边界时同步刷新受影响容器。
- 当前所有 section 均允许重复购买；账号购买计数与事务限额能力保留但不启用，待后续限购同步闭环。
- 礼包奖励只读取物品脚本的 `[package data]`；`[cs display only]` 是商城预览，不参与发货。`0207` 按奖励逐项解析，PVF 元数据有效的条目正常下发，缺失或类型错误的条目自动过滤；全部条目均无效时不消耗源礼包。
- `item_id=42` 是真实 Life Token 奖励，所有普通获取路径直接增加账号复活币并刷新固定 `item_id=1` / slot 1 钱包投影，不创建背包行。`0207/00A0` 中的账号契约和容量扩展券也按预热后的 PVF 身份在开盒事务内直接生效；同名 Booster/礼包先按容器定义展开。历史误入 Main 的同类物品在 START ITEMLIST 前吸收，固定钱包投影不参与迁移。`001B/00D0` 因结果 reader 依赖真实奖励槽而继续保留对象合同。
- `0207` 请求携带礼包来源槽位和全部时装的 `item_id/option`；时装集合必须与 PVF 完全一致，option 持久化到 `list1` 实例。

## 边界

- C2S/S2C 拼包放在 `opcodes/opcode0040-buy_cerashop_item.py`。
- Plus 武器与礼包领取分别放在 `opcodes/opcode04b5-rent_neo_premium_weapon.py`、`opcodes/opcode04b6-claim_neo_premium_gift.py`。
- 晶体契约选择拼包放在 `opcodes/opcode0218-update_cube_contract.py`，状态/校验放在 `game/shop/cube_contract.py`。
- 礼包开启拼包放在 `opcodes/opcode0207-open_cera_package.py`。
- 点券购买的扣款与发货，以及礼包消费与多奖励写入，分别由 `game/character/character_db.py` 在单个事务内完成。
- 复活币 `item_id=42` 的商城购入、礼包奖励、普通发放和副本扣除会与余额变化在同一事务写入账号流水；即使购入后立即复活导致余额回到零，仍可追溯完整变化。
- 点券合约的扣款与账号期限续期同样由 `game/character/character_db.py` 在一个事务内完成；成功 ACK 后追加当前 Stage3 的 `S0/0042` 激活通知，晶体契约再追加 `S0/0383` 角色选择状态。
- 商城购买只写背包 `list0` 或时装栏 `list1`，不写穿戴表或装备栏 `list3`。
