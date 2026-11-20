-- Each mart total must equal the same rule computed straight from the raw tables.
-- Returns one row per mismatch.
with delivered as (
  select order_id from {{ source('raw', 'orders') }} where order_status = 'delivered'
),
checks as (
  select 'revenue' as check_name,
    (select sum(i.price) from {{ ref('fct_order_items') }} i join {{ ref('fct_orders') }} o using (order_id) where o.status = 'delivered') as mart,
    (select sum(cast(price as decimal(18, 2))) from {{ source('raw', 'order_items') }} where order_id in (select order_id from delivered)) as raw
  union all
  select 'payments',
    (select sum(p.amount) from {{ ref('fct_payments') }} p join {{ ref('fct_orders') }} o using (order_id) where o.status = 'delivered'),
    (select sum(cast(payment_value as decimal(18, 2))) from {{ source('raw', 'order_payments') }} where order_id in (select order_id from delivered))
  union all
  select 'orders', (select count(*) from {{ ref('fct_orders') }}), (select count(*) from {{ source('raw', 'orders') }})
  union all
  select 'customers', (select count(*) from {{ ref('dim_customers') }}),
    (select count(distinct customer_unique_id) from {{ source('raw', 'customers') }})
  union all
  select 'repeat_customers', (select count(*) from {{ ref('dim_customers') }} where orders >= 2),
    (select count(*) from (select c.customer_unique_id from {{ source('raw', 'orders') }} o
       join {{ source('raw', 'customers') }} c using (customer_id) group by 1 having count(*) >= 2))
  union all
  select 'late_orders', (select count(*) from {{ ref('fct_orders') }} where is_late),
    (select count(*) from {{ source('raw', 'orders') }} where order_status = 'delivered'
       and cast(order_delivered_customer_date as date) > cast(order_estimated_delivery_date as date))
  union all
  select 'reviewed_orders', (select count(review_score) from {{ ref('fct_orders') }}),
    (select count(distinct order_id) from {{ source('raw', 'order_reviews') }})
)
select * from checks where mart is distinct from raw
