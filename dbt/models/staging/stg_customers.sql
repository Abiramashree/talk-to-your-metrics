-- customer_id is issued per order; customer_unique_id is the person.
select customer_id, customer_unique_id, customer_state
from {{ source('raw', 'customers') }}
