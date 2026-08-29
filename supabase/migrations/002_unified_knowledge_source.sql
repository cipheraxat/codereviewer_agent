-- Unified knowledge index: tag embeddings by source (code, jira, confluence).

alter table public.code_embeddings
  add column if not exists source text not null default 'code';

create index if not exists code_embeddings_repo_source_idx
  on public.code_embeddings (repo, source);

create or replace function public.match_code_embeddings(
  query_embedding vector(1536),
  match_repo text,
  match_count integer,
  match_threshold double precision default 0.55
)
returns table (
  path text,
  content text,
  similarity double precision,
  source text
)
language sql stable
as $$
  select
    ce.path,
    ce.content,
    1 - (ce.embedding <=> query_embedding) as similarity,
    ce.source
  from public.code_embeddings ce
  where ce.repo = match_repo
    and ce.embedding is not null
    and 1 - (ce.embedding <=> query_embedding) >= match_threshold
  order by ce.embedding <=> query_embedding
  limit match_count;
$$;
