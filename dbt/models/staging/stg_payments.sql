select
  order_id || '-' || payment_sequential as payment_id,
  order_id,
  payment_type,
  cast(payment_value as decimal(18, 2)) as amount
from {{ source('raw', 'order_payments') }}
