-- Run once in Supabase SQL Editor before starting the admin bot.
-- Adds missing status codes; does not delete or modify existing content/policies.
begin;
lock table public.post_statuses, public.comment_statuses in exclusive mode;
insert into public.post_statuses(id,code,name,sort_order)
select coalesce(max(id),0)+1,'rejected','адхілена',coalesce(max(sort_order),0)+1
from public.post_statuses
having not exists(select 1 from public.post_statuses where code='rejected');
insert into public.comment_statuses(id,code,name,sort_order)
select coalesce(max(id),0)+1,'rejected','адхілена',coalesce(max(sort_order),0)+1
from public.comment_statuses
having not exists(select 1 from public.comment_statuses where code='rejected');
commit;
