ALTER TABLE checkpoint_writes
ADD COLUMN IF NOT EXISTS writer_task_id TEXT;
