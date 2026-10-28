{{ config(materialized='ephemeral') }}
select id from {{ source('raw', 'input') }}
