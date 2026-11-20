select
  order_id || '-' || order_item_id as order_item_id,
  order_id,
  product_id,
  cast(price as decimal(18, 2)) as price,
  cast(freight_value as decimal(18, 2)) as freight
from {{ source('raw', 'order_items') }}
