# 保质期资料约定

每日上午收货、午间需求、下午订货按时间戳判定，过期在零点发生。开局库存仅 inventory；receipts 是历史供货经验而非候选策略的固定未来入库。外生情景应描述需求与供货冲击，不随策略改变抽样序列。实际销量是历史履约结果，requested_kg 由独立意向记录提供但有缺失。返回品均报废，金额以元计，公斤可取连续值。供货容量共享于同日同一供应商的各品种。

## 数据表

### `inventory`

模拟起点实际可用批次；到 expires_at 即不可售，过期时刻不是最后可售时刻。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `sku` | `text` |
| `quantity_kg` | `number` |
| `available_at` | `time` |
| `expires_at` | `time` |
| `unit_cost` | `money` |

### `supplier_days`

供应商日共同冲击和可交付容量，所有品种共享，不是模拟时可提前得知的信号。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `day` | `date` |
| `supplier` | `text` |
| `common_delay_days` | `int` |
| `capacity_kg` | `number` |

### `receipts`

历史实际到货，用于供货情景资料；只在 available_at 后成为策略可见的实物，不能直接当候选策略的既定订量。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `sku` | `text` |
| `supplier` | `text` |
| `ordered_at` | `time` |
| `expected_day` | `date` |
| `available_at` | `time` |
| `expires_at` | `time` |
| `quantity_kg` | `number` |
| `unit_cost` | `money` |
| `supplier_day_id` | `text` |

### `demand`

独立意向量与实际售出量分开；意向空值表示未采到，不是用销量替代的许可。退货报废不回到可售库存。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `sku` | `text` |
| `requested_at` | `time` |
| `requested_kg` | `number?` |
| `sold_kg` | `number` |
| `price` | `money` |
| `returned_kg` | `number` |
| `return_disposition` | `text` |

### `policy`

经营成本与每日规则，库存按公斤连续计量；订货发生在需求之后的下午截止点。

| 字段 | 解析类型 |
| --- | --- |
| `id` | `text` |
| `order_cutoff` | `text` |
| `daily_holding_per_kg` | `money` |
| `disposal_per_kg` | `money` |
| `ordering_fee` | `money` |
| `demand_unit` | `text` |

## 文件和数值语义

空单元格只在带问号的类型中表示缺失；零和空值不同。金额由十进制定点字符串保存，浮点数仅用于探索性统计。所有数据均为本地生成的虚构经营记录，不对应真实公司、客户或住址。数值剖面只反映观察数据，不能代替总体推断。

研究备注的参数为业务自由对象，不代表已执行了任何统计模型。原始表与已有视图可单独查询。
