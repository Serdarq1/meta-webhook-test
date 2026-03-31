import os
from supabase import create_client, Client 

SUPABASE_URL=os.getenv("SUPABASE_URL")
SUPABASE_KEY=os.getenv("SUPABASE_KEY")

if not SUPABASE_KEY or not SUPABASE_URL:
    raise RuntimeError("Missing SUPABASE_URL or SUPABASE_KEY.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)