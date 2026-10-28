select * from {{ ref('orders') }} where id < 1
