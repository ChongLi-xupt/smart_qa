# 业务术语表（glossary）
# 每行格式：术语: 定义（也支持中文冒号）。以 # 开头的行为注释。
# 术语表内容会随系统提示词注入，帮助模型正确理解业务口径。
# 当前库：ecommerce_db，口径与真实字段对应。
# 注：查询结果列的界面展示别名（图表/表头中文列名）请在
#     config/column_aliases.yaml 中维护，本文件只负责业务口径。
新增用户: register_time 落在统计区间内的 users 记录
销售额: orders 表 amount 字段的合计
热门商品: 按 orders.product_id 统计订单数最多的商品
用户规模: users 表的记录总数
